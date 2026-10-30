export type Item = Record<string, unknown>

export interface PropSpec {
  name: string
  label: string
  type:
    | 'string' | 'text' | 'number' | 'boolean' | 'options' | 'combo' | 'json' | 'code'
    | 'credential' | 'collection' | 'workflow' | 'section'
  default: unknown
  description?: string
  placeholder?: string
  options?: { label: string; value: unknown }[]
  required?: boolean
  show_if?: Record<string, unknown[]>
  credential_types?: string[]
  fields?: PropSpec[]
  rows?: number
  language?: string
  load_options?: 'models'
}

export interface NodeTypeSpec {
  type: string
  label: string
  group: string
  description: string
  icon: string
  color: string
  properties: PropSpec[]
  inputs: string[]
  ai_inputs: { name: string; label: string; required: boolean; max: number | null }[]
  outputs: string[]
  dynamic_outputs: string | null
  trigger: boolean
  /** Long-running listener (Kafka, Redis, …) that runs while the workflow is active. */
  listener?: boolean
  provides: string | null
}

export interface NodeSettings {
  on_error?: 'stop' | 'continue' | 'error_output'
  retry_on_fail?: boolean
  max_tries?: number
  wait_between_tries_ms?: number
  always_output_data?: boolean
  notes?: string
}

export interface WFNode {
  id: string
  type: string
  name: string
  position: { x: number; y: number }
  parameters: Record<string, unknown>
  settings?: NodeSettings
  disabled?: boolean
  width?: number
  height?: number
  /** Pinned output: used instead of executing the node in editor runs. */
  pin_data?: Item[] | Record<string, Item[]>
}

export interface WFEdge {
  id: string
  source: string
  sourceHandle: string
  target: string
  targetHandle: string
}

export interface Workflow {
  id: string
  name: string
  description: string
  active: boolean
  tags: string[]
  nodes: WFNode[]
  edges: WFEdge[]
  settings: {
    error_workflow_id?: string
    variables?: { key: string; value: string }[]
    /** Graph state schema (LangGraph channels): each key with its reducer and default. */
    state?: StateKey[]
  }
  updated_at: string
  created_at: string
  folder_id?: string | null
  owner_id?: string | null
  owner_name?: string | null
  node_count?: number
  trigger_types?: string[]
}

export interface LlmMessage { role: string; content: string; name?: string; tool_calls?: { name: string; args: unknown }[] }

/** One LLM / tool / memory call inside an AI node: a row of the log tree (n8n's AI logs). */
export type StateReducer = 'replace' | 'append' | 'add' | 'merge' | 'messages'
export interface StateKey { key: string; reducer: StateReducer; default?: string }

export interface SubRun {
  /** Missing on executions recorded before the log tree (rebuilt from `depth`). */
  id?: string
  /** The tool call this ran inside (e.g. a sub-agent's model calls), or null at the top. */
  parent?: string | null
  type: 'llm' | 'tool' | 'memory' | 'retriever' | 'vector_store'
  /** Canvas node name (or model / tool name for old records). */
  name: string
  node_id?: string
  node_name?: string
  node_type?: string
  model?: string
  tool?: string
  action?: string
  status: 'running' | 'success' | 'error'
  depth: number
  started_at: string
  duration_ms?: number
  input?: LlmMessage[] | string | Record<string, unknown> | unknown[]
  output?: { text?: string; tool_calls?: { name: string; args: unknown }[] } | string | Record<string, unknown> | unknown[]
  tokens?: { input?: number; output?: number; total?: number }
  error?: string
}

export interface NodeRun {
  node_id: string
  name: string
  type: string
  status: 'running' | 'success' | 'error' | 'waiting'
  started_at: string
  finished_at?: string
  duration_ms?: number
  input?: Item[]
  input_count?: number
  output?: Record<string, Item[]>
  item_counts?: Record<string, number>
  sources?: { node_id: string; port: string; handle: string; count: number }[]
  pinned?: boolean
  sub_runs?: SubRun[]
  /** Graph state after this node ran (only when the workflow uses state). */
  state?: Record<string, unknown>
  /** Keys this node wrote. */
  state_updates?: string[]
  error?: string | null
  run_index: number
}

export interface Execution {
  id: string
  workflow_id: string
  workflow_name?: string
  status: 'running' | 'success' | 'error' | 'waiting' | 'canceled'
  mode: string
  started_at: string
  finished_at?: string
  duration_ms?: number
  error?: string
  waiting_for?: { kind: string; node_id: string; node_name: string; message: string; fields?: { name: string; label: string }[] } | null
  input?: Item[]
  output?: Item[]
  node_runs?: NodeRun[]
  trigger_node?: string
  workflow?: { nodes: WFNode[]; edges: WFEdge[]; name: string }
}

export interface CredentialField {
  name: string
  label: string
  type: 'string' | 'number' | 'boolean' | 'options' | 'textarea'
  secret: boolean
  default: unknown
  placeholder: string
  required: boolean
  options: string[] | null
  help: string
  show_if: Record<string, unknown[]> | null
}

export interface CredentialType {
  label: string
  category: string
  fields: CredentialField[]
  color: string
  docs: string
  description: string
  testable: boolean
  oauth2: boolean
}

export interface CredentialCatalog {
  categories: string[]
  types: Record<string, CredentialType>
  oauth_redirect_uri: string
}

export interface Credential {
  id: string
  name: string
  type: string
  type_label: string
  category?: string
  color?: string
  testable?: boolean
  oauth2?: boolean
  oauth_connected?: boolean
  owner_id?: string | null
  owner_name?: string | null
  owner_email?: string | null
  updated_at: string
  data?: Record<string, unknown>
}

export interface TestResult { ok: boolean; message: string; untested?: boolean }

/** Latest run of a node + all its runs in the current execution (loops run nodes repeatedly). */
export interface RunState {
  status: 'running' | 'success' | 'error' | 'waiting'
  output?: Record<string, Item[]>
  input?: Item[]
  error?: string | null
  counts?: Record<string, number>
  duration_ms?: number
  started_at?: string
  pinned?: boolean
  runs: NodeRun[]
}

export interface ExecEvent {
  type: string
  execution_id: string
  node_id?: string
  status?: string
  error?: string | null
  output?: Record<string, Item[]> | Item[]
  item_counts?: Record<string, number>
  token?: string
  message?: string
  duration_ms?: number
  waiting_for?: Execution['waiting_for']
  mode?: string
  name?: string
  started_at?: string
  finished_at?: string
  run_index?: number
  pinned?: boolean
  ts?: number
  sub_run?: SubRun
}

export interface ListenerStatus {
  state: 'starting' | 'listening' | 'error' | 'stopped'
  error: string | null
  events: number
  last_event_at: number | null
  since: number
}

/** The LiveKit voice worker the backend runs for an active workflow's Voice Agent trigger. */
export interface VoiceWorkerStatus {
  kind: 'voice_worker'
  state: 'starting' | 'running' | 'error' | 'stopped' | 'external'
  error: string | null
  agent_name: string
  workflows: number
  since: number
  pid?: number | null
  restarts?: number
  logs: string[]
}

export interface Folder {
  id: string
  name: string
  parent_id: string | null
  owner_id?: string | null
  owner_name?: string | null
  workflow_count: number
  created_at: string
  updated_at: string
}

export interface ExecutionFilter {
  workflow_id?: string
  status?: string
  mode?: string
  started_after?: string
  started_before?: string
}

export interface CodeRuntime {
  key: string
  label: string
  available: boolean
  path: string | null
  version: string | null
  builtin?: boolean
  install_hint?: string | null
}

export type Role = 'owner' | 'admin' | 'editor' | 'viewer'

export interface User {
  id: string
  email: string
  username: string | null
  name: string
  role: Role
  mfa_enabled: boolean
  must_change_password: boolean
  disabled: boolean
  last_login_at: string | null
  created_at: string
  recovery_codes_left: number
  workflow_count?: number  // admin user list only
}

export interface AuthStatus { setup_required: boolean; signup_enabled: boolean; user: User | null }

export interface AuthSessionInfo { id: string; created_at: string; last_seen_at: string; expires_at: string; ip: string; user_agent: string; current: boolean }

export interface ApiKey { id: string; name: string; prefix: string; role: Role; created_at: string; expires_at: string | null; last_used_at: string | null }

export interface AuditEntry { id: number; at: string; user_email: string; action: string; method: string; path: string; status: number; ip: string; detail: Record<string, unknown> }

export interface LoginChallenge {
  verification_required: true
  challenge: string
  sent_to: string
  delivery: 'email' | 'console'
  methods: ('email' | 'totp' | 'recovery')[]
  resend_in: number
}

/** LiveKit SIP configuration of one LiveKit credential (protobuf JSON, snake_case). */
export interface SipTrunk {
  sip_trunk_id: string; name?: string; numbers?: string[]; address?: string; transport?: string
  krisp_enabled?: boolean; allowed_addresses?: string[]; auth_username?: string; metadata?: string
}
export interface DispatchRule {
  sip_dispatch_rule_id: string; name?: string; trunk_ids?: string[]; inbound_numbers?: string[]; krisp_enabled?: boolean
  rule?: Record<string, Record<string, unknown>>
  room_config?: { agents?: { agent_name?: string; metadata?: string }[] }
}
export interface Telephony { inbound: SipTrunk[]; outbound: SipTrunk[]; rules: DispatchRule[]; sip_uri: string }
