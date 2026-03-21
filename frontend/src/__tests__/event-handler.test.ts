import { describe, it, expect } from 'vitest'
import { handleEvent } from '../lib/event-handler.js'
import { createEmptyState } from '../lib/types.js'
import type { BridgeEvent } from '../lib/types.js'

function handle(e: BridgeEvent) {
  const state = createEmptyState()
  handleEvent(e, state)
  return state
}

describe('handleEvent', () => {
  it('round_start appends separator to both panels', () => {
    const s = handle({ type: 'round_start', data: { round: 1, max: 5 } })
    expect(s.logs.planner).toHaveLength(1)
    expect(s.logs.reviewer).toHaveLength(1)
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'sys' })
    expect((s.logs.planner[0] as { text: string }).text).toContain('第 1 / 5 轮')
  })

  it('agent_thinking switches tab to log', () => {
    const s = createEmptyState()
    s.activeTab.planner = 'result'
    handleEvent({ type: 'agent_thinking', data: { agent: 'planner', round: 1 } }, s)
    expect(s.activeTab.planner).toBe('log')
    expect(s.logs.planner).toHaveLength(1)
    expect((s.logs.planner[0] as { text: string }).text).toContain('处理中')
  })

  it('agent_chunk text appends to correct panel', () => {
    const s = handle({ type: 'agent_chunk', data: { agent: 'planner', text: 'hello' } })
    expect(s.logs.planner).toHaveLength(1)
    expect(s.logs.planner[0]).toMatchObject({ kind: 'text', text: 'hello' })
    expect(s.logs.reviewer).toHaveLength(0)
  })

  it('agent_chunk command creates command log entry', () => {
    const s = handle({ type: 'agent_chunk', data: { agent: 'reviewer', text: 'ls -la', chunk_type: 'command' } })
    expect(s.logs.reviewer[0]).toMatchObject({ kind: 'command', text: 'ls -la' })
  })

  it('agent_chunk command_output creates fold', () => {
    const s = createEmptyState()
    handleEvent({ type: 'agent_chunk', data: { agent: 'planner', text: 'line1', chunk_type: 'command_output' } }, s)
    expect(s.activeFold.planner).toBeTruthy()
    expect(s.logs.planner).toHaveLength(2) // fold_start + fold_chunk
    expect(s.logs.planner[0]).toMatchObject({ kind: 'fold_start' })
    expect(s.logs.planner[1]).toMatchObject({ kind: 'fold_chunk', text: 'line1' })

    // Second chunk reuses same fold
    handleEvent({ type: 'agent_chunk', data: { agent: 'planner', text: 'line2', chunk_type: 'command_output' } }, s)
    expect(s.logs.planner).toHaveLength(3)
    expect(s.logs.planner[2]).toMatchObject({ kind: 'fold_chunk', text: 'line2' })
  })

  it('chunk_boundary closes fold', () => {
    const s = createEmptyState()
    s.activeFold.planner = 'fold_1'
    handleEvent({ type: 'chunk_boundary', data: { agent: 'planner', boundary_type: 'end' } }, s)
    expect(s.activeFold.planner).toBeNull()
    expect(s.logs.planner[0]).toMatchObject({ kind: 'fold_end' })
  })

  it('agent_stderr mcp creates mcp log entry', () => {
    const s = handle({ type: 'agent_stderr', data: { agent: 'planner', text: 'mcp info', is_mcp: true } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'mcp', text: 'mcp info' })
  })

  it('agent_stderr non-mcp is ignored', () => {
    const s = handle({ type: 'agent_stderr', data: { agent: 'planner', text: 'debug', is_mcp: false } })
    expect(s.logs.planner).toHaveLength(0)
  })

  it('agent_result is a no-op', () => {
    const s = handle({ type: 'agent_result', data: { agent: 'planner', text: 'result' } })
    expect(s.logs.planner).toHaveLength(0)
  })

  it('agent_response planner creates version + switches tab + notifies reviewer', () => {
    const s = handle({
      type: 'agent_response',
      data: { round: 1, role: 'planner', phase: '提议', content: 'plan content' },
    })
    expect(s.versions.planner).toHaveLength(1)
    expect(s.versions.planner[0]).toMatchObject({ round: 1, phase: '提议', content: 'plan content' })
    expect(s.activeTab.planner).toBe('result')
    expect(s.activeVer.planner).toBe(-1)
    expect(s.showExecResult).toBe(false)
    // Reviewer gets notification
    expect(s.logs.reviewer.some(l => l.kind === 'collapsible')).toBe(true)
  })

  it('agent_response user appends to both panels', () => {
    const s = handle({
      type: 'agent_response',
      data: { round: 1, role: 'user', phase: '人工干预', content: 'user feedback' },
    })
    expect(s.logs.planner).toHaveLength(1)
    expect(s.logs.reviewer).toHaveLength(1)
    expect((s.logs.planner[0] as { text: string }).text).toContain('你')
  })

  it('agent_response deduplicates versions by round', () => {
    const s = createEmptyState()
    const e: BridgeEvent = {
      type: 'agent_response',
      data: { round: 1, role: 'planner', phase: '提议', content: 'v1' },
    }
    handleEvent(e, s)
    handleEvent(e, s)
    expect(s.versions.planner).toHaveLength(1)
  })

  it('consensus_reached appends ok separator', () => {
    const s = handle({ type: 'consensus_reached', data: { round: 3, msg: '达成共识' } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'ok' })
    expect(s.logs.reviewer[0]).toMatchObject({ kind: 'separator', level: 'ok' })
  })

  it('max_rounds_reached appends sys separator', () => {
    const s = handle({ type: 'max_rounds_reached', data: { round: 5, msg: '达到上限' } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'sys' })
  })

  it('execution_done sets executorPanel and result', () => {
    const s = handle({ type: 'execution_done', data: { result: 'success output', executor_panel: 'reviewer' } })
    expect(s.executorPanel).toBe('reviewer')
    expect(s.executionResult).toBe('success output')
    expect(s.showExecResult).toBe(true)
    expect(s.doneBadge).toBe('✓ 执行完毕')
    expect(s.logs.reviewer).toHaveLength(1)
  })

  it('error appends err separator to both', () => {
    const s = handle({ type: 'error', data: { msg: 'something broke' } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'err' })
    expect((s.logs.planner[0] as { text: string }).text).toContain('something broke')
  })

  it('warning appends only to planner', () => {
    const s = handle({ type: 'warning', data: { msg: 'watch out' } })
    expect(s.logs.planner).toHaveLength(1)
    expect(s.logs.reviewer).toHaveLength(0)
  })

  it('rollback truncates versions and resets activeVer', () => {
    const s = createEmptyState()
    s.versions.planner = [
      { round: 1, phase: '提议', content: 'v1' },
      { round: 2, phase: '提议', content: 'v2' },
      { round: 3, phase: '提议', content: 'v3' },
    ]
    s.versions.reviewer = [
      { round: 1, phase: '审查', content: 'r1' },
      { round: 2, phase: '审查', content: 'r2' },
    ]
    s.activeVer.planner = 2
    handleEvent({ type: 'rollback', data: { round: 1, max: 5, plan: '', msg: 'rolled back' } }, s)
    expect(s.versions.planner).toHaveLength(1)
    expect(s.versions.reviewer).toHaveLength(1)
    expect(s.activeVer.planner).toBe(-1)
    expect(s.activeVer.reviewer).toBe(-1)
  })

  it('status_change stopped appends to both', () => {
    const s = handle({ type: 'status_change', data: { status: 'stopped', msg: '' } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', text: '⏹ 已中止' })
  })

  it('status_change non-stopped is no-op', () => {
    const s = handle({ type: 'status_change', data: { status: 'running', msg: '' } })
    expect(s.logs.planner).toHaveLength(0)
  })

  it('review_response reviewer creates collapsibles on both panels', () => {
    const s = handle({
      type: 'review_response',
      data: { round: 1, role: 'reviewer', phase: '审查', content: 'review content' },
    })
    expect(s.logs.reviewer.some(l => l.kind === 'collapsible' && l.open === true)).toBe(true)
    expect(s.logs.planner.some(l => l.kind === 'collapsible' && l.open === false)).toBe(true)
  })

  it('review_response planner creates collapsibles on both panels', () => {
    const s = handle({
      type: 'review_response',
      data: { round: 1, role: 'planner', phase: '修复', content: 'fix summary' },
    })
    expect(s.logs.planner.some(l => l.kind === 'collapsible' && l.open === true)).toBe(true)
    expect(s.logs.reviewer.some(l => l.kind === 'collapsible' && l.open === false)).toBe(true)
  })

  it('review_start appends ok separator to both', () => {
    const s = handle({ type: 'review_start', data: { round: 1, max: 3 } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'ok' })
    expect((s.logs.planner[0] as { text: string }).text).toContain('执行后审查开始')
  })

  it('review_round_start appends round info', () => {
    const s = handle({ type: 'review_round_start', data: { round: 2, max: 3 } })
    expect((s.logs.planner[0] as { text: string }).text).toContain('审查修复轮 2 / 3')
  })

  it('review_needs_fix appends warning', () => {
    const s = handle({ type: 'review_needs_fix', data: { round: 1, msg: 'needs fix', review: 'details' } })
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'sys' })
  })

  it('review_done success=true sets doneBadge', () => {
    const s = handle({ type: 'review_done', data: { round: 1, msg: 'done', success: true } })
    expect(s.doneBadge).toBe('✓ 收口成功')
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'ok' })
  })

  it('review_done success=false sets doneBadge', () => {
    const s = handle({ type: 'review_done', data: { round: 1, msg: 'partial', success: false } })
    expect(s.doneBadge).toBe('⚠ 审查完成')
    expect(s.logs.planner[0]).toMatchObject({ kind: 'separator', level: 'sys' })
  })

  it('cli_start is a no-op', () => {
    const s = handle({ type: 'cli_start', data: { agent: 'planner', round: 1 } })
    expect(s.logs.planner).toHaveLength(0)
  })
})
