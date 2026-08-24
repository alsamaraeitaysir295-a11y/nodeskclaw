import api from './api'

// 附件基础信息（不含 URL）
export interface AttachmentItem {
  name: string
  size: number
  content_type: string
  storage_key: string
}

// 附件信息（含访问 URL，用于消息展示）
export interface AttachmentItemWithUrl extends AttachmentItem {
  url: string
}

// 附件上传接口返回结构
export interface AttachmentUploadResponse {
  storage_key: string
  name: string
  size: number
  content_type: string
  url: string
}

// 聊天会话
export interface ChatSession {
  id: string
  agent_id: string
  user_id: string
  org_id: string
  title: string | null
  created_at: string
  updated_at: string
}

// 聊天消息
export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  thinking: string | null
  attachments: AttachmentItemWithUrl[] | null
  created_at: string
}

export interface ExternalAgent {
  id: string
  org_id: string
  name: string
  description: string | null
  endpoint: string
  protocol: 'openai_compatible' | 'custom' | 'nap'
  capabilities: string[]
  icon_emoji: string | null
  theme_color: string | null
  is_reachable: boolean
  last_checked_at: string | null
  created_at: string
  updated_at: string
  /** Phase 1 §3 插件分类：A类对话 / B类工具（后端默认 "chat"）。 */
  type?: 'chat' | 'tool'
  /** Phase 1 §10 状态机：draft / active / disabled（后端默认 "active"）。 */
  status?: 'draft' | 'active' | 'disabled'
  /** 当前 manifest 版本号（§10：Manifest 变更 version+1）。 */
  version?: number
  /** Phase 2 §8.4：tool 型插件下的 function 数量（chat 型始终为 0，前端不展示）。 */
  function_count?: number
}

export interface ExternalAgentCreate {
  name: string
  endpoint: string
  api_key?: string
  protocol?: 'openai_compatible' | 'custom' | 'nap'
  description?: string
  capabilities?: string[]
  icon_emoji?: string
  theme_color?: string
}

export interface ExternalAgentUpdate {
  name?: string
  endpoint?: string
  api_key?: string
  protocol?: 'openai_compatible' | 'custom' | 'nap'
  description?: string
  capabilities?: string[]
  icon_emoji?: string
  theme_color?: string
  /** Phase 1 §10 状态机：draft / active / disabled（后端 PATCH 已支持）。 */
  status?: 'draft' | 'active' | 'disabled'
}

export const externalAgentApi = {
  list: (): Promise<ExternalAgent[]> =>
    api.get<{ data: ExternalAgent[] }>('/external-agents').then((r) => r.data.data ?? []),

  create: (body: ExternalAgentCreate): Promise<ExternalAgent> =>
    api.post<{ data: ExternalAgent }>('/external-agents', body).then((r) => r.data.data),

  update: (id: string, body: ExternalAgentUpdate): Promise<ExternalAgent> =>
    api.patch<{ data: ExternalAgent }>(`/external-agents/${id}`, body).then((r) => r.data.data),

  remove: (id: string): Promise<void> =>
    api.delete(`/external-agents/${id}`).then(() => undefined),

  sync: (id: string): Promise<{ reachable: boolean; agent_id: string }> =>
    api
      .post<{ data: { reachable: boolean; agent_id: string } }>(`/external-agents/${id}/sync`)
      .then((r) => r.data.data),

  /** 向外部 Agent 发送消息，返回原生 Response 用于 SSE 流读取。 */
  async chatStream(
    agentId: string,
    message: string,
    sessionId: string,
    attachments?: AttachmentItemWithUrl[],
  ): Promise<Response> {
    const token = localStorage.getItem('portal_token')
    return fetch(`/api/v1/external-agents/${agentId}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        attachments: attachments ?? [],
      }),
    })
  },

  /** 上传附件到指定 Agent，返回附件元信息及访问 URL。 */
  async uploadAttachment(agentId: string, file: File): Promise<AttachmentUploadResponse> {
    const token = localStorage.getItem('portal_token')
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`/api/v1/external-agents/${agentId}/attachments/upload`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    })
    if (!res.ok) throw new Error(`上传失败: ${res.status}`)
    const json = await res.json()
    return json.data as AttachmentUploadResponse
  },

  /** 获取指定 Agent 的会话列表。 */
  async listSessions(agentId: string): Promise<ChatSession[]> {
    const token = localStorage.getItem('portal_token')
    const res = await fetch(`/api/v1/external-agents/${agentId}/sessions`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!res.ok) throw new Error(`加载会话列表失败: ${res.status}`)
    const json = await res.json()
    return json.data as ChatSession[]
  },

  /** 为指定 Agent 创建新会话。 */
  async createSession(agentId: string): Promise<ChatSession> {
    const token = localStorage.getItem('portal_token')
    const res = await fetch(`/api/v1/external-agents/${agentId}/sessions`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!res.ok) throw new Error(`创建会话失败: ${res.status}`)
    const json = await res.json()
    return json.data as ChatSession
  },

  /** 删除指定 Agent 下的某条会话。 */
  async deleteSession(agentId: string, sessionId: string): Promise<void> {
    const token = localStorage.getItem('portal_token')
    await fetch(`/api/v1/external-agents/${agentId}/sessions/${sessionId}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })
  },

  /** 获取指定会话的消息历史。 */
  async getMessages(agentId: string, sessionId: string): Promise<ChatMessage[]> {
    const token = localStorage.getItem('portal_token')
    const res = await fetch(
      `/api/v1/external-agents/${agentId}/sessions/${sessionId}/messages`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    if (!res.ok) throw new Error(`加载消息历史失败: ${res.status}`)
    const json = await res.json()
    return json.data as ChatMessage[]
  },
}

// ─────────────────────────────────────────────────────────────────────────
// tool 型插件（Phase 1 §6.2 / §7.2）：用户侧 type + ui 决定控件，
// 与后端 input_schema/output_hint 形态一一对应，schema 校验发生在后端。
// -------------------------------------------------------------------------

/** 单个表单字段定义（input_schema.fields[*]）。 */
export interface ToolInputField {
  key: string
  type: 'string' | 'number' | 'boolean' | 'object' | 'file'
  ui: 'input' | 'textarea' | 'select' | 'number' | 'switch' | 'date' | 'daterange' | 'upload'
  label: string
  required?: boolean
  /** Phase 2 §4.1：字段作用，用户端渲染在 label 下方。 */
  description?: string
  /** Phase 2 §4.1：默认值，前端预填 + 服务端动态校验兜底（type=file 不允许）。 */
  default?: unknown
  options?: string[]
  accept?: string[]
  max_mb?: number
  placeholder?: string
}

/** input_schema 完整结构（含字段渲染顺序与字段定义）。 */
export interface ToolInputSchema {
  order: string[]
  fields: Record<string, ToolInputField | any>
}

/** output_hint —— 告知前端结果如何渲染。display=text 时按 text_path 取字符串渲染 Markdown。 */
export interface ToolOutputHint {
  display: 'table' | 'json' | 'text'
  items_path?: string
  text_path?: string
  primary_key?: string
}

/** tool 型插件形态定义响应（GET /{id}/form）。 */
export interface ToolForm {
  id: string
  name: string
  description?: string | null
  version: number
  input_schema: ToolInputSchema
  output_hint: ToolOutputHint
}

/** tool 型 invoke 成功响应（2xx）。display=text 时附带 text（后端按 text_path 抽取的字符串）。 */
export interface ToolInvokeSuccess {
  success: true
  data: any
  display?: 'table' | 'json' | 'text'
  items_path?: string
  text?: string | null
  primary_key?: string
  upstream_status?: number
}

/** tool 型 invoke 失败响应（上游 5xx / 不可达 / 校验失败）。 */
export interface ToolInvokeFailure {
  success: false
  upstream_status?: number
  error?: string
  /** 字段级错误，键为字段名，值为可读错误字符串。 */
  field_errors?: Record<string, string>
}

export type ToolInvokeResponse = ToolInvokeSuccess | ToolInvokeFailure

/** tool 型文件上传响应（§8 中转流程）。 */
export interface ToolFileUpload {
  file_id: string
  url: string
  name: string
  size: number
}

// ─────────────────────────────────────────────────────────────────────────
// Phase 1 §7.1 管理端：三步向导使用的 Manifest / Validate 类型
// -------------------------------------------------------------------------

/** chat 型支持的协议枚举（与后端 schema 对齐）。 */
export const PLUGIN_PROTOCOLS = [
  'openai_compatible',
  'nap',
  'rag_standard',
  'custom',
] as const
export type PluginProtocol = (typeof PLUGIN_PROTOCOLS)[number]

/** 插件类型：A类对话 / B类工具。 */
export type PluginType = 'chat' | 'tool'

/** chat 型会话归属：platform=平台维护；external=外部系统维护（仅 rag_standard 允许）。 */
export type SessionManagedBy = 'platform' | 'external'

/** invoke.http 方法。 */
export const PLUGIN_HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const
export type PluginHttpMethod = (typeof PLUGIN_HTTP_METHODS)[number]

/** invoke.auth 类型。 */
export type PluginAuthType = 'none' | 'bearer' | 'api_key_header'

/** invoke.pass_mode：multipart 流式转发 / url_ref 生成临时 URL。 */
export type PluginPassMode = 'multipart' | 'url_ref'

/** 字段类型（与后端一致，控制校验规则）。 */
export type PluginFieldType = 'string' | 'number' | 'boolean' | 'object' | 'file'

/** 控件类型（决定前端渲染）。 */
export const PLUGIN_FIELD_UIS = [
  'input',
  'textarea',
  'select',
  'number',
  'switch',
  'date',
  'daterange',
  'upload',
] as const
export type PluginFieldUi = (typeof PLUGIN_FIELD_UIS)[number]

/** 结果展示形态。 */
export type PluginDisplayType = 'table' | 'json'

/** input_schema 单字段定义。 */
export interface PluginFieldSchema {
  type: PluginFieldType
  ui: PluginFieldUi
  label: string
  required?: boolean
  /** Phase 2 §4.1：字段作用，管理端构建器可填。 */
  description?: string
  /** Phase 2 §4.1：默认值。type=file 不允许。 */
  default?: unknown
  options?: string[]
  accept?: string[]
  max_mb?: number
}

/** tool 型 invoke 配置。 */
export interface PluginInvokeConfig {
  endpoint: string
  method: PluginHttpMethod
  auth_type: PluginAuthType
  header_name?: string
  token?: string
  timeout_seconds: number
  pass_mode: PluginPassMode
}

/** tool 型 input_schema。 */
export interface PluginInputSchema {
  order: string[]
  fields: Record<string, PluginFieldSchema>
}

/** tool 型 output_hint。 */
export interface PluginOutputHint {
  display: PluginDisplayType
  items_path?: string
  primary_key?: string
}

/** 完整的插件清单（前端向导最终组装成的结构）。 */
export interface PluginManifest {
  name: string
  description?: string
  icon_emoji?: string
  theme_color?: string
  type: PluginType
  // chat 型
  protocol?: PluginProtocol
  endpoint?: string
  api_key?: string
  session_managed_by?: SessionManagedBy
  // tool 型
  invoke?: PluginInvokeConfig
  input_schema?: PluginInputSchema
  output_hint?: PluginOutputHint
}

/** /plugins/validate 响应（扁平化，从 {data: ...} 解包后）。 */
export interface PluginConnectivity {
  ok: boolean
  http_code?: number | null
  latency_ms?: number | null
  error?: string | null
}

export interface PluginValidateResult {
  schema_ok: boolean
  schema_errors?: string[]
  connectivity: PluginConnectivity
  warnings: string[]
}

/** SSRF 允许网段配置（原 §9.2）。 */
export interface AllowedCidrs {
  cidrs: string[]
}

/** Phase 1：插件状态（draft / active / disabled）。 */
export type PluginStatus = 'draft' | 'active' | 'disabled'

export const externalAgentToolApi = {
  /** 拉取 tool 型插件的表单 schema（仅 status=active 且用户有权限时返回）。 */
  getForm(agentId: string): Promise<ToolForm> {
    return api
      .get<{ data: ToolForm }>(`/external-agents/${agentId}/form`)
      .then((r) => r.data.data)
  },

  /**
   * 表单直连调用入口（§6.2 步骤 2-5）。
   * 普通字段直接落 JSON body；file 字段只发送文件对应的 file_id（由 uploadToolFile 获取）。
   * 后端 422 响应会通过 axios reject 抛出，调用方可在 catch 中读取 error.response.data
   * 拿到 { message, field_errors }。
   */
  invokeTool(agentId: string, params: Record<string, any>): Promise<ToolInvokeResponse> {
    return api
      .post<{ data: ToolInvokeResponse }>(`/external-agents/${agentId}/invoke`, params)
      .then((r) => r.data.data)
  },

  /**
   * 文件上传中转（§8）：用户文件**只传平台**，由平台落对象存储并返回 file_id（TTL 24h）。
   * 提交表单时通过 file_id 引用，平台按 pass_mode 决定流式转发或多模 URL_ref。
   * 通过项目的 axios `api` 实例，与 fetch + localStorage 旧模式隔离。
   */
  async uploadToolFile(agentId: string, file: File): Promise<ToolFileUpload> {
    const form = new FormData()
    form.append('file', file)
    const res = await api.post<{ data: ToolFileUpload }>(
      `/external-agents/${agentId}/files`,
      form,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return res.data.data
  },
}

// ─────────────────────────────────────────────────────────────────────────
// Phase 2 §8.2 / §8.3：function 维度 API（1 插件 → N 功能）。
// 与 externalAgentToolApi（agent 维度）平行；前者走 /functions/{id}/* 路由，
// 后者走旧的 /{id}/* 兼容代理。新代码（ExternalAgentToolForm.vue）统一走这里。
// -------------------------------------------------------------------------

/** function 列表项（Phase 2 §7.2 GET /{agent_id}/functions 返回）。 */
export interface ExternalAgentFunction {
  id: string
  agent_id: string
  name: string
  summary: string | null
  /** draft | active | disabled */
  status: 'draft' | 'active' | 'disabled'
  sort_order: number
  /** manual | openapi_import */
  source: 'manual' | 'openapi_import'
  version: number
  created_at: string
  updated_at: string
  /** 管理页展示 method+path 兜底用（后端响应始终携带，token 已脱敏）。 */
  invoke_config?: Record<string, any> | null
  input_schema?: Record<string, any> | null
  /** openapi 导入来源的原始引用：{path, method, operationId, spec_version}。 */
  origin_meta?: Record<string, any> | null
}

/** function 表单响应（GET /{agent_id}/functions/{function_id}/form）。
 *  shape 与 ToolForm 几乎一致，外加 function_id / agent_id 字段；
 *  invoke_config.auth.token 已被 redact_invoke_config 脱敏成 "***redacted***"。
 */
export interface ExternalAgentFunctionForm {
  function_id: string
  agent_id: string
  name: string
  summary: string | null
  version: number
  input_schema: ToolInputSchema
  output_hint: ToolOutputHint
  invoke_config: Record<string, any>
}

/** 调用历史单条记录（GET /{agent_id}/invocations，仅本人可见）。
 *  result_data 为完整 invoke 响应（success/data/display/items_path...），
 *  可直接交给 PluginResult 重放；超 50KB 的结果被截断为 {truncated: true, ...}。
 */
export interface InvocationHistoryItem {
  id: string
  agent_id: string
  function_id: string | null
  function_name: string
  params_summary: string | null
  success: boolean
  upstream_status: number | null
  latency_ms: number | null
  result_data: Record<string, any> | null
  error_message: string | null
  created_at: string
}

export const externalAgentFunctionApi = {
  /** 列出某插件下的所有 function（含 draft/disabled；前端按 status 决定是否可点）。 */
  list(agentId: string): Promise<ExternalAgentFunction[]> {
    return api
      .get<{ data: ExternalAgentFunction[] }>(`/external-agents/${agentId}/functions`)
      .then((r) => r.data.data ?? [])
  },

  /** 拉取单个 function 的表单 schema（脱敏 invoke_config；status 必须 active）。 */
  getForm(agentId: string, functionId: string): Promise<ExternalAgentFunctionForm> {
    return api
      .get<{ data: ExternalAgentFunctionForm }>(
        `/external-agents/${agentId}/functions/${functionId}/form`,
      )
      .then((r) => r.data.data)
  },

  /** function 级 invoke：参数走 { params } 包裹（与 /invoke 路由 body 形态对齐）。 */
  invoke(
    agentId: string,
    functionId: string,
    params: Record<string, any>,
  ): Promise<ToolInvokeResponse> {
    return api
      .post<{ data: ToolInvokeResponse }>(
        `/external-agents/${agentId}/functions/${functionId}/invoke`,
        { params },
      )
      .then((r) => r.data.data)
  },

  /** function 级文件上传中转（白名单/max_mb 读 function 的 file 字段）。 */
  async uploadFile(
    agentId: string,
    functionId: string,
    file: File,
  ): Promise<ToolFileUpload> {
    const form = new FormData()
    form.append('file', file)
    const res = await api.post<{ data: ToolFileUpload }>(
      `/external-agents/${agentId}/functions/${functionId}/files`,
      form,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return res.data.data
  },

  /**
   * 当前用户在该插件下的调用历史（created_at 倒序，仅本人可见）。
   * 用户侧表单页「调用历史」区块的数据源；点击条目用 result_data 重放完整结果。
   */
  listInvocations(agentId: string, limit = 10): Promise<InvocationHistoryItem[]> {
    return api
      .get<{ data: InvocationHistoryItem[] }>(
        `/external-agents/${agentId}/invocations`,
        { params: { limit } },
      )
      .then((r) => r.data.data ?? [])
  },

  /** 单功能试调（POST /{agentId}/functions/{functionId}/probe，operator 权限）。 */
  probe(
    agentId: string,
    functionId: string,
  ): Promise<{
    ok: boolean
    reachable: boolean
    http_code?: number | null
    latency_ms?: number | null
    error?: string | null
  }> {
    return api
      .post<{
        data: {
          ok: boolean
          reachable: boolean
          http_code?: number | null
          latency_ms?: number | null
          error?: string | null
        }
      }>(`/external-agents/${agentId}/functions/${functionId}/probe`)
      .then((r) => r.data.data)
  },

  /**
   * 编辑功能（PATCH；input_schema/invoke_config 变更后服务端 version+1）。
   * 管理页字段编辑器只提交 input_schema/summary/sort_order（鉴权与端点不在编辑范围）。
   */
  update(
    agentId: string,
    functionId: string,
    body: {
      name?: string
      summary?: string
      invoke_config?: Record<string, any>
      input_schema?: Record<string, any>
      status?: 'draft' | 'active' | 'disabled'
      sort_order?: number
    },
  ): Promise<ExternalAgentFunction> {
    return api
      .patch<{ data: ExternalAgentFunction }>(
        `/external-agents/${agentId}/functions/${functionId}`,
        body,
      )
      .then((r) => r.data.data)
  },

  /** 删除功能（DELETE；需要组织 admin 权限，403 时抛出 axios 错误）。 */
  remove(agentId: string, functionId: string): Promise<void> {
    return api
      .delete(`/external-agents/${agentId}/functions/${functionId}`)
      .then(() => undefined)
  },
}

// ─────────────────────────────────────────────────────────────────────────
// Phase 2 §7.1：OpenAPI 导入（preview + confirm）
// -------------------------------------------------------------------------

/** preview 阶段返回的单个 function 草稿。 */
export interface OpenapiFunctionDraft {
  name: string
  summary: string | null
  method: string
  path: string
  /** Phase 2 §6.2：type/ui/required/description/default/options 与 ToolInputField 同构。 */
  fields: ToolInputField[]
  /** Phase 2 §6.3：解析器给的建议 output_hint，管理员可在 confirm 前 tweak。 */
  output_hint_suggestion?: ToolOutputHint
  /** 解析器给出的告警，例如跨文档 $ref、未识别 format 等。 */
  warnings?: string[]
}

/** preview 响应。 */
export interface OpenapiImportPreviewResponse {
  /** 原始文档版本字符串，如 "3.0.0"/"2.0"。 */
  spec_version: string
  /** 文档声明的 servers（首项为默认）。每项为 {url, description?, variables?} 形态。 */
  servers: { url: string; description?: string }[]
  functions: OpenapiFunctionDraft[]
  /** 文档级 warnings（如相对路径、版本兼容）。 */
  warnings?: string[]
}

/** preview 请求体（二选一：doc_url 或 doc）。 */
export interface OpenapiImportPreviewRequest {
  doc_url?: string
  doc?: Record<string, any>
}

/** confirm 请求体。 */
export interface OpenapiImportConfirmRequest {
  /** 追加模式：传入已有插件 id 时不新建插件，functions 追加到该插件（仅 tool 型）。 */
  agent_id?: string
  /** 创建模式必填（服务端校验）；追加模式（带 agent_id）忽略。 */
  name?: string
  description?: string
  icon_emoji?: string
  theme_color?: string
  doc_url?: string
  doc?: Record<string, any>
  auth: {
    type: 'none' | 'bearer' | 'api_key_header'
    header_name?: string
    token?: string
  }
  selected: {
    name: string
    summary?: string
    method: string
    path: string
    output_hint?: ToolOutputHint
    field_overrides?: Record<string, { label?: string; default?: unknown; description?: string; ui?: PluginFieldUi }>
  }[]
}

/** confirm 响应：创建的 agent + 批量 functions（追加模式下 functions 仅含新建项）。 */
export interface OpenapiImportConfirmResponse {
  agent: ExternalAgent
  functions: ExternalAgentFunction[]
  /**
   * 追加模式按 method+path 去重后被跳过（未创建）的已存在 operation。
   * 创建模式恒为空数组/缺省。
   */
  skipped_existing?: Array<{ name: string; method: string; path: string }>
}

// ─────────────────────────────────────────────────────────────────────────
// Phase 1 §6.1 / §7.1：管理端向导使用的 API（validate / probe / 状态机）
// -------------------------------------------------------------------------

export const externalAgentPluginApi = {
  /**
   * 校验 Manifest 合法性 + 连通性试调，**不落库**。向导第 3 步调用。
   * （POST /api/v1/external-agents/plugins/validate）
   */
  validateManifest(body: PluginManifest): Promise<PluginValidateResult> {
    return api
      .post<{ data: PluginValidateResult }>('/external-agents/plugins/validate', body)
      .then((r) => r.data.data)
  },

  /** 手动健康探测（POST /{id}/probe）。 */
  probeAgent(id: string): Promise<PluginValidateResult> {
    return api
      .post<{ data: PluginValidateResult }>(`/external-agents/${id}/probe`)
      .then((r) => r.data.data)
  },

  /** 读取当前组织允许的 SSRF 目标网段（GET /allowed-cidrs）。 */
  listAllowedCidrs(): Promise<AllowedCidrs> {
    return api
      .get<{ data: AllowedCidrs }>('/external-agents/allowed-cidrs')
      .then((r) => r.data.data)
  },

  /** 更新当前组织允许的 SSRF 目标网段（PUT /allowed-cidrs）。 */
  updateAllowedCidrs(cidrs: string[]): Promise<AllowedCidrs> {
    return api
      .put<{ data: AllowedCidrs }>('/external-agents/allowed-cidrs', { cidrs })
      .then((r) => r.data.data)
  },

  /**
   * Phase 2 §7.1：解析 OpenAPI/Swagger 文档为 function 草稿。
   * doc_url 过 SSRF 闸门；不落库。返回草稿供前端勾选 + tweak field overrides。
   */
  importOpenapiPreview(
    body: OpenapiImportPreviewRequest,
  ): Promise<OpenapiImportPreviewResponse> {
    return api
      .post<{ data: OpenapiImportPreviewResponse }>(
        '/external-agents/plugins/import/openapi/preview',
        body,
      )
      .then((r) => r.data.data)
  },

  /**
   * Phase 2 §7.1：根据 selected + auth 一次性创建插件与批量 functions。
   * 服务端会重新解析文档（不信任前端回传草稿）以 method+path 匹配。
   */
  importOpenapiConfirm(
    body: OpenapiImportConfirmRequest,
  ): Promise<OpenapiImportConfirmResponse> {
    return api
      .post<{ data: OpenapiImportConfirmResponse }>(
        '/external-agents/plugins/import/openapi',
        body,
      )
      .then((r) => r.data.data)
  },
}
