export type ViewMode = 'terminal' | 'scene'
export type RoleKey = 'planner' | 'reviewer' | 'executor' | 'validator'

export interface Artifact {
  id: string
  role_key: RoleKey
  round: number
  phase: string
  artifact_kind: string
  content: string
  created_at: string
}

export interface Intervention {
  id: string
  origin_view: string
  origin_role_key: RoleKey | null
  target_scope: string
  target_roles: RoleKey[]
  text: string
  command: string | null
  status: string
  consumed_by_roles: Record<string, { round: number; ts: string }>
  created_at: string
  updated_at: string
}

export interface SessionState {
  session_id: string | null
  task: string
  project_path: string
  workflow_template: string
  view_mode: ViewMode
  status: string
  active_stage: string
  current_round: number
  current_review_round: number
  consensus_round: number
  max_rounds: number
  max_review_rounds: number
  error: string | null
  interrupt_reason: string | null
  created_at: string | null
  updated_at: string | null
  finished_at: string | null
  resume_available: boolean
}

export interface TerminalViewport {
  width_px: number
  height_px: number
  cols: number
  rows: number
  updated_at: string
}

export interface LaneInfo {
  lane_id: string
  role_key: RoleKey
  tool_id: string
  enabled: boolean
  sort_order: number
  lane_status: string
  transport_kind: string
  viewport?: TerminalViewport
  last_seq: number
  display_name?: string
}

export interface SessionSummary {
  session_id: string
  task: string
  project_path: string
  view_mode: ViewMode
  status: string
  active_stage: string
  current_round: number
  current_review_round: number
  max_rounds: number
  max_review_rounds: number
  updated_at: string
  created_at: string
  finished_at: string | null
  interrupt_reason: string | null
}

export interface StreamEvent {
  id: number
  type: string
  role_key: RoleKey | null
  source: string
  data: Record<string, unknown>
  ts: string
}

export interface SceneItem {
  id: string
  created_at: string
  type: 'artifact' | 'intervention' | 'event'
  role_key: RoleKey | null
  title: string
  content: string
  meta: string
}

export type TerminalProjection = Record<RoleKey, string>

export const ROLE_ORDER: RoleKey[] = ['planner', 'reviewer', 'executor', 'validator']

export const ROLE_TITLES: Record<RoleKey, string> = {
  planner: '规划者',
  reviewer: '审查者',
  executor: '执行者',
  validator: '校验者',
}

interface StreamPayload {
  session?: SessionState
  summary?: SessionSummary
  lane?: LaneInfo
  artifact?: Artifact
  intervention?: Intervention
  projection?: {
    terminal?: Partial<Record<RoleKey, string>>
    scene?: SceneItem | null
  }
  [key: string]: unknown
}

function payloadOf(event: StreamEvent): StreamPayload {
  return (event.data || {}) as StreamPayload
}

function emptyTerminalProjection(): TerminalProjection {
  return {
    planner: '',
    reviewer: '',
    executor: '',
    validator: '',
  }
}

export function sessionStateToSummary(session: SessionState): SessionSummary {
  return {
    session_id: String(session.session_id || ''),
    task: session.task,
    project_path: session.project_path,
    view_mode: session.view_mode,
    status: session.status,
    active_stage: session.active_stage,
    current_round: session.current_round,
    current_review_round: session.current_review_round,
    max_rounds: session.max_rounds,
    max_review_rounds: session.max_review_rounds,
    updated_at: String(session.updated_at || ''),
    created_at: String(session.created_at || ''),
    finished_at: session.finished_at,
    interrupt_reason: session.interrupt_reason,
  }
}

export function sessionStateFromStreamEvent(event: StreamEvent): SessionState | null {
  const session = payloadOf(event).session
  return session && typeof session === 'object' ? { ...session } : null
}

export function sessionSummaryFromStreamEvent(event: StreamEvent): SessionSummary | null {
  const summary = payloadOf(event).summary
  return summary && typeof summary === 'object' ? { ...summary } : null
}

export function laneFromStreamEvent(event: StreamEvent): LaneInfo | null {
  const lane = payloadOf(event).lane
  return lane && typeof lane === 'object' ? { ...lane } : null
}

export function artifactFromStreamEvent(event: StreamEvent): Artifact | null {
  const artifact = payloadOf(event).artifact
  return artifact && typeof artifact === 'object' ? { ...artifact } : null
}

export function interventionFromStreamEvent(event: StreamEvent): Intervention | null {
  const intervention = payloadOf(event).intervention
  return intervention && typeof intervention === 'object' ? { ...intervention } : null
}

export function upsertLaneInfo(lanes: LaneInfo[], lane: LaneInfo): LaneInfo[] {
  const next = lanes.filter((item) => item.role_key !== lane.role_key)
  next.push(lane)
  return next.sort((a, b) => a.sort_order - b.sort_order)
}

export function upsertArtifact(artifacts: Artifact[], artifact: Artifact): Artifact[] {
  const next = artifacts.filter((item) => item.id !== artifact.id)
  next.push(artifact)
  return next.sort((a, b) => {
    if (a.created_at === b.created_at) return a.id.localeCompare(b.id)
    return a.created_at.localeCompare(b.created_at)
  })
}

export function upsertIntervention(interventions: Intervention[], intervention: Intervention): Intervention[] {
  const next = interventions.filter((item) => item.id !== intervention.id)
  next.push(intervention)
  return next.sort((a, b) => {
    if (a.created_at === b.created_at) return a.id.localeCompare(b.id)
    return a.created_at.localeCompare(b.created_at)
  })
}

export function upsertSessionSummary(summaries: SessionSummary[], summary: SessionSummary): SessionSummary[] {
  const next = summaries.filter((item) => item.session_id !== summary.session_id)
  next.push(summary)
  return next.sort((a, b) => {
    if (a.updated_at === b.updated_at) return b.session_id.localeCompare(a.session_id)
    return b.updated_at.localeCompare(a.updated_at)
  })
}

export function terminalDeltaFromStreamEvent(event: StreamEvent): Partial<Record<RoleKey, string>> {
  const projection = payloadOf(event).projection
  if (projection?.terminal && typeof projection.terminal === 'object') {
    return projection.terminal
  }
  return {}
}

export function sceneDeltaFromStreamEvent(event: StreamEvent): SceneItem | null {
  const projection = payloadOf(event).projection
  if (projection?.scene) {
    return { ...projection.scene }
  }
  return null
}

export function appendTerminalProjection(current: TerminalProjection, event: StreamEvent): TerminalProjection {
  const delta = terminalDeltaFromStreamEvent(event)
  const next = { ...current }
  for (const roleKey of ROLE_ORDER) {
    const line = delta[roleKey]
    if (!line) continue
    next[roleKey] = next[roleKey] ? `${next[roleKey]}\n${line}` : line
  }
  return next
}

export function upsertSceneItem(items: SceneItem[], item: SceneItem): SceneItem[] {
  const next = items.filter((entry) => entry.id !== item.id)
  next.push(item)
  return next.sort((a, b) => {
    if (a.created_at === b.created_at) return a.id.localeCompare(b.id)
    return a.created_at.localeCompare(b.created_at)
  })
}

export function appendSceneProjection(items: SceneItem[], event: StreamEvent): SceneItem[] {
  const delta = sceneDeltaFromStreamEvent(event)
  return delta ? upsertSceneItem(items, delta) : items
}
