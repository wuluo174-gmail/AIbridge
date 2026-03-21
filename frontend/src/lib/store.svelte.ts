// Bridge frontend store — Svelte 5 runes
// Three-layer state: session mirror, event-derived, role config

import { api } from './api.js'
import { showConfirm, showAlert } from './dialog.svelte.js'
import { handleEvent } from './event-handler.js'
import { hydrateSession } from './hydrator.js'
import { t, getLocale, setLocale } from './i18n.svelte.js'
import { PHASE_KEYS } from './protocol.js'
import { resolveDisplayNames } from './types.js'
import type {
  SessionState, AgentPanel, LogEntry, VersionEntry,
  RoleConfig, ToolInfo, RoleConfigResponse, EventsResponse,
  BridgeEvent, TabData, ArchivedSession, ArchivedHistoryResponse,
} from './types.js'
import { createEmptyState } from './types.js'

function tPhase(phase: string): string {
  const key = PHASE_KEYS[phase]
  return key ? t(key) : phase
}
function switchLocale() {
  setLocale(getLocale() === 'zh-CN' ? 'en-US' : 'zh-CN')
}

// Layer 1: backend mirror
let session = $state<SessionState>({
  status: 'idle', round: 0, max_rounds: 5,
  consensus: false, consensus_round: 0,
  history_len: 0, error: null,
  planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
  executor_panel: 'planner', review_round: 0, max_review_rounds: 3,
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

// Layer 3: role config — split into global default + per-session
let defaultRoleConfig = $state<RoleConfig>({ planner_tool_id: 'claude-code', reviewer_tool_id: 'codex' })
let sessionRoleConfig = $state<RoleConfig>({ planner_tool_id: 'claude-code', reviewer_tool_id: 'codex' })
let toolMap = $state<Record<string, ToolInfo>>({})
let execNote = $state('')

// Theme
let theme = $state<'dark' | 'light'>(
  (typeof localStorage !== 'undefined' && localStorage.getItem('bridge-theme') as 'dark' | 'light') || 'dark'
)

function toggleTheme() {
  theme = theme === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', theme)
  localStorage.setItem('bridge-theme', theme)
}

let injectValue = $state('')
let sid = $state<string | null>(null)
let cursor = $state(0)
let pollHandle = $state<ReturnType<typeof setInterval> | null>(null)

// Tab management
let tabs = $state<TabData[]>([])
let activeTabId = $state('')
let tabSeq = $state(0)
let pollGeneration = $state(0)

// Form state (per-tab, lifted from components)
let projectPath = $state('')
let taskValue = $state('')
let roundsValue = $state(5)
let extraRounds = $state(3)

function roleFallback(role: import('./types.js').AgentPanel): string {
  return role === 'planner' ? t('role.planner') : t('role.reviewer')
}

// Derived — for component rendering only (not in AppState)
let toolDisplayNames = $derived(resolveDisplayNames(sessionRoleConfig, toolMap, roleFallback))

let canStart = $derived(['idle', 'done', 'error'].includes(session.status))
let canStop = $derived(['running', 'executing', 'review_pending'].includes(session.status))
let canExecute = $derived(session.status === 'consensus' || session.status === 'max_rounds')
let canContinue = $derived(session.status === 'max_rounds' || session.status === 'consensus')
let canFix = $derived(session.status === 'review_fix')
let canReviewContinue = $derived(session.status === 'review_max_rounds')
let canInject = $derived(session.status !== 'consensus')

function getAppState(): import('./types.js').AppState {
  return $state.snapshot({
    session, logs, versions, executionResult, showExecResult, executorPanel,
    activeVer, activeTab, activeFold, foldSeq, doneBadge, sessionRoleConfig,
  })
}

function applyAppState(s: import('./types.js').AppState): void {
  session = s.session; logs = s.logs; versions = s.versions
  executionResult = s.executionResult; showExecResult = s.showExecResult; executorPanel = s.executorPanel
  activeVer = s.activeVer; activeTab = s.activeTab; activeFold = s.activeFold
  foldSeq = s.foldSeq; doneBadge = s.doneBadge; sessionRoleConfig = s.sessionRoleConfig
}

function saveCurrentTab() {
  const current = tabs.find(t => t.id === activeTabId)
  if (!current) return
  current.snapshot = getAppState()
  current.sid = sid; current.cursor = cursor
  current.projectPath = projectPath; current.taskValue = taskValue
  current.roundsValue = roundsValue; current.extraRounds = extraRounds; current.injectValue = injectValue
}
function loadTab(target: TabData) {
  applyAppState(target.snapshot)
  sid = target.sid; cursor = target.cursor
  projectPath = target.projectPath; taskValue = target.taskValue
  roundsValue = target.roundsValue; extraRounds = target.extraRounds; injectValue = target.injectValue
}
function syncUrl() {
  const u = new URL(location.href)
  if (sid) u.searchParams.set('sid', sid); else u.searchParams.delete('sid')
  history.replaceState(null, '', u)
}

// Actions
async function doStart(path: string, task: string, rounds: number) {
  const r = await api<{ session_id?: string; error?: string }>('POST', '/api/start', {
    project_path: path, task, max_rounds: rounds,
  })
  if (r.error) { await showAlert(r.error); return }
  sid = r.session_id!
  sessionRoleConfig = $state.snapshot(defaultRoleConfig)
  const tab = tabs.find(t => t.id === activeTabId)
  if (tab) { tab.sid = sid; tab.label = task.slice(0, 20) || '新会话' }
  syncUrl()
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
  const sidAtOpen = sid
  if (!(await showConfirm(t('dialog.confirm_exec')))) return
  if (sid !== sidAtOpen || !canExecute) return
  const r = await api<{ error?: string }>('POST', '/api/execute', { session_id: sid })
  if (r.error) await showAlert(t('dialog.exec_fail') + r.error)
}

async function doContinue(extraRounds: number) {
  if (!sid) return
  const payload: Record<string, unknown> = { session_id: sid, extra_rounds: extraRounds }
  const wasConsensus = session.status === 'consensus'
  if (wasConsensus) {
    const reason = injectValue.trim()
    if (!reason) { await showAlert(t('dialog.reject_reason')); return }
    payload.message = reason
  }
  const r = await api<{ error?: string }>('POST', '/api/continue', payload)
  if (r.error) { await showAlert(r.error); return }
  if (wasConsensus) injectValue = ''
  if (!pollHandle) startPolling()
}

async function doInject() {
  if (!sid) return
  if (session.status === 'consensus') {
    await showAlert(t('dialog.consensus_hint'))
    return
  }
  const msg = injectValue.trim()
  if (!msg) return
  await api('POST', '/api/inject', { session_id: sid, message: msg })
  injectValue = ''
}

async function doReviewFix() {
  if (!sid) return
  const sidAtOpen = sid
  if (!(await showConfirm(t('dialog.confirm_fix')))) return
  if (sid !== sidAtOpen || !canFix) return
  const r = await api<{ error?: string }>('POST', '/api/review_fix', { session_id: sid })
  if (r.error) await showAlert(r.error)
}

async function doReviewSkip() {
  if (!sid) return
  await api('POST', '/api/review_skip', { session_id: sid })
}

async function doReviewContinue(extraRounds: number) {
  if (!sid) return
  const r = await api<{ error?: string }>('POST', '/api/review_continue', {
    session_id: sid, extra_rounds: extraRounds,
  })
  if (r.error) { await showAlert(r.error); return }
  if (!pollHandle) startPolling()
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
  const gen = pollGeneration; const pollSid = sid
  try {
    const r = await api<EventsResponse>('GET', `/api/events?sid=${pollSid}&since=${cursor}`)
    if (pollGeneration !== gen) return
    if (r.events) {
      const appState = getAppState()
      const names = resolveDisplayNames(appState.sessionRoleConfig, toolMap, roleFallback)
      for (const wireEvt of r.events) handleEvent(wireEvt as unknown as BridgeEvent, appState, names)
      applyAppState(appState)
    }
    cursor = r.next
    const s = await api<SessionState>('GET', `/api/state?sid=${pollSid}`)
    if (pollGeneration !== gen) return
    session = s
    if (['idle', 'done', 'error'].includes(s.status)) stopPolling()
  } catch {}
}

async function catchUpAndPoll(sessionSid: string, fromCursor: number) {
  const gen = pollGeneration
  try {
    const s = await api<SessionState>('GET', `/api/state?sid=${sessionSid}`)
    if (pollGeneration !== gen) return
    session = s
    if (s.planner_tool_id) sessionRoleConfig = { planner_tool_id: s.planner_tool_id, reviewer_tool_id: s.reviewer_tool_id }
    const r = await api<EventsResponse>('GET', `/api/events?sid=${sessionSid}&since=${fromCursor}`)
    if (pollGeneration !== gen) return
    if (r.events?.length) {
      const appState = getAppState()
      const names = resolveDisplayNames(appState.sessionRoleConfig, toolMap, roleFallback)
      for (const evt of r.events) handleEvent(evt as unknown as BridgeEvent, appState, names)
      applyAppState(appState)
    }
    cursor = r.next
    if (!['idle', 'done', 'error'].includes(s.status)) startPolling()
  } catch {}
}

// Init role config (must be called before hydration)
async function initRoleConfig() {
  try {
    const cfg = await api<RoleConfigResponse>('GET', '/api/role_config')
    defaultRoleConfig = { planner_tool_id: cfg.planner_tool_id, reviewer_tool_id: cfg.reviewer_tool_id }
    if (session.status === 'idle') sessionRoleConfig = { ...defaultRoleConfig }
    const map: Record<string, ToolInfo> = {}
    for (const tool of cfg.tools ?? []) map[tool.id] = tool
    toolMap = map

    const notes: string[] = []
    if (cfg.executor_tool_id !== cfg.planner_tool_id) {
      notes.push(t('note.executor', { name: map[cfg.executor_tool_id]?.display_name ?? cfg.executor_tool_id }))
    }
    const pTool = map[cfg.planner_tool_id]
    const rTool = map[cfg.reviewer_tool_id]
    if (pTool?.probe_error) notes.push(t('role.planner') + ': ' + pTool.probe_error)
    if (rTool?.probe_error && rTool.id !== pTool?.id) notes.push(t('role.reviewer') + ': ' + rTool.probe_error)
    execNote = notes.join(' ｜ ')
  } catch {
    // silent
  }
}

async function onRoleChange(plannerId: string, reviewerId: string) {
  const r = await api<{ error?: string }>('POST', '/api/role_config', {
    planner_tool_id: plannerId, reviewer_tool_id: reviewerId,
  })
  if (r.error) { await showAlert(r.error); await initRoleConfig(); return }
  defaultRoleConfig = { planner_tool_id: plannerId, reviewer_tool_id: reviewerId }
  if (session.status === 'idle') sessionRoleConfig = { ...defaultRoleConfig }
  await initRoleConfig()
}

// Hydration (called from App.svelte onMount)
function newTab() {
  saveCurrentTab(); stopPolling(); pollGeneration++
  const id = `tab-${++tabSeq}`; const snapshot = createEmptyState()
  snapshot.sessionRoleConfig = $state.snapshot(defaultRoleConfig)
  tabs.push({ id, label: '新会话', sid: null, cursor: 0, snapshot,
    projectPath: '', taskValue: '', roundsValue: 5, extraRounds: 3, injectValue: '' })
  applyAppState(snapshot); sid = null; cursor = 0
  projectPath = ''; taskValue = ''; roundsValue = 5; extraRounds = 3; injectValue = ''
  activeTabId = id; syncUrl()
}

function switchToTab(targetId: string) {
  if (targetId === activeTabId) return
  stopPolling(); pollGeneration++; saveCurrentTab()
  const target = tabs.find(t => t.id === targetId)!; loadTab(target); activeTabId = targetId
  if (sid && !['idle', 'done', 'error'].includes(session.status)) catchUpAndPoll(sid, cursor)
  syncUrl()
}

async function closeTab(tabId: string) {
  if (tabs.length <= 1) return
  const tab = tabs.find(t => t.id === tabId)!
  if (tab.sid) {
    const tabSession = tabId === activeTabId ? session : tab.snapshot.session
    if (!['idle', 'done', 'error'].includes(tabSession.status))
      if (!await showConfirm('该标签页的会话仍在运行，关闭后可通过页面刷新恢复。确认关闭？')) return
  }
  const idx = tabs.findIndex(t => t.id === tabId)
  if (tabId === activeTabId) {
    stopPolling(); pollGeneration++
    const next = tabs[idx === 0 ? 1 : idx - 1]; loadTab(next); activeTabId = next.id
    if (sid && !['idle', 'done', 'error'].includes(session.status)) catchUpAndPoll(sid, cursor)
  }
  tabs.splice(idx, 1); syncUrl()
}

async function recoverOrphanSessions() {
  const r = await api<{ sessions: Array<{ session_id: string; task: string; status: string; project_path: string; round: number; max_rounds: number }> }>('GET', '/api/sessions')
  const knownSids = new Set(tabs.map(t => t.sid).filter(Boolean))
  for (const o of (r.sessions ?? []).filter(s => !knownSids.has(s.session_id) && !['idle', 'done', 'error'].includes(s.status))) {
    const id = `tab-${++tabSeq}`
    tabs.push({ id, label: o.task.slice(0, 20) || '恢复会话', sid: o.session_id, cursor: 0, snapshot: createEmptyState(),
      projectPath: o.project_path, taskValue: o.task, roundsValue: o.max_rounds, extraRounds: 3, injectValue: '' })
  }
}

async function initFromUrl() {
  await initRoleConfig()
  const p = new URLSearchParams(location.search)
  const urlProject = p.get('project'); const urlSid = p.get('sid')
  const id = `tab-${++tabSeq}`; const snapshot = createEmptyState()
  snapshot.sessionRoleConfig = $state.snapshot(defaultRoleConfig)
  tabs.push({ id, label: '新会话', sid: urlSid, cursor: 0, snapshot,
    projectPath: urlProject ?? '', taskValue: '', roundsValue: 5, extraRounds: 3, injectValue: '' })
  activeTabId = id; if (urlProject) projectPath = urlProject
  if (urlSid) {
    sid = urlSid; const appState = getAppState()
    cursor = await hydrateSession(urlSid, appState, toolMap, roleFallback); applyAppState(appState)
    tabs[0].label = session.status !== 'idle' ? `会话 ${urlSid.slice(0, 4)}` : '新会话'
    startPolling()
  }
  await recoverOrphanSessions(); return { urlProject }
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

// Archived sessions
async function loadArchivedSessions(limit = 50, offset = 0) {
  return api<{ sessions: ArchivedSession[] }>('GET', `/api/archived_sessions?limit=${limit}&offset=${offset}`)
}
async function loadArchivedHistory(sessionId: string) {
  return api<ArchivedHistoryResponse>('GET', `/api/archived_session_history?sid=${sessionId}`)
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
  get defaultRoleConfig() { return defaultRoleConfig },
  get sessionRoleConfig() { return sessionRoleConfig },
  get toolMap() { return toolMap },
  get toolDisplayNames() { return toolDisplayNames },
  get execNote() { return execNote },
  get theme() { return theme }, toggleTheme,
  get sid() { return sid },
  get injectValue() { return injectValue },
  set injectValue(v: string) { injectValue = v },
  get canStart() { return canStart },
  get canStop() { return canStop },
  get canExecute() { return canExecute },
  get canContinue() { return canContinue },
  get canFix() { return canFix },
  get canReviewContinue() { return canReviewContinue },
  get canInject() { return canInject },
  get projectPath() { return projectPath }, set projectPath(v: string) { projectPath = v },
  get taskValue() { return taskValue }, set taskValue(v: string) { taskValue = v },
  get roundsValue() { return roundsValue }, set roundsValue(v: number) { roundsValue = v },
  get extraRounds() { return extraRounds }, set extraRounds(v: number) { extraRounds = v },
  get tabs() { return tabs }, get activeTabId() { return activeTabId },
  doStart, doStop, doExec, doContinue, doInject, doReviewFix, doReviewSkip, doReviewContinue,
  initFromUrl, onRoleChange, selectVersion, switchTab,
  loadPrompts, savePrompts, completePath, recentPaths, browseDir,
  loadArchivedSessions, loadArchivedHistory,
  t, tPhase, getLocale, switchLocale,
  newTab, switchToTab, closeTab,
}
