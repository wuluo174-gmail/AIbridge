import { describe, expect, it } from 'vitest'

import {
  artifactFromStreamEvent,
  appendSceneProjection,
  appendTerminalProjection,
  interventionFromStreamEvent,
  laneFromStreamEvent,
  sessionStateFromStreamEvent,
  sessionStateToSummary,
  sessionSummaryFromStreamEvent,
  type Artifact,
  type Intervention,
  type LaneInfo,
  type SessionState,
  type SessionSummary,
  type StreamEvent,
  upsertArtifact,
  upsertIntervention,
  upsertLaneInfo,
  upsertSessionSummary,
} from '../lib/workspace.js'

describe('workspace projections', () => {
  it('accumulates backend terminal deltas without local reconstruction helpers', () => {
    const events: StreamEvent[] = [
      {
        id: 0,
        type: 'session.status_changed',
        role_key: null,
        source: 'workflow',
        data: {
          message: '协商开始。',
          projection: { terminal: { planner: '[状态] 协商开始。', reviewer: '[状态] 协商开始。', executor: '[状态] 协商开始。', validator: '[状态] 协商开始。' } },
        },
        ts: '2026-03-22T10:00:00',
      },
      {
        id: 1,
        type: 'lane.stdout_chunk',
        role_key: 'planner',
        source: 'claude-code',
        data: { text: 'hello', projection: { terminal: { planner: 'hello' } } },
        ts: '2026-03-22T10:00:01',
      },
      {
        id: 2,
        type: 'session.view_mode_changed',
        role_key: null,
        source: 'workflow',
        data: {
          view_mode: 'terminal',
          message: '切换到 terminal 视图。',
          projection: { terminal: { planner: '[模式] 切换到 terminal 视图。', reviewer: '[模式] 切换到 terminal 视图。', executor: '[模式] 切换到 terminal 视图。', validator: '[模式] 切换到 terminal 视图。' } },
        },
        ts: '2026-03-22T10:00:02',
      },
      {
        id: 3,
        type: 'lane.result_emitted',
        role_key: 'planner',
        source: 'claude-code',
        data: { text: 'final', projection: { terminal: { planner: '[结果] final' } } },
        ts: '2026-03-22T10:00:03',
      },
    ]

    const output = events.reduce(
      (projection, event) => appendTerminalProjection(projection, event),
      { planner: '', reviewer: '', executor: '', validator: '' },
    ).planner

    expect(output).toContain('[状态] 协商开始。')
    expect(output).toContain('hello')
    expect(output).toContain('[模式] 切换到 terminal 视图。')
    expect(output).toContain('[结果] final')
  })

  it('prefers backend projection deltas over local inference', () => {
    const event: StreamEvent = {
      id: 1,
      type: 'lane.stdout_chunk',
      role_key: 'planner',
      source: 'claude-code',
      data: {
        text: 'hello',
        projection: {
          terminal: { planner: '[后端投影] hello' },
          scene: {
            id: 'event-1',
            created_at: '2026-03-22T10:00:01',
            type: 'event',
            role_key: 'planner',
            title: '规划者输出',
            content: 'hello',
            meta: '2026-03-22T10:00:01',
          },
        },
      },
      ts: '2026-03-22T10:00:01',
    }

    const terminal = appendTerminalProjection({ planner: '', reviewer: '', executor: '', validator: '' }, event)
    const scene = appendSceneProjection([], event)

    expect(terminal.planner).toBe('[后端投影] hello')
    expect(scene[0].title).toBe('规划者输出')
  })

  it('appends only backend high-signal scene deltas', () => {
    const events: StreamEvent[] = [
      {
        id: 2,
        type: 'session.stage_changed',
        role_key: null,
        source: 'workflow',
        data: {
          active_stage: 'planning',
          message: '第 1 轮：规划中。',
          projection: {
            scene: {
              id: 'event-2',
              created_at: '2026-03-22T10:00:01',
              type: 'event',
              role_key: null,
              title: '工作流阶段',
              content: 'planning 第 1 轮：规划中。',
              meta: '2026-03-22T10:00:01',
            },
          },
        },
        ts: '2026-03-22T10:00:01',
      },
      {
        id: 3,
        type: 'lane.command_started',
        role_key: 'executor',
        source: 'claude-code',
        data: {
          command: 'pytest -q',
          projection: {
            scene: {
              id: 'event-3',
              created_at: '2026-03-22T10:00:20',
              type: 'event',
              role_key: 'executor',
              title: '执行者 执行命令',
              content: 'pytest -q',
              meta: '2026-03-22T10:00:20',
            },
          },
        },
        ts: '2026-03-22T10:00:20',
      },
      {
        id: 4,
        type: 'lane.stdout_chunk',
        role_key: 'executor',
        source: 'claude-code',
        data: { text: 'low signal chunk' },
        ts: '2026-03-22T10:00:21',
      },
    ]

    const timeline = events.reduce((items, event) => appendSceneProjection(items, event), [] as ReturnType<typeof appendSceneProjection>)

    expect(timeline.map((item) => item.type)).toEqual(['event', 'event'])
    expect(timeline[0].title).toBe('工作流阶段')
    expect(timeline[1].title).toBe('执行者 执行命令')
    expect(timeline.some((item) => item.content.includes('low signal chunk'))).toBe(false)
  })

  it('extracts canonical live-state snapshots from stream events', () => {
    const sessionState: SessionState = {
      session_id: 'sess-1',
      task: 'task',
      project_path: '/tmp/project',
      workflow_template: 'standard',
      view_mode: 'terminal',
      status: 'running',
      active_stage: 'planning',
      current_round: 1,
      current_review_round: 0,
      consensus_round: 0,
      max_rounds: 5,
      max_review_rounds: 3,
      error: null,
      interrupt_reason: null,
      created_at: '2026-03-22T10:00:00',
      updated_at: '2026-03-22T10:00:02',
      finished_at: null,
      resume_available: false,
    }
    const sessionSummary: SessionSummary = sessionStateToSummary(sessionState)
    const lane: LaneInfo = {
      lane_id: 'lane-planner',
      role_key: 'planner',
      tool_id: 'claude-code',
      enabled: true,
      sort_order: 0,
      lane_status: 'busy',
      transport_kind: 'bridge-terminal',
      last_seq: 4,
      display_name: 'Claude Code',
    }
    const artifact: Artifact = {
      id: 'artifact-1',
      role_key: 'planner',
      round: 1,
      phase: 'planning',
      artifact_kind: 'plan',
      content: '# plan',
      created_at: '2026-03-22T10:00:03',
    }
    const intervention: Intervention = {
      id: 'intervention-1',
      origin_view: 'terminal',
      origin_role_key: 'planner',
      target_scope: 'planning',
      target_roles: ['planner', 'reviewer'],
      text: '补上恢复策略',
      command: null,
      status: 'queued',
      consumed_by_roles: {},
      created_at: '2026-03-22T10:00:04',
      updated_at: '2026-03-22T10:00:04',
    }

    const statusEvent: StreamEvent = {
      id: 5,
      type: 'session.status_changed',
      role_key: null,
      source: 'workflow',
      data: { session: sessionState, summary: sessionSummary, status: 'running', message: '协商开始。' },
      ts: '2026-03-22T10:00:02',
    }
    const laneEvent: StreamEvent = {
      id: 6,
      type: 'lane.status_changed',
      role_key: 'planner',
      source: 'workflow',
      data: { lane, status: 'busy', message: 'planner 通道忙碌中' },
      ts: '2026-03-22T10:00:02',
    }
    const artifactEvent: StreamEvent = {
      id: 7,
      type: 'artifact.published',
      role_key: 'planner',
      source: 'artifact',
      data: { artifact, artifact_kind: 'plan', round: 1 },
      ts: '2026-03-22T10:00:03',
    }
    const interventionEvent: StreamEvent = {
      id: 8,
      type: 'intervention.received',
      role_key: 'planner',
      source: 'intervention',
      data: { intervention },
      ts: '2026-03-22T10:00:04',
    }

    expect(sessionStateFromStreamEvent(statusEvent)?.session_id).toBe('sess-1')
    expect(sessionSummaryFromStreamEvent(statusEvent)?.status).toBe('running')
    expect(laneFromStreamEvent(laneEvent)?.lane_status).toBe('busy')
    expect(artifactFromStreamEvent(artifactEvent)?.id).toBe('artifact-1')
    expect(interventionFromStreamEvent(interventionEvent)?.id).toBe('intervention-1')
    expect(upsertLaneInfo([], lane)[0].lane_status).toBe('busy')
    expect(upsertArtifact([], artifact)[0].artifact_kind).toBe('plan')
    expect(upsertIntervention([], intervention)[0].target_scope).toBe('planning')
    expect(upsertSessionSummary([], sessionSummary)[0].session_id).toBe('sess-1')
  })
})
