<script lang="ts">
import type { OpenapiImportConfirmRequest } from '@/services/externalAgents'

/**
 * OpenAPI 导入面板产出的载荷：调用方补上 name（创建模式）或 agent_id（追加模式）
 * 后组成 OpenapiImportConfirmRequest 发给 confirm 端点。
 */
export interface OpenapiImportPayload {
  doc_url?: string
  doc?: Record<string, any>
  auth: OpenapiImportConfirmRequest['auth']
  selected: OpenapiImportConfirmRequest['selected']
}
</script>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  AlertCircle,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  Copy,
  Download,
  FileJson,
  Link2,
  Loader2,
  X,
} from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import {
  externalAgentPluginApi,
  type OpenapiImportPreviewResponse,
  type PluginFieldUi,
  type ToolInputField,
} from '@/services/externalAgents'

/**
 * OpenAPI 导入面板（原向导第 2 步导入模式的全部 UI）。
 *
 * 同一份面板被两处复用：
 *  - 创建向导第 2 步（ExternalAgentFormView 创建分支）
 *  - 管理页「添加功能」内联区块（追加模式，confirm 时带 agent_id）
 *
 * 面板只负责：贴 URL/JSON → 解析预览 → 勾选 + 字段 override + 统一鉴权，
 * 并对外暴露 checkValid() / buildPayload() / reset()；confirm 请求由调用方发起。
 */
const { t } = useI18n()

/**
 * 追加模式可选入参：插件下已存在的 operation（method+path，method 大小写不敏感）。
 * 解析预览后这些行默认取消勾选并打上「已存在」标记；不传时面板行为不变。
 */
const props = defineProps<{
  existingOps?: Array<{ method: string; path: string }>
}>()

const importSourceTab = ref<'url' | 'json'>('url')
const importUrl = ref('')
const importJson = ref('')
const importPreview = ref<OpenapiImportPreviewResponse | null>(null)
const importing = ref(false)
const importError = ref<string | null>(null)
const expandedFunctions = reactive<Record<string, boolean>>({})
const expandedServers = ref(false)
const serverCopied = ref(false)
// 按 function 名记录勾选状态（draft 阶段：全部勾选；用户在 UI 中可切换）。
const importSelected = reactive<Record<string, boolean>>({})
// 按 function 名记录 field overrides：key=field name，value={label?, default?, description?, ui?}。
const importFieldOverrides = reactive<
  Record<string, Record<string, { label?: string; default?: unknown; description?: string; ui?: PluginFieldUi }>>
>({})
// 统一鉴权配置
const importAuthType = ref<'none' | 'bearer' | 'api_key_header'>('none')
const importAuthHeaderName = ref('X-API-Key')
const importAuthToken = ref('')

// 已选 function 列表（保持文档顺序，仅返回勾选=true 的）。
const IMPORT_SELECTED_LIST = computed(() => {
  if (!importPreview.value) return []
  return importPreview.value.functions.filter((f) => importSelected[f.name] !== false)
})

const IMPORT_SELECTED_COUNT = computed(() => IMPORT_SELECTED_LIST.value.length)

const IMPORT_ALL_SELECTED = computed(() => {
  if (!importPreview.value || importPreview.value.functions.length === 0) return false
  return importPreview.value.functions.every((f) => importSelected[f.name] !== false)
})

// doc URL 与第一个 server 是否等价（用于 endpoint 域校验告警）。
// 仅做 hostname 等价粗校验：包含或前缀匹配首项 server 即视为安全。
const IMPORT_ENDPOINT_SAFE = computed(() => {
  if (!importUrl.value || !importPreview.value?.servers?.length) return true
  try {
    const u = new URL(importUrl.value)
    const serverUrl = importPreview.value.servers[0]?.url
    if (!serverUrl) return true
    // 相对路径直接判不安全
    if (!serverUrl.startsWith('http://') && !serverUrl.startsWith('https://')) return false
    const s = new URL(serverUrl)
    return s.hostname === u.hostname
  } catch {
    return false
  }
})

const IMPORT_HAS_RELATIVE_SERVER = computed(() => {
  const servers = importPreview.value?.servers ?? []
  return servers.some((s) => {
    const url = typeof s === 'string' ? s : s?.url
    return !url || (!url.startsWith('http://') && !url.startsWith('https://'))
  })
})

// 面板有效性：有解析结果 + 至少选 1 个 + api_key_header 时 header 名非空。
const isValid = computed(() => {
  if (!importPreview.value) return false
  if (IMPORT_SELECTED_COUNT.value === 0) return false
  if (importAuthType.value === 'api_key_header' && !importAuthHeaderName.value.trim()) {
    return false
  }
  return true
})

function selectAllFunctions(selected: boolean) {
  if (!importPreview.value) return
  for (const f of importPreview.value.functions) {
    importSelected[f.name] = selected
  }
}

/** 该 operation（method+path）是否已存在于插件功能中（仅追加模式传 existingOps 时生效）。 */
function isExistingOp(method: string, path: string): boolean {
  if (!props.existingOps?.length) return false
  const m = String(method).toUpperCase()
  return props.existingOps.some(
    (op) => String(op.method).toUpperCase() === m && op.path === path,
  )
}

function copyFirstServer() {
  const first = importPreview.value?.servers?.[0]
  if (!first) return
  const url = typeof first === 'string' ? first : first?.url
  if (!url) return
  if (typeof navigator !== 'undefined' && navigator.clipboard) {
    navigator.clipboard
      .writeText(url)
      .then(() => {
        serverCopied.value = true
        setTimeout(() => (serverCopied.value = false), 1500)
      })
      .catch(() => {
        serverCopied.value = false
      })
  }
}

function parseImportJson(): Record<string, any> {
  const raw = importJson.value.trim()
  if (!raw) {
    throw new Error(t('externalAgentWizard.import.parseFailed'))
  }
  return JSON.parse(raw)
}

// ── 格式说明：示例 JSON + 下载规范 ──────────────────────────────────────────

const EXAMPLE_JSON = JSON.stringify({
  openapi: '3.0.0',
  info: { title: '示例系统', version: '1.0.0' },
  servers: [{ url: 'http://10.50.54.233:9000', description: '替换为你的服务地址' }],
  paths: {
    '/api/v1/orders': {
      get: {
        operationId: 'listOrders',
        summary: '查询订单列表',
        parameters: [
          {
            name: 'status', in: 'query', required: false,
            description: '订单状态',
            schema: { type: 'string', enum: ['进行中', '已完成', '已取消'] },
          },
          {
            name: 'limit', in: 'query', required: false,
            description: '返回条数',
            schema: { type: 'integer', default: 20 },
          },
        ],
        responses: {
          200: {
            description: '订单列表',
            content: {
              'application/json': {
                schema: { type: 'array', items: { type: 'object' } },
              },
            },
          },
        },
      },
      post: {
        operationId: 'createOrder',
        summary: '创建订单',
        requestBody: {
          content: {
            'application/json': {
              schema: {
                type: 'object',
                required: ['product'],
                properties: {
                  product: { type: 'string', description: '产品名称', example: '电池包A' },
                  quantity: { type: 'integer', description: '数量', default: 1 },
                  urgent: { type: 'boolean', description: '是否加急', default: false },
                },
              },
            },
          },
        },
        responses: { 200: { description: '创建结果' } },
      },
    },
    '/api/v1/orders/{order_id}': {
      get: {
        operationId: 'getOrderDetail',
        summary: '查询订单详情',
        parameters: [
          {
            name: 'order_id', in: 'path', required: true,
            description: '订单编号',
            schema: { type: 'string' },
          },
        ],
        responses: { 200: { description: '订单详情' } },
      },
    },
  },
}, null, 2)

function fillExample() {
  importSourceTab.value = 'json'
  importJson.value = EXAMPLE_JSON
}

function downloadSpec() {
  const blob = new Blob([EXAMPLE_JSON], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'openapi-example.json'
  a.click()
  URL.revokeObjectURL(url)
}

const FORMAT_SPEC_MD = [
  '# OpenAPI 导入格式规范（精简版）',
  '',
  '## 支持范围',
  '- OpenAPI 3.0/3.1 + Swagger 2.0',
  '- 贴 URL 或直接粘贴 JSON（≤5MB）',
  '',
  '## 字段映射规则',
  '',
  '| OpenAPI 定义 | 平台效果 |',
  '|---|---|',
  '| parameter 的 description | 字段中文名（表单标签） |',
  '| schema 的 example | 输入框灰字提示 |',
  '| type: string + enum | 下拉框 |',
  '| type: string + format: date | 日期选择器 |',
  '| type: string + format: binary | 文件上传 |',
  '| type: integer/number | 数字输入 |',
  '| type: boolean | 开关 |',
  '| type: array/object | JSON 多行文本 |',
  '| 响应含 answer 字符串 | Markdown 文档渲染 |',
  '',
  '## 必须遵守',
  '',
  '1. servers[0].url 必须是绝对地址（http://...）',
  '2. 禁止 allOf/oneOf/anyOf（字段会被跳过）',
  '3. 鉴权不要用 header 参数（走平台统一配置）',
  '4. file 字段不要配 default',
  '5. 响应含 answer 字段 → 自动 Markdown 渲染',
  '',
  '## 最小示例',
  '',
  '    {',
  '      "openapi": "3.0.0",',
  '      "info": {"title": "系统名", "version": "1.0"},',
  '      "servers": [{"url": "http://你的服务地址:端口"}],',
  '      "paths": {',
  '        "/api/xxx": {',
  '          "get": {',
  '            "operationId": "getXxx",',
  '            "summary": "简洁功能名",',
  '            "parameters": [{',
  '              "name": "param1", "in": "query",',
  '              "description": "参数中文名",',
  '              "schema": {"type": "string", "example": "示例值"}',
  '            }],',
  '            "responses": {"200": {"description": "ok"}}',
  '          }',
  '        }',
  '      }',
  '    }',
  '',
  '## 给 AI 的提示词模板',
  '',
  '"请根据以下接口文档，生成一份符合 OpenAPI 3.0 规范的 JSON 文件。',
  '要求：每个参数都写中文 description 和 example；响应如果包含完整文本回答，',
  '在 200 响应的 schema 里定义一个 answer 字符串字段；不要使用 allOf。"',
  '',
  '完整版规范请联系平台管理员获取。',
].join('\n') + '\n'

function downloadFormatSpec() {
  const blob = new Blob([FORMAT_SPEC_MD], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'OpenAPI导入格式规范.md'
  a.click()
  URL.revokeObjectURL(url)
}

async function runImportPreview() {
  importError.value = null
  importPreview.value = null
  importing.value = true
  try {
    let body: { doc_url?: string; doc?: Record<string, any> }
    if (importSourceTab.value === 'url') {
      if (!importUrl.value.trim()) {
        importError.value = t('externalAgentWizard.import.source.hint')
        importing.value = false
        return
      }
      body = { doc_url: importUrl.value.trim() }
    } else {
      try {
        body = { doc: parseImportJson() }
      } catch (e) {
        importError.value =
          e instanceof Error && e.message ? e.message : t('externalAgentWizard.import.parseFailed')
        importing.value = false
        return
      }
    }
    const resp = await externalAgentPluginApi.importOpenapiPreview(body)
    importPreview.value = resp
    // 默认全选；追加模式下已存在的 operation（method+path 去重）预取消勾选
    for (const k of Object.keys(expandedFunctions)) delete expandedFunctions[k]
    for (const f of resp.functions) {
      importSelected[f.name] = !isExistingOp(f.method, f.path)
    }
  } catch (e: unknown) {
    importError.value =
      e instanceof Error && e.message ? e.message : t('externalAgentWizard.import.parseFailed')
  } finally {
    importing.value = false
  }
}

function toggleFunctionExpanded(name: string) {
  expandedFunctions[name] = !expandedFunctions[name]
}

function setFieldOverride(
  funcName: string,
  fieldName: string,
  patch: { label?: string; default?: unknown; description?: string; ui?: PluginFieldUi },
) {
  if (!importFieldOverrides[funcName]) importFieldOverrides[funcName] = {}
  const cur = importFieldOverrides[funcName][fieldName] ?? {}
  importFieldOverrides[funcName][fieldName] = { ...cur, ...patch }
}

function readFieldOverride(
  funcName: string,
  fieldName: string,
  key: 'label' | 'default' | 'description' | 'ui',
): unknown {
  return importFieldOverrides[funcName]?.[fieldName]?.[key]
}

function readFieldDefaultForInput(f: ToolInputField, funcName: string): string {
  const override = importFieldOverrides[funcName]?.[f.key]?.default
  const v = override !== undefined ? override : f.default
  if (v === undefined || v === null) return ''
  if (f.type === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

function writeFieldOverrideFromText(funcName: string, f: ToolInputField, raw: string) {
  if (raw === '') {
    setFieldOverride(funcName, f.key, { default: undefined })
    return
  }
  if (f.type === 'number') {
    const n = Number(raw)
    if (Number.isNaN(n)) return
    setFieldOverride(funcName, f.key, { default: n })
    return
  }
  if (f.type === 'boolean') {
    setFieldOverride(funcName, f.key, { default: raw === 'true' })
    return
  }
  if (f.type === 'object') {
    try {
      setFieldOverride(funcName, f.key, { default: JSON.parse(raw) })
    } catch {
      // 静默忽略非法 JSON，保持旧值。
    }
    return
  }
  setFieldOverride(funcName, f.key, { default: raw })
}

/** 组装 confirm 载荷（doc 来源 + auth + 勾选及 overrides）；无效时返回 null。 */
function buildPayload() {
  if (!isValid.value) return null
  const selected = IMPORT_SELECTED_LIST.value.map((f) => {
    const overrides = importFieldOverrides[f.name] ?? {}
    const fieldOverrides: Record<
      string,
      { label?: string; default?: unknown; description?: string; ui?: PluginFieldUi }
    > = {}
    for (const [k, v] of Object.entries(overrides)) {
      const out: { label?: string; default?: unknown; description?: string; ui?: PluginFieldUi } = {}
      if (v.label !== undefined) out.label = v.label
      if (v.default !== undefined) out.default = v.default
      if (v.description !== undefined) out.description = v.description
      if (v.ui !== undefined) out.ui = v.ui
      if (Object.keys(out).length > 0) fieldOverrides[k] = out
    }
    const item = {
      name: f.name,
      method: f.method,
      path: f.path,
    } as NonNullable<OpenapiImportPayload['selected'][number]>
    if (f.summary) item.summary = f.summary
    if (f.output_hint_suggestion) item.output_hint = f.output_hint_suggestion
    if (Object.keys(fieldOverrides).length > 0) item.field_overrides = fieldOverrides
    return item
  })
  const auth: OpenapiImportPayload['auth'] = { type: importAuthType.value }
  if (importAuthType.value === 'api_key_header' && importAuthHeaderName.value.trim()) {
    auth.header_name = importAuthHeaderName.value.trim()
  }
  if (importAuthType.value !== 'none' && importAuthToken.value.trim()) {
    auth.token = importAuthToken.value.trim()
  }
  const payload: OpenapiImportPayload = { auth, selected }
  if (importSourceTab.value === 'url') payload.doc_url = importUrl.value.trim()
  else payload.doc = parseImportJson()
  return payload
}

/** 清空全部导入状态（管理页收起「添加功能」时调用）。 */
function reset() {
  importSourceTab.value = 'url'
  importUrl.value = ''
  importJson.value = ''
  importPreview.value = null
  importError.value = null
  importing.value = false
  expandedServers.value = false
  serverCopied.value = false
  importAuthType.value = 'none'
  importAuthHeaderName.value = 'X-API-Key'
  importAuthToken.value = ''
  for (const k of Object.keys(expandedFunctions)) delete expandedFunctions[k]
  for (const k of Object.keys(importSelected)) delete importSelected[k]
  for (const k of Object.keys(importFieldOverrides)) delete importFieldOverrides[k]
}

defineExpose({
  /** 当前导入配置是否有效（面板外部用函数式访问，保持跨组件响应性）。 */
  checkValid: () => isValid.value,
  buildPayload,
  reset,
})
</script>

<template>
  <div class="space-y-5">
    <!-- 0. 格式说明 + 示例（可折叠） -->
    <details class="rounded-lg border border-border bg-muted/20" data-testid="import-guide">
      <summary class="flex items-center gap-2 px-4 py-2.5 text-sm cursor-pointer select-none text-foreground">
        <BookOpen class="w-4 h-4 text-primary shrink-0" />
        <span class="font-medium">{{ t('externalAgentWizard.import.guide.title') }}</span>
        <ChevronDown class="w-4 h-4 text-muted-foreground ml-auto transition-transform" :class="{ 'rotate-180': false }" />
      </summary>
      <div class="px-4 pb-4 space-y-3 text-xs text-muted-foreground border-t border-border pt-3">
        <p>{{ t('externalAgentWizard.import.guide.intro') }}</p>
        <ul class="list-disc list-inside space-y-1">
          <li>{{ t('externalAgentWizard.import.guide.point1') }}</li>
          <li>{{ t('externalAgentWizard.import.guide.point2') }}</li>
          <li>{{ t('externalAgentWizard.import.guide.point3') }}</li>
          <li>{{ t('externalAgentWizard.import.guide.point4') }}</li>
        </ul>
        <div class="flex items-center gap-2 pt-1">
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs text-foreground hover:bg-muted/50"
            data-testid="import-fill-example"
            @click="fillExample"
          >
            <FileJson class="w-3 h-3" />
            {{ t('externalAgentWizard.import.guide.fillExample') }}
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs text-foreground hover:bg-muted/50"
            data-testid="import-download-spec"
            @click="downloadSpec"
          >
            <Download class="w-3 h-3" />
            {{ t('externalAgentWizard.import.guide.downloadSpec') }}
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs text-foreground hover:bg-muted/50"
            data-testid="import-download-format-spec"
            @click="downloadFormatSpec"
          >
            <Download class="w-3 h-3" />
            {{ t('externalAgentWizard.import.guide.downloadFormatSpec') }}
          </button>
        </div>
      </div>
    </details>

    <!-- 1. Source input: URL / JSON tabs -->
    <div class="space-y-3">
      <div class="inline-flex rounded-lg border border-border bg-background p-1 gap-1" data-testid="import-source-tabs">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors"
          :class="importSourceTab === 'url' ? 'bg-primary text-primary-foreground' : 'text-foreground hover:bg-muted'"
          data-testid="import-source-url"
          @click="importSourceTab = 'url'"
        >
          <Link2 class="w-3.5 h-3.5" />
          {{ t('externalAgentWizard.import.source.url') }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors"
          :class="importSourceTab === 'json' ? 'bg-primary text-primary-foreground' : 'text-foreground hover:bg-muted'"
          data-testid="import-source-json"
          @click="importSourceTab = 'json'"
        >
          <FileJson class="w-3.5 h-3.5" />
          {{ t('externalAgentWizard.import.source.json') }}
        </button>
      </div>

      <div v-if="importSourceTab === 'url'">
        <input
          v-model="importUrl"
          class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
          :placeholder="t('externalAgentWizard.import.source.urlPlaceholder')"
          data-testid="import-url-input"
        />
      </div>
      <div v-else>
        <textarea
          v-model="importJson"
          rows="8"
          class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary/30 resize-y"
          :placeholder="t('externalAgentWizard.import.source.jsonPlaceholder')"
          data-testid="import-json-input"
        />
      </div>

      <div class="flex items-center gap-3">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50"
          :disabled="importing"
          data-testid="import-parse-button"
          @click="runImportPreview"
        >
          <Loader2 v-if="importing" class="w-4 h-4 animate-spin" />
          <FileJson v-else class="w-4 h-4" />
          {{ importing ? t('externalAgentWizard.import.parsing') : t('externalAgentWizard.import.parseButton') }}
        </button>
        <p class="text-[10px] text-muted-foreground">
          {{ t('externalAgentWizard.import.source.hint') }}
        </p>
      </div>

      <div v-if="importError" class="rounded-lg bg-destructive/10 px-4 py-3 text-xs text-destructive">
        {{ importError }}
      </div>
    </div>

    <!-- 2. Preview result display -->
    <div v-if="importPreview" class="space-y-4" data-testid="import-preview">
      <!-- 文档级 warnings -->
      <div
        v-if="importPreview.warnings?.length"
        class="rounded-lg bg-amber-500/10 px-4 py-3 text-xs text-amber-700 dark:text-amber-400"
      >
        <div class="font-semibold mb-1">{{ t('externalAgentWizard.probe.warnings') }}</div>
        <ul class="list-disc list-inside space-y-0.5">
          <li v-for="(msg, i) in importPreview.warnings" :key="i">{{ msg }}</li>
        </ul>
      </div>

      <!-- spec_version banner -->
      <div class="flex items-center gap-3 rounded-lg border border-border bg-muted/30 px-4 py-2.5">
        <Bot class="w-4 h-4 text-primary" />
        <div class="flex-1">
          <div class="text-[10px] text-muted-foreground">{{ t('externalAgentWizard.import.specVersion') }}</div>
          <div class="text-sm font-medium text-foreground">{{ importPreview.spec_version }}</div>
        </div>
        <div class="flex-1">
          <div class="text-[10px] text-muted-foreground">{{ t('externalAgentWizard.import.servers') }}</div>
          <div class="flex items-center gap-1.5">
            <button
              type="button"
              class="text-xs font-mono text-foreground truncate max-w-[200px] hover:text-primary"
              :title="importPreview.servers[0]?.url || ''"
              @click="expandedServers = !expandedServers"
            >
              {{ importPreview.servers[0]?.url || '/' }}
            </button>
            <button
              v-if="importPreview.servers[0]?.url"
              type="button"
              class="text-muted-foreground hover:text-foreground"
              :title="t('externalAgentWizard.import.copyServer')"
              data-testid="import-copy-server"
              @click="copyFirstServer"
            >
              <Copy class="w-3.5 h-3.5" />
            </button>
            <span v-if="serverCopied" class="text-[10px] text-green-600">{{ t('externalAgentWizard.import.copied') }}</span>
          </div>
          <div v-if="expandedServers && importPreview.servers.length > 1" class="mt-1 text-[10px] text-muted-foreground font-mono">
            <div v-for="(s, i) in importPreview.servers" :key="i">{{ s.url }}</div>
          </div>
        </div>
      </div>

      <div v-if="IMPORT_HAS_RELATIVE_SERVER" class="rounded-lg bg-amber-500/10 px-4 py-2.5 text-xs text-amber-700 dark:text-amber-400">
        {{ t('externalAgentWizard.import.relativeUrlWarning') }}
      </div>
      <div v-if="!IMPORT_ENDPOINT_SAFE && importSourceTab === 'url'" class="rounded-lg bg-amber-500/10 px-4 py-2.5 text-xs text-amber-700 dark:text-amber-400">
        {{ t('externalAgentWizard.import.endpointWarning') }}
      </div>

      <!-- function 列表 -->
      <div class="space-y-2">
        <div class="flex items-center justify-between">
          <div class="text-sm font-medium text-foreground">
            {{ t('externalAgentWizard.import.functionCount', { count: importPreview.functions.length }) }}
            <span class="text-xs text-muted-foreground ml-2">
              {{ t('externalAgentWizard.import.selectedCount', { count: IMPORT_SELECTED_COUNT }) }}
            </span>
          </div>
          <div class="flex items-center gap-2">
            <button
              v-if="!IMPORT_ALL_SELECTED"
              type="button"
              class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-2.5 py-1 text-xs hover:bg-muted/50"
              data-testid="import-select-all"
              @click="selectAllFunctions(true)"
            >
              <Check class="w-3.5 h-3.5" />
              {{ t('externalAgentWizard.import.selectAll') }}
            </button>
            <button
              v-else
              type="button"
              class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-2.5 py-1 text-xs hover:bg-muted/50"
              data-testid="import-deselect-all"
              @click="selectAllFunctions(false)"
            >
              <X class="w-3.5 h-3.5" />
              {{ t('externalAgentWizard.import.deselectAll') }}
            </button>
          </div>
        </div>

        <div v-if="importPreview.functions.length === 0" class="rounded-lg border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
          {{ t('externalAgentWizard.import.noFunctions') }}
        </div>

        <div v-else class="space-y-2" data-testid="import-function-list">
          <div
            v-for="f in importPreview.functions"
            :key="f.name"
            class="rounded-lg border border-border bg-background"
            :class="importSelected[f.name] === false ? 'opacity-60' : ''"
          >
            <div class="flex items-start gap-3 p-3">
              <input
                type="checkbox"
                class="mt-1 accent-primary"
                :checked="importSelected[f.name] !== false"
                :data-testid="`import-fn-checkbox-${f.name}`"
                @change="(e) => { importSelected[f.name] = (e.target as HTMLInputElement).checked }"
              />
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-sm font-medium text-foreground">{{ f.name }}</span>
                  <span class="inline-flex items-center px-1.5 py-0.5 rounded bg-muted text-foreground font-mono text-[10px]">
                    {{ f.method }} {{ f.path }}
                  </span>
                  <span class="text-[10px] text-muted-foreground">
                    {{ t('externalAgentWizard.import.fieldCount', { count: f.fields.length }) }}
                  </span>
                  <span
                    v-if="isExistingOp(f.method, f.path)"
                    class="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-400 text-[10px] font-medium"
                    :data-testid="`import-fn-exists-${f.name}`"
                  >
                    {{ t('externalAgentWizard.import.fnExists') }}
                  </span>
                  <span v-if="f.warnings?.length" class="inline-flex items-center gap-1 text-[10px] text-amber-700 dark:text-amber-400">
                    <AlertCircle class="w-3 h-3" />
                    {{ f.warnings.length }}
                  </span>
                </div>
                <p v-if="f.summary" class="text-xs text-muted-foreground mt-1 line-clamp-2">
                  {{ f.summary }}
                </p>
                <p v-if="f.output_hint_suggestion" class="text-[10px] text-muted-foreground mt-1 font-mono">
                  {{ t('externalAgentWizard.import.outputHintLabel', { display: f.output_hint_suggestion.display }) }}
                  <span v-if="f.output_hint_suggestion.items_path">{{ t('externalAgentWizard.import.outputHintItemsPath', { path: f.output_hint_suggestion.items_path }) }}</span>
                  <span v-if="f.output_hint_suggestion.primary_key">{{ t('externalAgentWizard.import.outputHintPrimaryKey', { key: f.output_hint_suggestion.primary_key }) }}</span>
                </p>
              </div>
              <button
                v-if="f.fields.length > 0"
                type="button"
                class="p-1 text-muted-foreground hover:text-foreground"
                :data-testid="`import-fn-expand-${f.name}`"
                @click="toggleFunctionExpanded(f.name)"
              >
                <ChevronDown class="w-4 h-4 transition-transform" :class="expandedFunctions[f.name] ? 'rotate-180' : ''" />
              </button>
            </div>

            <!-- 字段明细：可编辑 default + description -->
            <div v-if="expandedFunctions[f.name]" class="border-t border-border px-3 py-3 space-y-2 bg-muted/20">
              <div class="text-[10px] font-semibold text-muted-foreground uppercase">
                {{ t('externalAgentWizard.import.expandedFields') }}
              </div>
              <div
                v-for="field in f.fields"
                :key="field.key"
                class="rounded-md border border-border bg-background p-2 space-y-1.5"
              >
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-xs font-mono font-medium text-foreground">{{ field.key }}</span>
                  <span class="text-[10px] text-muted-foreground">{{ field.type }}</span>
                  <span class="text-[10px] text-muted-foreground">ui={{ field.ui }}</span>
                  <span v-if="field.required" class="text-[10px] text-destructive">required</span>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div>
                    <label class="text-[10px] text-muted-foreground">
                      {{ t('externalAgentWizard.import.fieldOverrideLabel') }}
                    </label>
                    <input
                      :value="(readFieldOverride(f.name, field.key, 'label') as string | undefined) ?? field.label ?? ''"
                      class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                      data-testid="import-field-label-override"
                      @input="(e) => setFieldOverride(f.name, field.key, { label: (e.target as HTMLInputElement).value || undefined })"
                    />
                  </div>
                  <div>
                    <label class="text-[10px] text-muted-foreground">
                      {{ t('externalAgentWizard.import.fieldOverrideDescription') }}
                    </label>
                    <input
                      :value="(readFieldOverride(f.name, field.key, 'description') as string | undefined) ?? field.description ?? ''"
                      class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                      @input="(e) => setFieldOverride(f.name, field.key, { description: (e.target as HTMLInputElement).value || undefined })"
                    />
                  </div>
                  <div v-if="field.type !== 'file'">
                    <label class="text-[10px] text-muted-foreground">
                      {{ t('externalAgentWizard.import.fieldOverrideDefault') }}
                    </label>
                    <input
                      v-if="field.type !== 'boolean' && field.ui !== 'select'"
                      :type="field.type === 'number' ? 'number' : 'text'"
                      :value="readFieldDefaultForInput(field, f.name)"
                      class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                      @input="(e) => writeFieldOverrideFromText(f.name, field, (e.target as HTMLInputElement).value)"
                    />
                    <label v-else-if="field.type === 'boolean'" class="flex items-center gap-1.5 text-[11px]">
                      <input
                        type="checkbox"
                        class="accent-primary"
                        :checked="readFieldDefaultForInput(field, f.name) === 'true'"
                        @change="(e) => writeFieldOverrideFromText(f.name, field, (e.target as HTMLInputElement).checked ? 'true' : 'false')"
                      />
                    </label>
                    <select
                      v-else-if="field.ui === 'select'"
                      :value="readFieldDefaultForInput(field, f.name)"
                      class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                      @change="(e) => writeFieldOverrideFromText(f.name, field, (e.target as HTMLSelectElement).value)"
                    >
                      <option value=""></option>
                      <option v-for="opt in (field.options ?? [])" :key="opt" :value="opt">{{ opt }}</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 3. Auth configuration -->
      <div class="rounded-lg border border-border bg-muted/20 p-4 space-y-3">
        <div class="text-sm font-medium text-foreground">
          {{ t('externalAgentWizard.import.authSection') }}
        </div>
        <div class="flex gap-3 text-sm">
          <label class="flex items-center gap-1.5 cursor-pointer">
            <input v-model="importAuthType" type="radio" value="none" class="accent-primary" />
            {{ t('externalAgentWizard.import.authNone') }}
          </label>
          <label class="flex items-center gap-1.5 cursor-pointer">
            <input v-model="importAuthType" type="radio" value="bearer" class="accent-primary" />
            {{ t('externalAgentWizard.import.authBearer') }}
          </label>
          <label class="flex items-center gap-1.5 cursor-pointer">
            <input v-model="importAuthType" type="radio" value="api_key_header" class="accent-primary" />
            {{ t('externalAgentWizard.import.authApiKeyHeader') }}
          </label>
        </div>
        <div v-if="importAuthType === 'api_key_header'" class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-[10px] text-muted-foreground">
              {{ t('externalAgentWizard.import.headerName') }}
            </label>
            <input
              v-model="importAuthHeaderName"
              class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-sm"
              placeholder="X-API-Key"
            />
          </div>
          <div>
            <label class="text-[10px] text-muted-foreground">
              {{ t('externalAgentWizard.import.token') }}
            </label>
            <input
              v-model="importAuthToken"
              type="password"
              class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-sm"
              :placeholder="t('externalAgentWizard.import.tokenPlaceholder')"
            />
          </div>
        </div>
        <div v-else-if="importAuthType === 'bearer'">
          <label class="text-[10px] text-muted-foreground">
            {{ t('externalAgentWizard.import.token') }}
          </label>
          <input
            v-model="importAuthToken"
            type="password"
            class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-sm"
            :placeholder="t('externalAgentWizard.import.tokenPlaceholder')"
          />
        </div>
        <p class="text-[10px] text-muted-foreground">
          {{ t('externalAgentWizard.import.confirmHint') }}
        </p>
      </div>
    </div>
  </div>
</template>
