import type { ExprResult } from './components/ExprField'
import type { ApiKey, Telephony, AuditEntry, AuthSessionInfo, AuthStatus, LoginChallenge, CodeRuntime, Credential, CredentialCatalog, Execution, ExecutionFilter, Folder, Item, ListenerStatus, NodeTypeSpec, VoiceWorkerStatus, TestResult, User, Workflow, WorkflowVersion } from './types'

/** Double-submit CSRF token: the server sets the readable `db_csrf` cookie at sign-in. */
export function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)db_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

/** A non-2xx answer: `status` and the server's `detail` (string, or an object such as {code, message, workflow}). */
export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
  /** Overloaded server (503 + Retry-After) or rate limited. */
  get retryable() { return this.status === 503 || this.status === 429 }
}

/** The conflicting workflow when a save was rejected because someone else saved first (409 conflict). */
export function conflictOf(e: unknown): Workflow | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null
  const d = e.detail as { code?: string; workflow?: Workflow } | null
  return d && typeof d === 'object' && d.code === 'conflict' && d.workflow ? d.workflow : null
}

async function req<T>(method: string, url: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrfToken() }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = { detail: text.slice(0, 200) } }
  if (!res.ok) {
    const detail = data?.detail
    // Session expired / revoked, or an admin-issued temporary password: let the auth layer react.
    if (res.status === 401 && !url.startsWith('/api/auth/')) window.dispatchEvent(new Event('auth:expired'))
    if (res.status === 403 && detail === 'password_change_required') window.dispatchEvent(new Event('auth:expired'))
    // Structured errors ({code, message, …}) show their message; the full detail stays on the error.
    // FastAPI validation errors are a list of {loc, msg}: "name: String should have at most 200 characters".
    const fieldErr = (d: { loc?: unknown[]; msg?: string }) => {
      const loc = (d.loc ?? []).filter((x) => x !== 'body' && x !== 'query' && x !== 'path').join('.')
      return `${loc ? `${loc}: ` : ''}${d.msg ?? 'invalid value'}`.replace(/^Value error, /, '')
    }
    const msg = typeof detail === 'string' ? detail
      : Array.isArray(detail) && detail.length ? detail.map((d) => (d && typeof d === 'object' ? fieldErr(d) : String(d))).join('; ')
      : detail && typeof detail === 'object' && typeof detail.message === 'string' ? detail.message
      : res.status === 503 ? `The server is busy. Try again${res.headers.get('Retry-After') ? ` in ${res.headers.get('Retry-After')} s` : ' shortly'}.`
      : res.status === 413 ? 'That is too large for the server (limit 16 MB).'
      : JSON.stringify(detail ?? data ?? res.statusText)
    throw new ApiError(msg, res.status, detail)
  }
  return data as T
}

export const auth = {
  status: () => req<AuthStatus>('GET', '/api/auth/status'),
  /** Create an account (the owner on a new installation). Nothing is saved until the emailed code is entered. */
  signup: (b: { email: string; name: string; username: string; password: string }) => req<LoginChallenge>('POST', '/api/auth/signup', b),
  /** Password step: never signs in directly; returns a challenge (code emailed to the user). */
  login: (login: string, password: string) => req<LoginChallenge>('POST', '/api/auth/login', { login, password }),
  verify: (challenge: string, code: string, method: 'email' | 'totp' | 'recovery') =>
    req<{ user: User }>('POST', '/api/auth/login/verify', { challenge, code, method }),
  resend: (challenge: string) => req<{ delivery: string; resend_in: number }>('POST', '/api/auth/login/resend', { challenge }),
  /** Forgotten password: always "sent" (the server never reveals whether the account exists). */
  forgot: (login: string) => req<{ challenge: string; delivery: 'email' | 'console'; resend_in: number }>('POST', '/api/auth/password/forgot', { login }),
  resetVerify: (challenge: string, code: string) => req<{ reset_token: string }>('POST', '/api/auth/password/reset/verify', { challenge, code }),
  finishReset: (reset_token: string, password: string) => req('POST', '/api/auth/password/reset', { reset_token, password }),
  logout: () => req('POST', '/api/auth/logout'),
  me: () => req<{ user: User; via: string }>('GET', '/api/auth/me'),
  updateMe: (b: { name?: string; username?: string }) => req<{ user: User }>('PUT', '/api/auth/me', b),
  changePassword: (current_password: string, new_password: string) => req('POST', '/api/auth/password', { current_password, new_password }),
  mfaSetup: () => req<{ secret: string; otpauth_uri: string; qr: string; pending_token: string }>('POST', '/api/auth/mfa/setup'),
  mfaEnable: (pending_token: string, code: string) => req<{ recovery_codes: string[] }>('POST', '/api/auth/mfa/enable', { pending_token, code }),
  mfaDisable: (password: string, code: string) => req('POST', '/api/auth/mfa/disable', { password, code }),
  recoveryCodes: (password: string, code: string) => req<{ recovery_codes: string[] }>('POST', '/api/auth/mfa/recovery-codes', { password, code }),
  sessions: () => req<AuthSessionInfo[]>('GET', '/api/auth/sessions'),
  revokeSession: (id: string) => req('DELETE', `/api/auth/sessions/${id}`),
  revokeOtherSessions: () => req('POST', '/api/auth/sessions/revoke-others'),
  apiKeys: () => req<ApiKey[]>('GET', '/api/auth/api-keys'),
  createApiKey: (b: { name: string; role?: string; expires_days?: number | null }) => req<ApiKey & { key: string }>('POST', '/api/auth/api-keys', b),
  revokeApiKey: (id: string) => req('DELETE', `/api/auth/api-keys/${id}`),
  // admin
  users: () => req<User[]>('GET', '/api/users'),
  createUser: (b: { email: string; name: string; username: string; role: string }) => req<{ user: User; temporary_password: string | null }>('POST', '/api/users', b),
  updateUser: (id: string, b: { name?: string; username?: string; role?: string; disabled?: boolean }) => req<{ user: User }>('PUT', `/api/users/${id}`, b),
  resetPassword: (id: string) => req<{ temporary_password: string }>('POST', `/api/users/${id}/reset-password`),
  resetMfa: (id: string) => req('POST', `/api/users/${id}/reset-mfa`),
  revokeUserSessions: (id: string) => req('POST', `/api/users/${id}/revoke-sessions`),
  /** Deletes the user's workflows, credentials and executions too, unless `transferTo` names a user who gets them. */
  deleteUser: (id: string, transferTo?: string) =>
    req('DELETE', `/api/users/${id}${transferTo ? `?transfer_to=${encodeURIComponent(transferTo)}` : ''}`),
  audit: (params: { user?: string; action?: string; limit?: number; offset?: number }) =>
    req<{ total: number; items: AuditEntry[] }>('GET', `/api/audit?${new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))}`),
}

export const api = {
  nodeTypes: () => req<NodeTypeSpec[]>('GET', '/api/node-types'),
  codeRuntimes: () => req<CodeRuntime[]>('GET', '/api/code-runtimes'),

  workflows: () => req<Workflow[]>('GET', '/api/workflows'),
  workflow: (id: string) => req<Workflow>('GET', `/api/workflows/${id}`),
  expressionPreview: (id: string, body: { value: unknown; items: Item[]; node_outputs: Record<string, Item[]>; node_names?: string[]; item_index?: number; state?: Record<string, unknown> }) =>
    req<ExprResult>('POST', `/api/workflows/${id}/expression-preview`, body),
  codeCheck: (id: string, body: { language: string; code: string }) =>
    req<{ ok: boolean; checked?: boolean; line?: number; column?: number; message?: string }>('POST', `/api/workflows/${id}/code-check`, body),
  listenerStatus: (id: string) => req<Record<string, ListenerStatus>>('GET', `/api/workflows/${id}/listeners`),
  voiceWorkerStatus: (id: string) => req<Record<string, VoiceWorkerStatus>>('GET', `/api/workflows/${id}/listeners`),
  restartVoiceWorker: (id: string, nodeId: string) => req<VoiceWorkerStatus>('POST', `/api/workflows/${id}/voice-worker/${nodeId}/restart`),
  createWorkflow: (wf: Partial<Workflow>) => req<Workflow>('POST', '/api/workflows', wf),
  /** With `revision`, the server refuses (409 conflict) when the workflow changed since that revision. */
  updateWorkflow: (id: string, wf: Partial<Workflow> & { revision?: number }) => req<Workflow>('PUT', `/api/workflows/${id}`, wf),
  deleteWorkflow: (id: string) => req('DELETE', `/api/workflows/${id}`),
  /** Publish the draft as a new version: production (triggers, webhooks, schedules) runs it from now on. */
  publishWorkflow: (id: string, body: { note?: string; activate?: boolean } = {}) => req<Workflow>('POST', `/api/workflows/${id}/publish`, body),
  workflowVersions: (id: string) => req<WorkflowVersion[]>('GET', `/api/workflows/${id}/versions`),
  workflowVersion: (id: string, versionId: string) => req<WorkflowVersion>('GET', `/api/workflows/${id}/versions/${versionId}`),
  /** Make production run this earlier version (rollback); the draft is unchanged. */
  publishVersion: (id: string, versionId: string) => req<Workflow>('POST', `/api/workflows/${id}/versions/${versionId}/publish`),
  duplicateWorkflow: (id: string, body: { name?: string; folder_id?: string | null } = {}) =>
    req<Workflow>('POST', `/api/workflows/${id}/duplicate`, body),
  moveWorkflows: (ids: string[], folderId: string | null) => req('POST', '/api/workflows/move', { ids, folder_id: folderId }),
  deleteWorkflows: (ids: string[]) => req<{ deleted: number }>('POST', '/api/workflows/delete', { ids }),
  exportWorkflow: (id: string) => req<Record<string, unknown>>('GET', `/api/workflows/${id}/export`),
  exportWorkflows: (params: { ids?: string[]; folder_id?: string }) =>
    req<{ workflows: Record<string, unknown>[] }>('GET', `/api/workflows/export?${new URLSearchParams(
      params.ids ? { ids: params.ids.join(',') } : params.folder_id ? { folder_id: params.folder_id } : {})}`),
  importWorkflows: (body: { data?: unknown; url?: string; folder_id?: string | null }) => req<Workflow[]>('POST', '/api/workflows/import', body),

  folders: () => req<Folder[]>('GET', '/api/folders'),
  createFolder: (name: string, parentId: string | null) => req<Folder>('POST', '/api/folders', { name, parent_id: parentId }),
  updateFolder: (id: string, body: { name?: string; parent_id?: string | null }) => req<Folder>('PUT', `/api/folders/${id}`, body),
  deleteFolder: (id: string, mode: 'move' | 'delete') => req('DELETE', `/api/folders/${id}?mode=${mode}`),
  runWorkflow: (id: string, body: { trigger_node_id?: string; items?: unknown[]; workflow?: Partial<Workflow>; destination_node_id?: string }) =>
    req<{ execution_id: string }>('POST', `/api/workflows/${id}/run`, body),
  templates: () => req<{ id: string; name: string; description: string; tags: string[] }[]>('GET', '/api/templates'),
  fromTemplate: (id: string) => req<Workflow>('POST', `/api/templates/${id}`),

  executions: (params: ExecutionFilter & { limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))
    return req<{ total: number; items: Execution[] }>('GET', `/api/executions?${q}`)
  },
  execution: (id: string) => req<Execution>('GET', `/api/executions/${id}`),
  deleteExecution: (id: string) => req('DELETE', `/api/executions/${id}`),
  /** Batch actions: explicit ids, or everything matching a filter ("select all N"). */
  deleteExecutions: (sel: { ids?: string[]; filter?: ExecutionFilter }) => req<{ deleted: number }>('POST', '/api/executions/delete', sel),
  stopExecutions: (sel: { ids?: string[]; filter?: ExecutionFilter }) => req<{ stopped: number }>('POST', '/api/executions/stop', sel),
  resume: (id: string, approved: boolean, data: Record<string, unknown> = {}) =>
    req('POST', `/api/executions/${id}/resume`, { approved, data }),
  cancel: (id: string) => req('POST', `/api/executions/${id}/cancel`),
  retry: (id: string) => req<{ execution_id: string }>('POST', `/api/executions/${id}/retry`),
  execStats: () => req<Record<string, number>>('GET', '/api/executions/stats'),

  credentialTypes: () => req<CredentialCatalog>('GET', '/api/credentials/types'),
  testCredential: (type: string, data: Record<string, unknown>, id?: string) =>
    req<TestResult>('POST', '/api/credentials/test', { type, data, id }),
  testSavedCredential: (id: string) => req<TestResult>('POST', `/api/credentials/${id}/test`),
  oauthUrl: (credentialId: string) => req<{ url: string; redirect_uri: string }>('GET', `/api/oauth2/auth-url?credential_id=${credentialId}`),
  credentials: (type?: string) => req<Credential[]>('GET', `/api/credentials${type ? `?type=${type}` : ''}`),
  credential: (id: string) => req<Credential>('GET', `/api/credentials/${id}`),
  /** Models the provider offers for this credential (chat model nodes' "From list"). */
  credentialModels: (id: string, refresh = false) =>
    req<{ models: { value: string; label: string; hint?: string }[] }>('GET', `/api/credentials/${id}/models${refresh ? '?refresh=true' : ''}`),
  createCredential: (c: { name: string; type: string; data: Record<string, unknown> }) => req<Credential>('POST', '/api/credentials', c),
  updateCredential: (id: string, c: { name?: string; data?: Record<string, unknown> }) => req<Credential>('PUT', `/api/credentials/${id}`, c),
  deleteCredential: (id: string) => req('DELETE', `/api/credentials/${id}`),

  // Telephony (LiveKit SIP trunks / dispatch rules, Plivo wiring)
  telephony: (credId: string) => req<Telephony>('GET', `/api/voice/telephony/${credId}`),
  addInboundTrunk: (credId: string, b: Record<string, unknown>) => req<Record<string, unknown>>('POST', `/api/voice/telephony/${credId}/inbound`, b),
  addOutboundTrunk: (credId: string, b: Record<string, unknown>) => req<Record<string, unknown>>('POST', `/api/voice/telephony/${credId}/outbound`, b),
  addDispatchRule: (credId: string, b: Record<string, unknown>) => req<Record<string, unknown>>('POST', `/api/voice/telephony/${credId}/rules`, b),
  deleteTrunk: (credId: string, id: string) => req('DELETE', `/api/voice/telephony/${credId}/trunks/${id}`),
  deleteDispatchRule: (credId: string, id: string) => req('DELETE', `/api/voice/telephony/${credId}/rules/${id}`),
  plivoConnect: (b: Record<string, unknown>) => req<Record<string, unknown>>('POST', '/api/voice/telephony/plivo-connect', b),

  chat: (workflowId: string, message: string, sessionId: string) =>
    req<{ execution_id: string }>('POST', `/api/chat/${workflowId}`, { message, session_id: sessionId },
      { 'x-debbuilder-editor': '1' }),
  voiceSession: (workflowId: string) =>
    req<{ url: string; token: string; room: string }>('POST', '/api/voice/session', { workflow_id: workflowId }),
}

export function openSocket(path: string, onEvent: (ev: any) => void): () => void {
  let ws: WebSocket | null = null
  let closed = false
  let retry: number | undefined
  const connect = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}${path}`)
    ws.onmessage = (m) => onEvent(JSON.parse(m.data))
    ws.onclose = () => {
      if (!closed) retry = window.setTimeout(connect, 1500)
    }
  }
  connect()
  return () => {
    closed = true
    window.clearTimeout(retry)
    ws?.close()
  }
}

export function outputText(items: Record<string, unknown>[] | undefined): string {
  const first = items?.[0]
  if (!first) return ''
  for (const k of ['output', 'text', 'response', 'message', 'answer', 'content']) {
    const v = first[k]
    if (typeof v === 'string' && v) return v
  }
  return JSON.stringify(first, null, 2)
}

/** Save JSON as a file download. */
export function downloadJson(data: unknown, filename: string) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
  a.download = filename.replace(/[\\/:*?"<>|]+/g, '_')
  a.click()
  URL.revokeObjectURL(a.href)
}
