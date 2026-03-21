import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.stubGlobal('location', { href: 'http://localhost/', search: '' })
vi.stubGlobal('history', { replaceState: vi.fn() })

vi.mock('../lib/api.js', () => ({ api: vi.fn() }))
vi.mock('../lib/dialog.svelte.js', () => {
  const showConfirm = vi.fn<(msg: string) => Promise<boolean>>()
  const showAlert = vi.fn<(msg: string) => Promise<void>>().mockResolvedValue(undefined)
  return { showConfirm, showAlert, dialog: { open: false, message: '', mode: 'confirm' as const, resolve: vi.fn() } }
})

import { api } from '../lib/api.js'
import { showConfirm, showAlert } from '../lib/dialog.svelte.js'
import { store } from '../lib/store.svelte.js'

const mockApi = vi.mocked(api)
const mockShowConfirm = vi.mocked(showConfirm)
const mockShowAlert = vi.mocked(showAlert)

async function setupSession(status: string = 'consensus') {
  mockApi.mockResolvedValueOnce({ session_id: 'test-sid' })
  await store.doStart('/tmp', 'test task', 5)
  mockApi
    .mockResolvedValueOnce({ events: [], next: 0 })
    .mockResolvedValueOnce({
      status, round: 3, max_rounds: 5,
      consensus: status === 'consensus', consensus_round: status === 'consensus' ? 3 : 0,
      history_len: 6, error: null,
      planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
      executor_panel: 'planner',
    })
  await vi.advanceTimersByTimeAsync(350)
}

describe('doExec confirm flow', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers() })

  it('confirm=true → calls /api/execute', async () => {
    await setupSession('consensus')
    mockShowConfirm.mockResolvedValue(true)
    mockApi.mockResolvedValueOnce({})
    await store.doExec()
    expect(mockShowConfirm).toHaveBeenCalled()
    expect(mockApi).toHaveBeenCalledWith('POST', '/api/execute', expect.objectContaining({ session_id: 'test-sid' }))
  })

  it('confirm=false → does NOT call /api/execute', async () => {
    await setupSession('consensus')
    mockShowConfirm.mockResolvedValue(false)
    await store.doExec()
    expect(mockApi.mock.calls.filter(c => c[1] === '/api/execute')).toHaveLength(0)
  })

  it('exec error → shows alert', async () => {
    await setupSession('consensus')
    mockShowConfirm.mockResolvedValue(true)
    mockApi.mockResolvedValueOnce({ error: 'exec failed' })
    await store.doExec()
    expect(mockShowAlert).toHaveBeenCalled()
  })

  it('sid changes during confirm → does NOT call api', async () => {
    await setupSession('consensus')
    mockShowConfirm.mockImplementation(async () => {
      mockApi.mockResolvedValueOnce({ session_id: 'other-sid' })
      await store.doStart('/tmp', 'other task', 3)
      mockApi
        .mockResolvedValueOnce({ events: [], next: 0 })
        .mockResolvedValueOnce({
          status: 'consensus', round: 1, max_rounds: 3,
          consensus: true, consensus_round: 1,
          history_len: 2, error: null,
          planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
          executor_panel: 'planner',
        })
      await vi.advanceTimersByTimeAsync(350)
      return true
    })
    await store.doExec()
    expect(mockApi.mock.calls.filter(c => c[1] === '/api/execute')).toHaveLength(0)
  })
})

describe('doReviewFix confirm flow', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers() })

  it('confirm=true → calls /api/review_fix', async () => {
    await setupSession('review_fix')
    mockShowConfirm.mockResolvedValue(true)
    mockApi.mockResolvedValueOnce({})
    await store.doReviewFix()
    expect(mockApi).toHaveBeenCalledWith('POST', '/api/review_fix', expect.objectContaining({ session_id: 'test-sid' }))
  })

  it('confirm=false → does NOT call api', async () => {
    await setupSession('review_fix')
    mockShowConfirm.mockResolvedValue(false)
    await store.doReviewFix()
    expect(mockApi.mock.calls.filter(c => c[1] === '/api/review_fix')).toHaveLength(0)
  })

  it('review_fix error → shows alert', async () => {
    await setupSession('review_fix')
    mockShowConfirm.mockResolvedValue(true)
    mockApi.mockResolvedValueOnce({ error: 'not fixable' })
    await store.doReviewFix()
    expect(mockShowAlert).toHaveBeenCalled()
  })
})

describe('showAlert paths', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers() })

  it('doInject in consensus → shows alert, no api call', async () => {
    await setupSession('consensus')
    store.injectValue = 'test'
    await store.doInject()
    expect(mockShowAlert).toHaveBeenCalled()
    expect(mockApi.mock.calls.filter(c => c[1] === '/api/inject')).toHaveLength(0)
  })
})
