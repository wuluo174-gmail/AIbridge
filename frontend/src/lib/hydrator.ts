// Explicit hydration pipeline — translates server.py L1366-1444
// Precondition: initRoleConfig() completed (toolMap populated)
// Input: API responses → Output: store state mutations

import { api } from './api.js'
import type {
  AppState, AgentPanel, SessionState, HistoryResponse,
  LogEntry, ReviewEntry,
} from './types.js'

function pushLog(state: AppState, agent: AgentPanel, entry: LogEntry): void {
  state.logs[agent].push(entry)
}

function pushBothLogs(state: AppState, entry: LogEntry): void {
  state.logs.planner.push(entry)
  state.logs.reviewer.push(entry)
}

export async function hydrateSession(sid: string, state: AppState): Promise<number> {
  // Step 1: restore backend state snapshot
  const s = await api<SessionState>('GET', `/api/state?sid=${sid}`)
  state.session = s
  if (s.planner_tool_id) {
    state.roleConfig = { planner_tool_id: s.planner_tool_id, reviewer_tool_id: s.reviewer_tool_id }
    state.executorPanel = s.executor_panel ?? 'planner'
  }

  // Step 2: restore negotiation history → rebuild versions
  const h = await api<HistoryResponse>('GET', `/api/history?sid=${sid}`)
  for (const entry of h.entries ?? []) {
    const role = entry.role
    if (role === 'user') continue
    const agent = role as AgentPanel
    if (!state.versions[agent].some(v => v.round === entry.round)) {
      state.versions[agent].push({ round: entry.round, phase: entry.phase, content: entry.content })
    }
  }
  for (const agent of ['planner', 'reviewer'] as const) {
    if (state.versions[agent].length) {
      state.activeVer[agent] = -1
    }
  }

  // Step 3: restore execution result
  if (h.execution_result != null) {
    state.executionResult = h.execution_result
    state.showExecResult = true
  }

  // Step 4: restore review context
  if (h.review_status) {
    hydrateReviewContext(h, state)
  }

  // Step 5: return cursor to skip already-restored events
  return h.event_cursor ?? 0
}

export function hydrateReviewContext(history: HistoryResponse, state: AppState): void {
  pushBothLogs(state, { kind: 'separator', level: 'ok', text: '══════ 执行后审查开始 ══════' })

  for (const h of history.review_entries ?? []) {
    pushLog(state, h.role, {
      kind: 'separator', level: 'sys',
      text: `── ${h.phase} (审查轮 ${h.round}) ──`,
    })
    if (h.content) {
      const label = h.role === 'planner'
        ? `${state.toolDisplayNames.planner} 修复总结`
        : `${state.toolDisplayNames.reviewer} 审查意见`
      pushLog(state, h.role, { kind: 'collapsible', label, content: h.content, open: false })
      const other: AgentPanel = h.role === 'planner' ? 'reviewer' : 'planner'
      pushLog(state, other, { kind: 'collapsible', label: `查看${label}`, content: h.content, open: false })
    }
  }

  const rs = history.review_status
  if (!rs) return

  if (rs.status === 'done') {
    const lastReviewer = (history.review_entries ?? [])
      .filter((e: ReviewEntry) => e.role === 'reviewer')
      .pop()
    const success = lastReviewer?.content?.split('\n')[0]?.includes('任务收口成功') ?? false
    const msg = success ? '══════ 任务收口成功 ══════' : '⚠ 审查完成（未达成收口确认）'
    pushBothLogs(state, { kind: 'separator', level: success ? 'ok' : 'sys', text: msg })
    state.doneBadge = success ? '✓ 收口成功' : '⚠ 审查完成'
  } else if (rs.status === 'review_fix') {
    pushBothLogs(state, {
      kind: 'separator', level: 'sys',
      text: `⚠ ${state.toolDisplayNames.reviewer} 发现问题，等待你确认是否修复。`,
    })
  } else if (rs.status === 'review_pending') {
    pushLog(state, 'reviewer', {
      kind: 'separator', level: 'sys',
      text: `[${state.toolDisplayNames.reviewer} 评审中...]`,
    })
  }
}
