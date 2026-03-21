import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createEmptyState, resolveDisplayNames } from '../lib/types.js'
import type { AppState, HistoryResponse, SessionState, ToolInfo } from '../lib/types.js'
import { hydrateReviewContext, hydrateSession } from '../lib/hydrator.js'

vi.mock('../lib/api.js', () => ({ api: vi.fn() }))
import { api } from '../lib/api.js'
const mockApi = vi.mocked(api)

const mockToolMap: Record<string, ToolInfo> = {
  'claude-code': { id: 'claude-code', display_name: 'Claude', agent_name: 'claude', detected_installed: true, executable_path: null, version: null, probe_error: null, last_checked_at: null, capabilities: {} },
  'codex': { id: 'codex', display_name: 'Codex', agent_name: 'codex', detected_installed: true, executable_path: null, version: null, probe_error: null, last_checked_at: null, capabilities: {} },
  'my-tool': { id: 'my-tool', display_name: 'MyTool', agent_name: 'mytool', detected_installed: true, executable_path: null, version: null, probe_error: null, last_checked_at: null, capabilities: {} },
}

function makeHistory(overrides: Partial<HistoryResponse> = {}): HistoryResponse {
  return { entries: [], execution_result: null, review_entries: [], review_round: 0, review_status: null, event_cursor: 0, ...overrides }
}

function makeState(overrides: Partial<SessionState> = {}): SessionState {
  return {
    status: 'running', round: 3, max_rounds: 5, consensus: false, consensus_round: 0,
    history_len: 6, error: null, planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
    executor_panel: 'planner', review_round: 0, max_review_rounds: 3, ...overrides,
  }
}

describe('hydrateSession', () => {
  let state: AppState
  beforeEach(() => { state = createEmptyState(); mockApi.mockReset() })

  it('restores session state from /api/state', async () => {
    mockApi.mockResolvedValueOnce(makeState({ status: 'consensus', round: 3, planner_tool_id: 'my-tool' }))
    mockApi.mockResolvedValueOnce(makeHistory())
    await hydrateSession('abc', state, mockToolMap)
    expect(state.session.status).toBe('consensus')
    expect(state.session.round).toBe(3)
    expect(state.sessionRoleConfig.planner_tool_id).toBe('my-tool')
  })

  it('rebuilds versions from history entries', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({
      entries: [
        { round: 1, role: 'planner', phase: '提议', content: 'plan v1' },
        { round: 1, role: 'reviewer', phase: '审查', content: 'review v1' },
        { round: 2, role: 'planner', phase: '提议', content: 'plan v2' },
      ],
    }))
    await hydrateSession('abc', state, mockToolMap)
    expect(state.versions.planner).toHaveLength(2)
    expect(state.versions.reviewer).toHaveLength(1)
  })

  it('restores execution result', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ execution_result: 'success output' }))
    await hydrateSession('abc', state, mockToolMap)
    expect(state.executionResult).toBe('success output')
    expect(state.showExecResult).toBe(true)
  })

  it('restores executor_panel', async () => {
    mockApi.mockResolvedValueOnce(makeState({ executor_panel: 'reviewer' }))
    mockApi.mockResolvedValueOnce(makeHistory())
    await hydrateSession('abc', state, mockToolMap)
    expect(state.executorPanel).toBe('reviewer')
  })

  it('returns event_cursor', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ event_cursor: 42 }))
    expect(await hydrateSession('abc', state, mockToolMap)).toBe(42)
  })

  it('returns 0 when event_cursor is 0', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ event_cursor: 0 }))
    expect(await hydrateSession('abc', state, mockToolMap)).toBe(0)
  })

  it('skips user entries', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({
      entries: [
        { round: 1, role: 'user', phase: '人工干预', content: 'feedback' },
        { round: 1, role: 'planner', phase: '提议', content: 'plan' },
      ],
    }))
    await hydrateSession('abc', state, mockToolMap)
    expect(state.versions.planner).toHaveLength(1)
  })

  it('calls review hydration when review_status present', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ review_status: { status: 'review_fix', round: 1 } }))
    await hydrateSession('abc', state, mockToolMap)
    expect(state.logs.planner.length).toBeGreaterThan(0)
  })
})

describe('hydrateReviewContext', () => {
  let state: AppState
  const names = resolveDisplayNames({ planner_tool_id: 'claude-code', reviewer_tool_id: 'codex' }, mockToolMap)
  beforeEach(() => { state = createEmptyState() })

  it('appends review_start separator to both panels', () => {
    hydrateReviewContext(makeHistory({ review_status: { status: 'review_pending', round: 1 } }), state, names)
    expect(state.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'ok', text: expect.stringContaining('执行后审查开始') })
    expect(state.logs.reviewer[0]).toMatchObject({ kind: 'separator', level: 'ok' })
  })

  it('restores review entries with cross-panel collapsibles', () => {
    hydrateReviewContext(makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [{ round: 1, role: 'reviewer', phase: '审查', content: 'review text' }],
    }), state, names)
    expect(state.logs.reviewer.find(l => l.kind === 'collapsible')).toMatchObject({
      label: 'Codex 审查意见', content: 'review text', open: false,
    })
    expect(state.logs.planner.find(l => l.kind === 'collapsible')).toMatchObject({
      label: expect.stringContaining('查看'), open: false,
    })
  })

  it('restores planner fix entries', () => {
    hydrateReviewContext(makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [{ round: 1, role: 'planner', phase: '修复', content: 'fix summary' }],
    }), state, names)
    expect(state.logs.planner.find(l => l.kind === 'collapsible')).toMatchObject({ label: 'Claude 修复总结' })
  })

  it('review_status=done success sets doneBadge', () => {
    hydrateReviewContext(makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [{ round: 1, role: 'reviewer', phase: '审查', content: '任务收口成功\n详细说明' }],
    }), state, names)
    expect(state.doneBadge).toBe('✓ 收口成功')
  })

  it('review_status=done failure sets doneBadge', () => {
    hydrateReviewContext(makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [{ round: 1, role: 'reviewer', phase: '审查', content: '还有问题' }],
    }), state, names)
    expect(state.doneBadge).toBe('⚠ 审查完成')
  })

  it('review_status=review_fix shows waiting message', () => {
    hydrateReviewContext(makeHistory({ review_status: { status: 'review_fix', round: 1 } }), state, names)
    const log = state.logs.planner.find(l => l.kind === 'separator' && 'text' in l && l.text.includes('Codex'))
    expect(log).toBeTruthy()
    expect((log as { text: string }).text).toContain('等待你确认是否修复')
  })

  it('review_status=review_pending shows reviewing message', () => {
    hydrateReviewContext(makeHistory({ review_status: { status: 'review_pending', round: 1 } }), state, names)
    const log = state.logs.reviewer.find(l => l.kind === 'separator' && 'text' in l && l.text.includes('Codex'))
    expect(log).toBeTruthy()
  })

  it('no review_status does not crash', () => {
    hydrateReviewContext(makeHistory({ review_status: null }), state, names)
    expect(state.logs.planner).toHaveLength(1)
  })

  it('multiple review entries are restored in order', () => {
    hydrateReviewContext(makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'reviewer', phase: '审查', content: 'r1' },
        { round: 1, role: 'planner', phase: '修复', content: 'f1' },
        { round: 2, role: 'reviewer', phase: '审查', content: '任务收口成功' },
      ],
    }), state, names)
    expect(state.logs.planner.filter(l => l.kind === 'separator').length).toBeGreaterThanOrEqual(3)
    expect(state.doneBadge).toBe('✓ 收口成功')
  })
})
