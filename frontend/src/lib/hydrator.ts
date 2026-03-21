// Explicit hydration pipeline — translates server.py L1366-1444
// Precondition: initRoleConfig() completed (toolMap populated)
// Input: API responses → Output: store state mutations

import { api } from './api.js'
import { isClosureText } from './protocol.js'
import { resolveDisplayNames } from './types.js'
import type {
  AppState, AgentPanel, SessionState, HistoryResponse,
  LogEntry, ReviewEntry, DisplayNames, ToolInfo,
} from './types.js'

function pushLog(state: AppState, agent: AgentPanel, entry: LogEntry): void {
  state.logs[agent].push(entry)
}

function pushBothLogs(state: AppState, entry: LogEntry): void {
  state.logs.planner.push(entry)
  state.logs.reviewer.push(entry)
}

export async function hydrateSession(sid: string, state: AppState, toolMap: Record<string, ToolInfo>, fallback?: (role: AgentPanel) => string): Promise<number> {
  const s = await api<SessionState>('GET', `/api/state?sid=${sid}`)
  state.session = s
  if (s.planner_tool_id) {
    state.sessionRoleConfig = { planner_tool_id: s.planner_tool_id, reviewer_tool_id: s.reviewer_tool_id }
    state.executorPanel = s.executor_panel ?? 'planner'
  }

  const names = resolveDisplayNames(state.sessionRoleConfig, toolMap, fallback)

  const h = await api<HistoryResponse>('GET', `/api/history?sid=${sid}`)
  for (const entry of h.entries ?? []) {
    if (entry.role === 'user') continue
    const agent = entry.role as AgentPanel
    if (!state.versions[agent].some(v => v.round === entry.round)) {
      state.versions[agent].push({ round: entry.round, phase: entry.phase, content: entry.content })
    }
  }
  for (const agent of ['planner', 'reviewer'] as const) {
    if (state.versions[agent].length) state.activeVer[agent] = -1
  }

  if (h.execution_result != null) {
    state.executionResult = h.execution_result
    state.showExecResult = true
  }

  if (h.review_status) {
    hydrateReviewContext(h, state, names)
  }

  return h.event_cursor ?? 0
}

export function hydrateReviewContext(history: HistoryResponse, state: AppState, names: DisplayNames): void {
  pushBothLogs(state, { kind: 'separator', level: 'ok', text: '══════ 执行后审查开始 ══════' })

  for (const h of history.review_entries ?? []) {
    pushLog(state, h.role, {
      kind: 'separator', level: 'sys',
      text: `── ${h.phase} (审查轮 ${h.round}) ──`,
    })
    if (h.content) {
      const label = h.role === 'planner'
        ? `${names.planner} 修复总结`
        : `${names.reviewer} 审查意见`
      pushLog(state, h.role, { kind: 'collapsible', label, content: h.content, open: false })
      const other: AgentPanel = h.role === 'planner' ? 'reviewer' : 'planner'
      pushLog(state, other, { kind: 'collapsible', label: `查看${label}`, content: h.content, open: false })
    }
  }

  const rs = history.review_status
  if (!rs) return

  if (rs.status === 'done') {
    const lastReviewer = (history.review_entries ?? [])
      .filter((e: ReviewEntry) => e.role === 'reviewer').pop()
    const success = isClosureText(lastReviewer?.content ?? '')
    const msg = success ? '══════ 任务收口成功 ══════' : '⚠ 审查完成（未达成收口确认）'
    pushBothLogs(state, { kind: 'separator', level: success ? 'ok' : 'sys', text: msg })
    state.doneBadge = success ? '✓ 收口成功' : '⚠ 审查完成'
  } else if (rs.status === 'review_fix') {
    pushBothLogs(state, {
      kind: 'separator', level: 'sys',
      text: `⚠ ${names.reviewer} 发现问题，等待你确认是否修复。`,
    })
  } else if (rs.status === 'review_max_rounds') {
    pushBothLogs(state, {
      kind: 'separator', level: 'sys',
      text: `⚠ 已达最大审查轮次，等待你选择继续审查或跳过。`,
    })
  } else if (rs.status === 'review_pending') {
    pushLog(state, 'reviewer', {
      kind: 'separator', level: 'sys',
      text: `[${names.reviewer} 评审中...]`,
    })
  }
}
