// Bridge frontend store — Svelte 5 runes
// Three-layer state: session mirror, event-derived, role config

import { api } from './api.js'
import { handleEvent } from './event-handler.js'
import { hydrateSession } from './hydrator.js'
import type {
  SessionStatus, SessionState, AgentPanel, LogEntry, VersionEntry,
  RoleConfig, ToolInfo, RoleConfigResponse, EventsResponse,
  BridgeEvent, WireEvent, TERMINAL_STATES,
} from './types.js'
import { createEmptyState } from './types.js'

// Layer 1: backend mirror
let session = $state<SessionState>({
  status: 'idle', round: 0, max_rounds: 5,
  consensus: false, consensus_round: 0,
  history_len: 0, error: null,
  planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
  executor_panel: 'planner',
})

// Layer 2: event-derived
let logs = $state<Record<AgentPanel, LogEntry[]>>({ planner: [], reviewer: [] })
let versions = $state<Record<AgentPanel, VersionEntry[]>>({ planner: [], reviewer: [] })
let executionResult = $state<string | null>(null)
let showExecResult = $state(false)
let executorPanel = $state<AgentPanel>('planner')

// Layer 2 attached: UI state
let activeVer = $state<Record<AgentPanel, number>>({ planner: -1, reviewer: -1 })
let activeTab = $state<Record<AgentPanel, 'log' | 'result'>>({ planner: 'log', reviewer: 'log' })
let activeFold = $state<Record<AgentPanel, string | null>>({ planner: null, reviewer: null })
let foldSeq = $state(0)
let doneBadge = $state<string | null>(null)

// Layer 3: role config
let roleConfig = $state<RoleConfig>({ planner_tool_id: 'claude-code', reviewer_tool_id: 'codex' })
let toolMap = $state<Record<string, ToolInfo>>({})
let execNote = $state('')

// Inject bar state (shared between InjectBar and ControlBar's "继续协商")
let injectValue = $state('')

// Session tracking
let sid = $state<string | null>(null)
let cursor = $state(0)
let pollHandle = $state<ReturnType<typeof setInterval> | null>(null)

// Derived
let toolDisplayNames = $derived({
  planner: toolMap[roleConfig.planner_tool_id]?.display_name ?? 'Planner',
  reviewer: toolMap[roleConfig.reviewer_tool_id]?.display_name ?? 'Reviewer',
})

let canStart = $derived(['idle', 'done', 'error'].includes(session.status))
let canStop = $derived(['running', 'executing', 'review_pending'].includes(session.status))
let canExecute = $derived(session.status === 'consensus' || session.status === 'max_rounds')
let canContinue = $derived(session.status === 'max_rounds' || session.status === 'consensus')
let canFix = $derived(session.status === 'review_fix')
let canInject = $derived(session.status !== 'consensus')

function getAppState(): import('./types.js').AppState {
  return {
    session, logs, versions, executionResult, showExecResult, executorPanel,
    activeVer, activeTab, activeFold, foldSeq, doneBadge,
    roleConfig, toolDisplayNames,
  }
}

function applyAppState(s: import('./types.js').AppState): void {
  session = s.session
  logs = s.logs
  versions = s.versions
  executionResult = s.executionResult
  showExecResult = s.showExecResult
  executorPanel = s.executorPanel
  activeVer = s.activeVer
  activeTab = s.activeTab
  activeFold = s.activeFold
  foldSeq = s.foldSeq
  doneBadge = s.doneBadge
  roleConfig = s.roleConfig
}

// Actions
async function doStart(path: string, task: string, rounds: number) {
  const r = await api<{ session_id?: string; error?: string }>('POST', '/api/start', {
    project_path: path, task, max_rounds: rounds,
  })
  if (r.error) { alert(r.error); return }
  sid = r.session_id!
  const u = new URL(location.href)
  u.searchParams.set('sid', sid)
  if (u.searchParams.has('project')) u.searchParams.delete('project')
  history.replaceState(null, '', u)
  logs = { planner: [], reviewer: [] }
  versions = { planner: [], reviewer: [] }
  activeVer = { planner: -1, reviewer: -1 }
  activeTab = { planner: 'log', reviewer: 'log' }
  activeFold = { planner: null, reviewer: null }
  foldSeq = 0
  executionResult = null
  showExecResult = false
  doneBadge = null
  cursor = 0
  startPolling()
}

async function doStop() {
  if (!sid) return
  await api('POST', '/api/stop', { session_id: sid })
}

async function doExec() {
  if (!sid) return
  if (!confirm('确认执行？执行者将用 --dangerously-skip-permissions')) return
  const r = await api<{ error?: string }>('POST', '/api/execute', { session_id: sid })
  if (r.error) alert('执行启动失败: ' + r.error)
}

async function doContinue(extraRounds: number) {
  if (!sid) return
  const payload: Record<string, unknown> = { session_id: sid, extra_rounds: extraRounds }
  const wasConsensus = session.status === 'consensus'
  if (wasConsensus) {
    const reason = injectValue.trim()
    if (!reason) { alert('请在输入框中填写驳回理由'); return }
    payload.message = reason
  }
  const r = await api<{ error?: string }>('POST', '/api/continue', payload)
  if (r.error) { alert(r.error); return }
  if (wasConsensus) injectValue = ''
  if (!pollHandle) startPolling()
}

async function doInject() {
  if (!sid) return
  if (session.status === 'consensus') {
    alert('共识状态下请使用"继续协商"提交驳回理由')
    return
  }
  const msg = injectValue.trim()
  if (!msg) return
  await api('POST', '/api/inject', { session_id: sid, message: msg })
  injectValue = ''
}

async function doReviewFix() {
  if (!sid) return
  if (!confirm('确认修复？执行者将继续用 --dangerously-skip-permissions')) return
  await api('POST', '/api/review_fix', { session_id: sid })
}

async function doReviewSkip() {
  if (!sid) return
  await api('POST', '/api/review_skip', { session_id: sid })
}

// Polling
function startPolling() {
  if (pollHandle) clearInterval(pollHandle)
  pollHandle = setInterval(pollEvents, 300)
}

function stopPolling() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null }
}

async function pollEvents() {
  if (!sid) return
  try {
    const r = await api<EventsResponse>('GET', `/api/events?sid=${sid}&since=${cursor}`)
    if (r.events) {
      const appState = getAppState()
      for (const wireEvt of r.events) {
        handleEvent(wireEvt as unknown as BridgeEvent, appState)
      }
      applyAppState(appState)
    }
    cursor = r.next
    const s = await api<SessionState>('GET', `/api/state?sid=${sid}`)
    session = s
    if (['idle', 'done', 'error'].includes(s.status)) stopPolling()
  } catch {
    // Network errors — silently retry on next poll
  }
}

// Init role config (must be called before hydration)
async function initRoleConfig() {
  try {
    const cfg = await api<RoleConfigResponse>('GET', '/api/role_config')
    roleConfig = { planner_tool_id: cfg.planner_tool_id, reviewer_tool_id: cfg.reviewer_tool_id }
    const map: Record<string, ToolInfo> = {}
    for (const t of cfg.tools ?? []) map[t.id] = t
    toolMap = map

    const notes: string[] = ['工具状态来自启动时扫描；保存角色配置时会实时校验安装状态']
    if (cfg.executor_tool_id !== cfg.planner_tool_id) {
      notes.push('执行者: ' + (map[cfg.executor_tool_id]?.display_name ?? cfg.executor_tool_id) + '（按能力自动选择）')
    }
    const pTool = map[cfg.planner_tool_id]
    const rTool = map[cfg.reviewer_tool_id]
    if (pTool?.last_checked_at) notes.push('Planner 扫描: ' + pTool.last_checked_at)
    if (rTool?.last_checked_at && rTool.id !== pTool?.id) notes.push('Reviewer 扫描: ' + rTool.last_checked_at)
    if (pTool?.probe_error) notes.push('Planner: ' + pTool.probe_error)
    if (rTool?.probe_error) notes.push('Reviewer: ' + rTool.probe_error)
    execNote = notes.join(' ｜ ')
  } catch {
    // silent
  }
}

async function onRoleChange(plannerId: string, reviewerId: string) {
  const r = await api<{ error?: string }>('POST', '/api/role_config', {
    planner_tool_id: plannerId, reviewer_tool_id: reviewerId,
  })
  if (r.error) { alert(r.error); await initRoleConfig(); return }
  roleConfig = { planner_tool_id: plannerId, reviewer_tool_id: reviewerId }
  await initRoleConfig()
}

// Hydration (called from App.svelte onMount)
async function initFromUrl() {
  await initRoleConfig()
  const p = new URLSearchParams(location.search)
  const urlProject = p.get('project')
  const urlSid = p.get('sid')
  if (urlSid) {
    sid = urlSid
    const appState = getAppState()
    cursor = await hydrateSession(urlSid, appState)
    applyAppState(appState)
    startPolling()
  }
  return { urlProject }
}

// Version tab selection
function selectVersion(agent: AgentPanel, idx: number) {
  if (idx === -2) {
    showExecResult = true
  } else {
    showExecResult = false
    activeVer[agent] = idx
  }
}

function switchTab(agent: AgentPanel, tab: 'log' | 'result') {
  activeTab[agent] = tab
}

// Prompt config
async function loadPrompts(): Promise<Record<string, string>> {
  return api<Record<string, string>>('GET', '/api/prompts')
}

async function savePrompts(body: Record<string, string>): Promise<{ ok?: boolean; error?: string }> {
  return api<{ ok?: boolean; error?: string }>('POST', '/api/prompts', body)
}

// Path autocomplete
async function completePath(prefix: string) {
  return api<{ suggestions: { path: string; name: string; is_git: boolean }[] }>('GET',
    '/api/complete?prefix=' + encodeURIComponent(prefix))
}

async function recentPaths() {
  return api<{ paths: string[] }>('GET', '/api/recent_paths')
}

// Browse
async function browseDir(path: string) {
  return api<{
    current: string; parent: string | null; dirs: { path: string; name: string; is_git: boolean }[];
    is_git: boolean; truncated: boolean; error?: string
  }>('GET', '/api/browse?path=' + encodeURIComponent(path))
}

export const store = {
  get session() { return session },
  get logs() { return logs },
  get versions() { return versions },
  get executionResult() { return executionResult },
  get showExecResult() { return showExecResult },
  get executorPanel() { return executorPanel },
  get activeVer() { return activeVer },
  get activeTab() { return activeTab },
  get doneBadge() { return doneBadge },
  get roleConfig() { return roleConfig },
  get toolMap() { return toolMap },
  get toolDisplayNames() { return toolDisplayNames },
  get execNote() { return execNote },
  get sid() { return sid },
  get injectValue() { return injectValue },
  set injectValue(v: string) { injectValue = v },
  get canStart() { return canStart },
  get canStop() { return canStop },
  get canExecute() { return canExecute },
  get canContinue() { return canContinue },
  get canFix() { return canFix },
  get canInject() { return canInject },
  doStart, doStop, doExec, doContinue, doInject, doReviewFix, doReviewSkip,
  initFromUrl, onRoleChange, selectVersion, switchTab,
  loadPrompts, savePrompts, completePath, recentPaths, browseDir,
}
