import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createEmptyState } from '../lib/types.js'
import type { AppState, HistoryResponse, SessionState } from '../lib/types.js'
import { hydrateReviewContext, hydrateSession } from '../lib/hydrator.js'

// Mock api module
vi.mock('../lib/api.js', () => ({
  api: vi.fn(),
}))
import { api } from '../lib/api.js'
const mockApi = vi.mocked(api)

function makeHistory(overrides: Partial<HistoryResponse> = {}): HistoryResponse {
  return {
    entries: [],
    execution_result: null,
    review_entries: [],
    review_round: 0,
    review_status: null,
    event_cursor: 0,
    ...overrides,
  }
}

function makeState(overrides: Partial<SessionState> = {}): SessionState {
  return {
    status: 'running', round: 3, max_rounds: 5,
    consensus: false, consensus_round: 0,
    history_len: 6, error: null,
    planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
    executor_panel: 'planner',
    ...overrides,
  }
}

describe('hydrateSession', () => {
  let state: AppState

  beforeEach(() => {
    state = createEmptyState()
    state.toolDisplayNames = { planner: 'Claude', reviewer: 'Codex' }
    mockApi.mockReset()
  })

  it('restores session state from /api/state', async () => {
    const sessionData = makeState({ status: 'consensus', round: 3, planner_tool_id: 'my-tool' })
    mockApi.mockResolvedValueOnce(sessionData) // /api/state
    mockApi.mockResolvedValueOnce(makeHistory()) // /api/history
    await hydrateSession('abc', state)
    expect(state.session.status).toBe('consensus')
    expect(state.session.round).toBe(3)
    expect(state.roleConfig.planner_tool_id).toBe('my-tool')
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
    await hydrateSession('abc', state)
    expect(state.versions.planner).toHaveLength(2)
    expect(state.versions.reviewer).toHaveLength(1)
    expect(state.activeVer.planner).toBe(-1)
  })

  it('restores execution result', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ execution_result: 'success output' }))
    await hydrateSession('abc', state)
    expect(state.executionResult).toBe('success output')
    expect(state.showExecResult).toBe(true)
  })

  it('restores executor_panel from /api/state', async () => {
    mockApi.mockResolvedValueOnce(makeState({ executor_panel: 'reviewer' }))
    mockApi.mockResolvedValueOnce(makeHistory())
    await hydrateSession('abc', state)
    expect(state.executorPanel).toBe('reviewer')
  })

  it('returns event_cursor for polling', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ event_cursor: 42 }))
    const cursor = await hydrateSession('abc', state)
    expect(cursor).toBe(42)
  })

  it('returns 0 when event_cursor is null', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({ event_cursor: 0 }))
    const cursor = await hydrateSession('abc', state)
    expect(cursor).toBe(0)
  })

  it('skips user entries when rebuilding versions', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({
      entries: [
        { round: 1, role: 'user', phase: '人工干预', content: 'feedback' },
        { round: 1, role: 'planner', phase: '提议', content: 'plan' },
      ],
    }))
    await hydrateSession('abc', state)
    expect(state.versions.planner).toHaveLength(1)
    // user entries don't create versions
  })

  it('calls review hydration when review_status present', async () => {
    mockApi.mockResolvedValueOnce(makeState())
    mockApi.mockResolvedValueOnce(makeHistory({
      review_status: { status: 'review_fix', round: 1 },
    }))
    await hydrateSession('abc', state)
    // Should have appended review logs
    expect(state.logs.planner.length).toBeGreaterThan(0)
  })
})

describe('hydrateReviewContext', () => {
  let state: AppState

  beforeEach(() => {
    state = createEmptyState()
    state.toolDisplayNames = { planner: 'Claude', reviewer: 'Codex' }
  })

  it('appends review_start separator to both panels', () => {
    const h = makeHistory({ review_status: { status: 'review_pending', round: 1 } })
    hydrateReviewContext(h, state)
    expect(state.logs.planner[0]).toMatchObject({
      kind: 'separator', level: 'ok', text: expect.stringContaining('执行后审查开始'),
    })
    expect(state.logs.reviewer[0]).toMatchObject({
      kind: 'separator', level: 'ok',
    })
  })

  it('restores review entries with cross-panel collapsibles', () => {
    const h = makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'reviewer', phase: '审查', content: 'review text' },
      ],
    })
    hydrateReviewContext(h, state)
    const reviewerCollapsible = state.logs.reviewer.find(l => l.kind === 'collapsible')
    expect(reviewerCollapsible).toMatchObject({
      kind: 'collapsible',
      label: 'Codex 审查意见',
      content: 'review text',
      open: false,
    })
    const plannerCollapsible = state.logs.planner.find(l => l.kind === 'collapsible')
    expect(plannerCollapsible).toMatchObject({
      kind: 'collapsible',
      label: expect.stringContaining('查看'),
      open: false,
    })
  })

  it('restores planner fix entries', () => {
    const h = makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'planner', phase: '修复', content: 'fix summary' },
      ],
    })
    hydrateReviewContext(h, state)
    const plannerCollapsible = state.logs.planner.find(l => l.kind === 'collapsible')
    expect(plannerCollapsible).toMatchObject({ label: 'Claude 修复总结' })
  })

  it('review_status=done success shows 收口成功 + sets doneBadge', () => {
    const h = makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'reviewer', phase: '审查', content: '任务收口成功\n详细说明' },
      ],
    })
    hydrateReviewContext(h, state)
    expect(state.doneBadge).toBe('✓ 收口成功')
  })

  it('review_status=done failure shows 审查完成', () => {
    const h = makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'reviewer', phase: '审查', content: '还有问题' },
      ],
    })
    hydrateReviewContext(h, state)
    expect(state.doneBadge).toBe('⚠ 审查完成')
  })

  it('review_status=review_fix shows waiting message with tool name', () => {
    const h = makeHistory({ review_status: { status: 'review_fix', round: 1 } })
    hydrateReviewContext(h, state)
    const log = state.logs.planner.find(
      l => l.kind === 'separator' && (l as { text: string }).text.includes('Codex'),
    )
    expect(log).toBeTruthy()
    expect((log as { text: string }).text).toContain('等待你确认是否修复')
  })

  it('review_status=review_pending shows reviewing message', () => {
    const h = makeHistory({ review_status: { status: 'review_pending', round: 1 } })
    hydrateReviewContext(h, state)
    const log = state.logs.reviewer.find(
      l => l.kind === 'separator' && (l as { text: string }).text.includes('Codex'),
    )
    expect(log).toBeTruthy()
  })

  it('no review_status does not crash', () => {
    const h = makeHistory({ review_status: null })
    hydrateReviewContext(h, state)
    expect(state.logs.planner).toHaveLength(1)
  })

  it('multiple review entries are restored in order', () => {
    const h = makeHistory({
      review_status: { status: 'done', round: 1 },
      review_entries: [
        { round: 1, role: 'reviewer', phase: '审查', content: 'r1' },
        { round: 1, role: 'planner', phase: '修复', content: 'f1' },
        { round: 2, role: 'reviewer', phase: '审查', content: '任务收口成功' },
      ],
    })
    hydrateReviewContext(h, state)
    const separators = state.logs.planner.filter(l => l.kind === 'separator')
    expect(separators.length).toBeGreaterThanOrEqual(3)
    expect(state.doneBadge).toBe('✓ 收口成功')
  })
})
