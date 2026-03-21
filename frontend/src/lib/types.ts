// Bridge frontend type system
// Authoritative source: bridge/protocol.py (L20-232)
// Optional fields: bridge/server.py handle(e) actual usage (L965-1121)

export type AgentPanel = 'planner' | 'reviewer'

// 9 session states (protocol.py L20-30)
export type SessionStatus =
  | 'idle' | 'running' | 'consensus' | 'max_rounds'
  | 'executing' | 'review_pending' | 'review_fix'
  | 'done' | 'error'

export const TERMINAL_STATES: ReadonlySet<SessionStatus> = new Set(['idle', 'done', 'error'])
export const EXECUTABLE_STATES: ReadonlySet<SessionStatus> = new Set(['consensus', 'max_rounds'])
export const CONTINUABLE_STATES: ReadonlySet<SessionStatus> = new Set(['consensus', 'max_rounds'])

// 20 event types — discriminated union (protocol.py L61-87, L211-232)
export type BridgeEvent =
  | { type: 'status_change'; data: { status: string; msg: string } }
  | { type: 'round_start'; data: { round: number; max: number } }
  | { type: 'agent_thinking'; data: { agent: AgentPanel; round: number } }
  | { type: 'cli_start'; data: { agent: AgentPanel; round: number } }
  | { type: 'agent_chunk'; data: { agent: AgentPanel; text: string; chunk_type?: 'text' | 'command' | 'command_output' } }
  | { type: 'chunk_boundary'; data: { agent: AgentPanel; boundary_type: string } }
  | { type: 'agent_stderr'; data: { agent: AgentPanel; text: string; is_mcp: boolean } }
  | { type: 'agent_result'; data: { agent: AgentPanel; text: string } }
  | { type: 'agent_response'; data: { round: number; role: AgentPanel | 'user'; phase: string; content: string } }
  | { type: 'consensus_reached'; data: { round: number; msg: string } }
  | { type: 'max_rounds_reached'; data: { round: number; msg: string } }
  | { type: 'warning'; data: { msg: string } }
  | { type: 'rollback'; data: { round: number; max: number; plan: string; msg: string } }
  | { type: 'error'; data: { msg: string } }
  | { type: 'execution_done'; data: { result: string; executor_panel: AgentPanel } }
  | { type: 'review_start'; data: { round: number; max: number } }
  | { type: 'review_round_start'; data: { round: number; max: number } }
  | { type: 'review_response'; data: { round: number; role: AgentPanel; phase: string; content: string } }
  | { type: 'review_needs_fix'; data: { round: number; msg: string; review: string } }
  | { type: 'review_done'; data: { round: number; msg: string; success: boolean } }

export type BridgeEventType = BridgeEvent['type']

// Wire event — what comes from /api/events
export interface WireEvent {
  id: number
  type: string
  data: Record<string, unknown>
  ts: string
}

// Log entries — structured, not raw HTML
export type LogEntry =
  | { kind: 'text'; text: string }
  | { kind: 'command'; text: string }
  | { kind: 'separator'; level: 'sys' | 'ok' | 'err'; text: string }
  | { kind: 'mcp'; text: string }
  | { kind: 'fold_start'; foldId: string; label: string; foldType: string }
  | { kind: 'fold_chunk'; foldId: string; text: string }
  | { kind: 'fold_end'; foldId: string }
  | { kind: 'collapsible'; label: string; content: string; open: boolean }

export interface VersionEntry {
  round: number
  phase: string
  content: string
}

// API response types (protocol.py L148-203)
export interface SessionState {
  status: SessionStatus
  round: number
  max_rounds: number
  consensus: boolean
  consensus_round: number
  history_len: number
  error: string | null
  planner_tool_id: string
  reviewer_tool_id: string
  executor_panel: AgentPanel
}

export interface HistoryEntry {
  round: number
  role: AgentPanel | 'user'
  phase: string
  content: string
}

export interface ReviewEntry {
  round: number
  role: AgentPanel
  phase: string
  content: string
}

export interface HistoryResponse {
  entries: HistoryEntry[]
  execution_result: string | null
  review_entries: ReviewEntry[]
  review_round: number
  review_status: { status: string; round: number } | null
  event_cursor: number
}

export interface EventsResponse {
  events: WireEvent[]
  next: number
}

export interface ToolInfo {
  id: string
  display_name: string
  agent_name: string
  detected_installed: boolean
  executable_path: string | null
  version: string | null
  probe_error: string | null
  last_checked_at: string | null
  capabilities: Record<string, boolean>
}

export interface RoleConfigResponse {
  planner_tool_id: string
  reviewer_tool_id: string
  executor_tool_id: string
  tools: ToolInfo[]
}

export interface RoleConfig {
  planner_tool_id: string
  reviewer_tool_id: string
}

// Prompt config keys (protocol.py L94-106)
export const PROMPT_KEYS = [
  'claude_first', 'claude_revise', 'codex_first', 'codex_review',
  'execution', 'execution_unapproved',
  'codex_post_review', 'claude_post_fix', 'codex_post_review_followup',
  'user_inject_label_claude', 'user_inject_label_codex',
] as const

export type PromptKey = typeof PROMPT_KEYS[number]

// Complete app state — everything event-handler and hydrator read/write
export interface AppState {
  // Layer 1: backend mirror
  session: SessionState

  // Layer 2: event-derived
  logs: Record<AgentPanel, LogEntry[]>
  versions: Record<AgentPanel, VersionEntry[]>
  executionResult: string | null
  showExecResult: boolean
  executorPanel: AgentPanel

  // Layer 2 attached: UI state that event-handler/hydrator write
  activeVer: Record<AgentPanel, number>  // -1 = follow latest
  activeTab: Record<AgentPanel, 'log' | 'result'>
  activeFold: Record<AgentPanel, string | null>
  foldSeq: number
  doneBadge: string | null

  // Layer 3: role config
  roleConfig: RoleConfig
  toolDisplayNames: Record<AgentPanel, string>
}

export function createEmptyState(): AppState {
  return {
    session: {
      status: 'idle', round: 0, max_rounds: 5,
      consensus: false, consensus_round: 0,
      history_len: 0, error: null,
      planner_tool_id: 'claude-code', reviewer_tool_id: 'codex',
      executor_panel: 'planner',
    },
    logs: { planner: [], reviewer: [] },
    versions: { planner: [], reviewer: [] },
    executionResult: null,
    showExecResult: false,
    executorPanel: 'planner',
    activeVer: { planner: -1, reviewer: -1 },
    activeTab: { planner: 'log', reviewer: 'log' },
    activeFold: { planner: null, reviewer: null },
    foldSeq: 0,
    doneBadge: null,
    roleConfig: { planner_tool_id: 'claude-code', reviewer_tool_id: 'codex' },
    toolDisplayNames: { planner: 'Planner', reviewer: 'Reviewer' },
  }
}
