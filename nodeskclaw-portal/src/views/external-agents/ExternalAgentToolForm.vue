<script setup lang="ts">
/**
 * 用户侧 tool 型插件入口（spec Phase 1 §7.2 + Phase 2 §8.2）。
 *
 * 设计要点：
 * - Phase 2：进入页面先 GET /{id}/functions 拉该插件全部 function。
 *   - 单 function（最常见，存量迁移出来的 default function）：**UX 与 Phase 1 完全一致**——
 *     无任何选择器，直接渲染默认 function 的表单（review C5 硬约束）。
 *   - 多 function：在表单顶部加横向 tab 选择器，切换即按 function.id 重新拉表单 schema；
 *     状态非 active 的 tab 加 disabled 视觉态，但仍然可点（UX 决策：预览可见）。
 * - 字段类型由后端 (type, ui) 决定前端控件：input/textarea/select/number/switch/date/daterange/upload。
 *   模式参考 `views/InstanceChannels.vue:399-468`（schema 驱动渲染）。
 * - 字段下方显示 `description`（Phase 2 §4.3，spec §6.2 从 OpenAPI 自动带入）；
 *   默认值由 `default` 预填（spec §4.1 后端兜底 + 前端读 default）。
 * - 提交 POST /functions/{id}/invoke；文件字段先 POST /functions/{id}/files 拿 file_id 再传引用。
 * - 422 字段级错误回显到对应控件下方；外部错误（含 success=false）展示 + 重试按钮。
 * - 成功结果交给 PluginResult.vue 渲染（表格 / JSON 树二选一）。
 * - 调用历史（结果区下方可折叠卡片）：GET /{id}/invocations 拉当前用户最近 10 条，
 *   点击条目用存档的 result_data 经 PluginResult 重放完整结果（agent 级，跨 function）。
 */

import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  Bot,
  ChevronDown,
  ChevronLeft,
  ChevronUp,
  CircleAlert,
  Clock,
  FileText,
  Loader2,
  RefreshCw,
  Upload,
  X,
} from 'lucide-vue-next'
import CustomSelect from '@/components/shared/CustomSelect.vue'
import PluginResult from '@/components/external-agents/PluginResult.vue'
import {
  externalAgentFunctionApi,
  type ExternalAgentFunction,
  type ExternalAgentFunctionForm,
  type InvocationHistoryItem,
  type ToolForm,
  type ToolInputField,
  type ToolInputSchema,
  type ToolInvokeResponse,
  type ToolFileUpload,
} from '@/services/externalAgents'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const agentId = route.params.id as string

// ── Phase 2 §8.2：function 列表与选中态 ─────────────────────────────────────────
const functions = ref<ExternalAgentFunction[]>([])
const selectedFunctionId = ref<string | null>(null)
const functionsLoading = ref(false)
const loadingFunctionId = ref<string | null>(null)
// 全部功能都是 draft/disabled 时展示"去管理页启用"引导态（不发 form 请求，避免裸 403）
const noActiveFunction = ref(false)
// 插件/功能未启用（403 not_active / function_not_active）时的引导态：
// 展示后端文案 + 「去管理页启用」按钮，不让用户对着裸 403 猜
const enableGuidanceMessage = ref<string | null>(null)

/** 未启用类错误 → 引导态；其它错误返回文案。 */
function handleFormError(e: unknown): string {
  const key = (e as any)?.response?.data?.message_key
  if (key === 'errors.external_agent.not_active' || key === 'errors.external_agent.function_not_active') {
    enableGuidanceMessage.value = t(key)
    return t(key)
  }
  enableGuidanceMessage.value = null
  return apiErrorMessage(e)
}

/** 把 pydantic 的英文字段校验错误翻译成用户友好的中文；未识别的返回通用文案。 */
function translateFieldError(msg: string): string {
  const m = (msg || '').trim()
  if (!m) return t('pluginForm.errors.fieldInvalid')
  if (/^Field required/i.test(m)) return t('pluginForm.errors.fieldRequired')
  if (/valid number/i.test(m)) return t('pluginForm.errors.fieldNumber')
  if (/valid integer/i.test(m)) return t('pluginForm.errors.fieldNumber')
  if (/valid boolean/i.test(m)) return t('pluginForm.errors.fieldBoolean')
  if (/valid date/i.test(m)) return t('pluginForm.errors.fieldDate')
  if (/valid dict|valid object|valid JSON/i.test(m)) return t('pluginForm.errors.fieldJson')
  if (/at least \d+ character/i.test(m)) return t('pluginForm.errors.fieldTooShort')
  if (/Extra inputs|extra_forbidden/i.test(m)) return t('pluginForm.errors.fieldInvalid')
  // 已经是中文（后端自定义文案）原样返回
  if (/[一-龥]/.test(m)) return m
  return t('pluginForm.errors.fieldInvalid')
}

/** 从 axios 错误里取后端错误信息；优先用后端 message（角色名等参数已插值），
 *  仅当 message 缺失时才走 i18n key 翻译（避免 {role} 等占位符显示为空）。 */
function apiErrorMessage(e: unknown, fallbackKey = 'pluginForm.errors.loadFailed'): string {
  const resp = (e as any)?.response?.data
  // 后端 message 已包含完整中文（如"需要 operator 及以上角色"），优先使用
  if (resp?.message && /[一-龥]/.test(resp.message)) return resp.message
  // 无中文 message 时走 i18n（key 对应的模板不含参数占位符的场景）
  if (resp?.message_key && !resp.message_key.includes('insufficient')) {
    return t(resp.message_key)
  }
  if (resp?.message) return resp.message
  if (e instanceof Error && e.message) return e.message
  return t(fallbackKey)
}

// 把 ExternalAgentFunctionForm 适配成 ToolForm 形态（head + version + input_schema + output_hint），
// 这样下方所有 render 分支不感知数据来源变化。
function toToolForm(fn: ExternalAgentFunctionForm | null): ToolForm | null {
  if (!fn) return null
  return {
    id: fn.agent_id,
    name: fn.name,
    description: fn.summary ?? null,
    version: fn.version,
    input_schema: fn.input_schema ?? { order: [], fields: {} },
    output_hint: fn.output_hint ?? { display: 'json' },
  }
}

// ── 表单定义状态 ───────────────────────────────────────────────────────────────
const form = ref<ToolForm | null>(null)
const fields = computed<ToolInputField[]>(() => {
  if (!form.value?.input_schema?.order) return []
  const schema: ToolInputSchema = form.value.input_schema
  return schema.order
    .map((key) => ({ ...(schema.fields?.[key] || {}), key }))
    .filter((f) => f && f.ui)
})

// ── 用户输入：动态键值对 ─────────────────────────────────────────────────────
const values = reactive<Record<string, any>>({})
const fieldErrors = ref<Record<string, string>>({})
const topLevelError = ref<string | null>(null)
const fileState = reactive<
  Record<string, { uploading?: boolean; uploaded?: ToolFileUpload; error?: string }>
>({})

// ── 提交态 ───────────────────────────────────────────────────────────────────
const submitting = ref(false)
const response = ref<ToolInvokeResponse | null>(null)

// ── 调用历史（仅本人可见，agent 级：覆盖该插件全部 function） ─────────────────
const history = ref<InvocationHistoryItem[]>([])
const historyLoading = ref(false)
const historyError = ref<string | null>(null)
const historyOpen = ref(true)
const expandedHistoryId = ref<string | null>(null)

async function loadHistory() {
  historyLoading.value = true
  historyError.value = null
  try {
    history.value = await externalAgentFunctionApi.listInvocations(agentId, 10)
  } catch {
    // 历史加载失败不打断表单主流程，只在区块内提示
    historyError.value = t('pluginForm.history.loadFailed')
  } finally {
    historyLoading.value = false
  }
}

function toggleHistoryItem(id: string) {
  expandedHistoryId.value = expandedHistoryId.value === id ? null : id
}

/** 历史项的完整响应（result_data 即 invoke 响应），交给 PluginResult 重放。 */
function historyResponse(item: InvocationHistoryItem): ToolInvokeResponse | null {
  return (item.result_data as unknown as ToolInvokeResponse) ?? null
}

function historyDisplay(item: InvocationHistoryItem): 'table' | 'json' | 'text' {
  const d = item.result_data?.display
  return d === 'table' || d === 'text' ? d : 'json'
}

function historyItemsPath(item: InvocationHistoryItem): string | null {
  const p = item.result_data?.items_path
  return typeof p === 'string' ? p : null
}

function formatHistoryTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function truncateSummary(text: string | null, max = 120): string {
  if (!text) return ''
  return text.length > max ? text.slice(0, max) + '...' : text
}

// ── 初始化：先拉 function 列表，再选默认 + 拉表单；历史并行加载 ───────────────
onMounted(async () => {
  await loadFunctions()
  void loadHistory()
})

async function loadFunctions() {
  functionsLoading.value = true
  topLevelError.value = null
  try {
    const list = await externalAgentFunctionApi.list(agentId)
    functions.value = list
    if (list.length === 0) {
      topLevelError.value = t('pluginForm.noFunctions')
      return
    }
    // 默认选中第一个 active 功能；若一个都没有（全部 draft/disabled），
    // 不去调 form 接口（会 403），改为展示"去管理页启用"引导态
    const initial = list.find((f) => f.status === 'active')
    if (!initial) {
      noActiveFunction.value = true
      return
    }
    await selectFunction(initial.id)
  } catch (e: unknown) {
    topLevelError.value = apiErrorMessage(e)
  } finally {
    functionsLoading.value = false
  }
}

async function selectFunction(functionId: string) {
  if (functionId === selectedFunctionId.value) return
  // 未启用的功能点开只会 403，前端直接给出引导提示，不发请求
  const target = functions.value.find((f) => f.id === functionId)
  if (target && target.status !== 'active') {
    topLevelError.value = t('pluginForm.functionNotActiveHint')
    return
  }
  selectedFunctionId.value = functionId
  loadingFunctionId.value = functionId
  // 清掉旧表单状态（review §8.2：切换 function 即清 values/errors/response/file）
  for (const k of Object.keys(values)) delete values[k]
  for (const k of Object.keys(fileState)) fileState[k] = {}
  fieldErrors.value = {}
  topLevelError.value = null
  enableGuidanceMessage.value = null
  response.value = null
  form.value = null
  try {
    const fn = await externalAgentFunctionApi.getForm(agentId, functionId)
    form.value = toToolForm(fn)
    // 用 schema.default 预填（review §8.2：spec §4.1 已贯通）
    if (fn.input_schema?.fields) {
      for (const [key, def] of Object.entries(fn.input_schema.fields)) {
        if (def && (def as any).default !== undefined) {
          values[key] = (def as any).default
        }
      }
    }
  } catch (e: unknown) {
    topLevelError.value = handleFormError(e)
  } finally {
    loadingFunctionId.value = null
  }
}

// 当用户拿到新表单时，清掉旧响应
watch(form, () => {
  response.value = null
  fieldErrors.value = {}
  topLevelError.value = null
})

// ── 控件渲染 dispatch ─────────────────────────────────────────────────────────
//
// spec §4 / §7.2 字段类型 + UI 映射表：
//
// | ui          | 控件类型                         | 适用 type          |
// | input       | <input type=text>                | string             |
// | textarea    | <textarea>                       | string             |
// | number      | <input type=number>              | number             |
// | select      | <CustomSelect>                   | string + options   |
// | switch      | <input type=checkbox> + label    | boolean            |
// | date        | <input type=date>                | string (ISO)       |
// | daterange   | 两个 <input type=date>           | object {start,end} |
// | upload      | 自定义文件按钮                   | file               |
//
// 字段 key 即输入值键名（撞名时按 spec 仅以 key 为准；type+ui 用于决定控件即可）。

function updateValue(key: string, value: any) {
  values[key] = value
  // 用户编辑后清掉该字段先前的校验错误
  if (fieldErrors.value[key]) {
    const next = { ...fieldErrors.value }
    delete next[key]
    fieldErrors.value = next
  }
}

function getDateRangeValue(key: string): { start: string; end: string } {
  const v = values[key]
  if (v && typeof v === 'object' && ('start' in v || 'end' in v)) {
    return { start: v.start ?? '', end: v.end ?? '' }
  }
  return { start: '', end: '' }
}

function setDateRangeValue(key: string, part: 'start' | 'end', val: string) {
  const cur = getDateRangeValue(key)
  cur[part] = val
  updateValue(key, { start: cur.start || undefined, end: cur.end || undefined })
}

// ── 文件上传（§8 中转：先 POST /functions/{id}/files 拿 file_id，invoke 时引用 file_id）───
const fileInputRefs = reactive<Record<string, HTMLInputElement | null>>({})

function setFileInputRef(key: string) {
  return (el: any) => {
    if (el) fileInputRefs[key] = el
  }
}

function openFilePicker(key: string) {
  fileInputRefs[key]?.click()
}

function acceptAttrFor(field: ToolInputField): string | undefined {
  if (!field.accept?.length) return undefined
  return field.accept.join(',')
}

function validateLocalFile(field: ToolInputField, file: File): string | null {
  if (field.max_mb && file.size > field.max_mb * 1024 * 1024) {
    return t('pluginForm.fileTooLarge', { max: field.max_mb })
  }
  if (field.accept?.length) {
    const ext = '.' + (file.name.split('.').pop() || '').toLowerCase()
    const ok = field.accept.some((a) => {
      const lower = a.toLowerCase()
      if (lower.startsWith('.')) return lower === ext
      // mime type (e.g. "image/*") match
      if (lower.endsWith('/*')) {
        const prefix = lower.slice(0, -1)
        return file.type.startsWith(prefix)
      }
      return lower === file.type
    })
    if (!ok) return t('pluginForm.fileTypeNotAllowed')
  }
  return null
}

async function handleFileChange(field: ToolInputField, e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  // 重置 input.value 以支持重复选同名文件
  input.value = ''
  if (!file) return
  if (!selectedFunctionId.value) return
  const validation = validateLocalFile(field, file)
  if (validation) {
    fileState[field.key] = { uploading: false, error: validation }
    return
  }
  fileState[field.key] = { uploading: true }
  try {
    const uploaded = await externalAgentFunctionApi.uploadFile(
      agentId,
      selectedFunctionId.value,
      file,
    )
    fileState[field.key] = { uploading: false, uploaded }
    // file 字段的表单值即 file_id（参 §6.2 第 3 步：file 字段按 pass_mode 转发）
    updateValue(field.key, uploaded.file_id)
  } catch (err: unknown) {
    fileState[field.key] = {
      uploading: false,
      error: err instanceof Error ? err.message : t('pluginForm.errors.networkError'),
    }
  }
}

function clearFile(key: string) {
  fileState[key] = {}
  updateValue(key, undefined)
}

// ── 提交 ────────────────────────────────────────────────────────────────────────

async function submit() {
  if (!form.value || !selectedFunctionId.value) return
  topLevelError.value = null
  fieldErrors.value = {}
  response.value = null
  submitting.value = true
  try {
    // 校验：本地只做最小防御，必填校验完全后端（spec §6.2 第 2 步 pydantic 动态校验）
    const missing = fields.value.filter(
      (f) =>
        f.required &&
        (values[f.key] === undefined ||
          values[f.key] === '' ||
          values[f.key] === null ||
          (f.ui === 'daterange' &&
            (!values[f.key] || (!values[f.key].start && !values[f.key].end)))),
    )
    if (missing.length) {
      const next: Record<string, string> = {}
      for (const m of missing) next[m.key] = t('pluginForm.required')
      fieldErrors.value = next
      topLevelError.value = t('pluginForm.errors.submitFailed')
      return
    }

    // 上传中的文件不允许提交
    const inflight = Object.entries(fileState).filter(([, s]) => s.uploading)
    if (inflight.length) {
      topLevelError.value = t('pluginForm.errors.missingFile')
      return
    }

    const payload: Record<string, any> = {}
    for (const f of fields.value) {
      // 不上传的 file 字段不要进 body
      if (f.ui === 'upload' && !values[f.key]) continue
      if (values[f.key] !== undefined) payload[f.key] = values[f.key]
    }

    const res = await externalAgentFunctionApi.invoke(
      agentId,
      selectedFunctionId.value,
      payload,
    )
    response.value = res
    if (res.success === true) {
      // 成功后清除字段级错误（防 stale）
      fieldErrors.value = {}
    }
    // 成功 / 上游失败（HTTP 200 + success=false）后端都会落一条调用历史，
    // 两种情况都刷新历史列表（422/503 异常路径不落历史，无需刷新）
    void loadHistory()
  } catch (err: any) {
    const detail = err?.response?.data?.detail ?? err?.response?.data ?? null
    // 字段级错误映射
    const fetched: Record<string, string> | undefined =
      detail?.field_errors ?? detail?.data?.field_errors ?? err?.response?.data?.field_errors
    if (fetched && typeof fetched === 'object') {
      // 后端字段错误来自 pydantic 英文文案，翻译成中文再展示；
      // 兼容 string / {message} / [{message}] 三种形态
      const translated: Record<string, string> = {}
      for (const [k, v] of Object.entries(fetched as Record<string, unknown>)) {
        let msg = ''
        if (typeof v === 'string') msg = v
        else if (Array.isArray(v) && v.length) {
          const first: any = v[0]
          msg = typeof first === 'string' ? first : (first?.message ?? first?.msg ?? '')
        } else if (v && typeof v === 'object') {
          msg = (v as any).message ?? (v as any).msg ?? ''
        }
        translated[k] = translateFieldError(msg)
      }
      fieldErrors.value = translated
    }
    const upstreamStatus: number | undefined =
      err?.response?.status ?? detail?.upstream_status
    if (err?.response?.status === 422) {
      topLevelError.value = t('pluginForm.errors.validation')
    } else if (upstreamStatus && upstreamStatus >= 502 && upstreamStatus <= 504) {
      topLevelError.value = t('pluginForm.errors.unreachable')
    } else if (err?.response?.status === 403) {
      topLevelError.value = t('pluginForm.errors.functionNotActive')
    } else if (err?.response?.status === 503) {
      topLevelError.value = t('pluginForm.errors.unreachable')
    } else if (!err?.response) {
      topLevelError.value = t('pluginForm.errors.networkError')
    } else {
      topLevelError.value = detail?.error ?? detail?.message ?? t('pluginForm.errors.submitFailed')
    }
  } finally {
    submitting.value = false
  }
}

function reset() {
  for (const k of Object.keys(values)) delete values[k]
  for (const k of Object.keys(fileState)) fileState[k] = {}
  fieldErrors.value = {}
  topLevelError.value = null
  response.value = null
  // 用 schema.default 重新预填
  if (form.value?.input_schema?.fields) {
    for (const [key, def] of Object.entries(form.value.input_schema.fields)) {
      if (def && (def as any).default !== undefined) {
        values[key] = (def as any).default
      }
    }
  }
}

function retry() {
  response.value = null
  topLevelError.value = null
  // 上一次成功后，用户主动点 retry 时只是清掉结果，不会自动重新提交。
}

// 提供给 PluginResult 用于渲染 display/json 决策
const display = computed<'table' | 'json' | 'text'>(() => form.value?.output_hint?.display ?? 'json')
const itemsPath = computed(() => form.value?.output_hint?.items_path ?? null)

// ── 选项统一映射：CustomSelect 需要 { value, label }[] ─────────────────────────
function selectOptionsFor(field: ToolInputField) {
  const opts = field.options ?? []
  return opts.map((o) => ({ value: o, label: o }))
}

// 是否展示 function 选择器：仅当多 function 时（review C5：单 function 保持现状 UX）
const showFunctionSelector = computed(() => functions.value.length > 1)

// 当前选中的 function 元信息（用于头部展示）
const selectedFunction = computed(() =>
  functions.value.find((f) => f.id === selectedFunctionId.value) ?? null,
)
</script>

<template>
  <div class="max-w-4xl mx-auto px-6 py-8 space-y-6">
    <!-- 头部 -->
    <header class="flex items-center gap-3">
      <button
        type="button"
        class="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        @click="router.push('/agents')"
      >
        <ChevronLeft class="w-4 h-4" />
        {{ t('pluginForm.back') }}
      </button>
      <!-- 标题用中文说明（OpenAPI summary），无说明才回退功能名；不再显示 "/英文名" 面包屑 -->
      <h1
        v-if="form"
        class="flex items-center gap-2 text-xl font-semibold text-foreground"
      >
        <Bot class="w-6 h-6 text-primary" />
        <span>{{ form.description || form.name }}</span>
        <span class="text-xs px-1.5 py-0.5 rounded bg-secondary text-secondary-foreground font-normal">
          v{{ form.version }}
        </span>
      </h1>
      <h1 v-else class="text-xl font-semibold text-foreground">
        {{ t('pluginForm.title') }}
      </h1>
    </header>

    <!-- Phase 2 §8.2：function 选择器（仅多 function 时渲染，单 function 保持原 UX） -->
    <div v-if="showFunctionSelector" class="function-selector">
      <div class="flex items-center gap-2 text-xs text-muted-foreground mb-2">
        <span>{{ t('pluginForm.functionSelector.label') }}</span>
      </div>
      <div class="flex flex-wrap gap-2 border-b border-border">
        <button
          v-for="fn in functions"
          :key="fn.id"
          type="button"
          :class="[
            'inline-flex items-center gap-1 px-3 py-2 text-sm border-b-2 -mb-px transition-colors',
            fn.id === selectedFunctionId
              ? 'border-primary text-primary font-medium'
              : 'border-transparent text-muted-foreground hover:text-foreground',
            fn.status !== 'active' && 'opacity-60 cursor-not-allowed',
          ]"
          :disabled="loadingFunctionId === fn.id"
          :data-test="`function-tab-${fn.id}`"
          @click="selectFunction(fn.id)"
        >
          <Loader2 v-if="loadingFunctionId === fn.id" class="w-3.5 h-3.5 animate-spin" />
          <span>{{ fn.summary || fn.name }}</span>
          <span
            v-if="fn.status !== 'active'"
            class="ml-1 text-[10px] px-1 py-0.5 rounded bg-muted text-muted-foreground"
          >
            {{ t('pluginForm.functionStatus.inactive') }}
          </span>
        </button>
      </div>
    </div>

    <!-- 插件/功能未启用（403 not_active）：后端文案 + 去管理页启用按钮 -->
    <div
      v-if="enableGuidanceMessage && !functionsLoading"
      class="rounded-lg border border-amber-500/40 bg-amber-500/5 p-5 flex flex-col items-center gap-3 text-center"
      data-testid="enable-guidance"
    >
      <CircleAlert class="w-6 h-6 text-amber-500" />
      <p class="text-sm text-foreground">{{ enableGuidanceMessage }}</p>
      <button
        type="button"
        class="inline-flex items-center gap-1 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90"
        @click="router.push({ name: 'ExternalAgentEdit', params: { id: agentId } })"
      >
        {{ t('pluginForm.goManageEnable') }}
      </button>
    </div>

    <!-- 全部功能未启用：引导去管理页启用（不裸调 form 接口） -->
    <div
      v-if="noActiveFunction && !functionsLoading"
      class="rounded-lg border border-amber-500/40 bg-amber-500/5 p-5 flex flex-col items-center gap-3 text-center"
      data-testid="no-active-function"
    >
      <CircleAlert class="w-6 h-6 text-amber-500" />
      <p class="text-sm text-foreground">{{ t('pluginForm.noActiveFunction') }}</p>
      <p class="text-xs text-muted-foreground">{{ t('pluginForm.noActiveFunctionHint') }}</p>
      <button
        type="button"
        class="inline-flex items-center gap-1 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90"
        @click="router.push({ name: 'ExternalAgentEdit', params: { id: agentId } })"
      >
        {{ t('pluginForm.goManageEnable') }}
      </button>
    </div>

    <!-- 加载/失败态 -->
    <div
      v-if="topLevelError && !form"
      class="rounded-lg border border-destructive/40 bg-destructive/5 p-4 flex items-start gap-2"
    >
      <CircleAlert class="w-5 h-5 text-destructive shrink-0 mt-0.5" />
      <p class="text-sm text-destructive">{{ topLevelError }}</p>
    </div>
    <div
      v-else-if="functionsLoading && !form"
      class="flex items-center gap-2 text-sm text-muted-foreground"
    >
      <Loader2 class="w-4 h-4 animate-spin" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- 描述已并入头部标题（中文说明即标题），不再单独渲染一行，避免重复 -->

    <!-- 表单 -->
    <form
      v-if="form"
      class="space-y-5 rounded-xl border border-border bg-card p-5"
      @submit.prevent="submit"
    >
      <div
        v-for="field in fields"
        :key="field.key"
        class="space-y-1.5"
      >
        <label
          class="text-xs text-muted-foreground flex items-center gap-1.5"
          :for="`tool-field-${field.key}`"
        >
          <span>{{ field.label || field.key }}</span>
          <span v-if="field.required" class="text-destructive">*</span>
        </label>
        <!-- Phase 2 §4.3：字段作用说明。OpenAPI 导入时 label 与 description 同源，
             相同时只显示一行，避免同一句话渲染两遍 -->
        <p
          v-if="field.description && field.description !== (field.label || field.key)"
          class="text-xs text-muted-foreground mt-0.5"
        >
          {{ field.description }}
        </p>

        <!-- input / textarea / number / date（统一样式） -->
        <input
          v-if="field.ui === 'input' && field.type !== 'number'"
          :id="`tool-field-${field.key}`"
          type="text"
          :value="values[field.key] ?? ''"
          :placeholder="field.placeholder || t('pluginForm.placeholder')"
          class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
          @input="updateValue(field.key, ($event.target as HTMLInputElement).value)"
        />
        <input
          v-else-if="field.ui === 'number' || (field.type === 'number' && field.ui === 'input')"
          :id="`tool-field-${field.key}`"
          type="number"
          :value="values[field.key] ?? ''"
          :placeholder="field.placeholder || t('pluginForm.placeholder')"
          class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
          @input="updateValue(field.key, ($event.target as HTMLInputElement).value)"
        />
        <textarea
          v-else-if="field.ui === 'textarea'"
          :id="`tool-field-${field.key}`"
          rows="4"
          :value="values[field.key] ?? ''"
          :placeholder="field.placeholder || t('pluginForm.placeholder')"
          class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50 resize-y"
          @input="updateValue(field.key, ($event.target as HTMLTextAreaElement).value)"
        />
        <CustomSelect
          v-else-if="field.ui === 'select'"
          :model-value="values[field.key] ?? null"
          :options="selectOptionsFor(field)"
          trigger-class="w-full"
          @update:model-value="updateValue(field.key, $event)"
        />
        <label v-else-if="field.ui === 'switch'" class="flex items-center gap-2 cursor-pointer">
          <input
            :id="`tool-field-${field.key}`"
            type="checkbox"
            :checked="!!values[field.key]"
            class="accent-primary"
            @change="updateValue(field.key, ($event.target as HTMLInputElement).checked)"
          />
          <span class="text-sm text-foreground">
            {{ values[field.key] ? t('pluginForm.booleanTrue') : t('pluginForm.booleanFalse') }}
          </span>
        </label>
        <input
          v-else-if="field.ui === 'date'"
          :id="`tool-field-${field.key}`"
          type="date"
          :value="values[field.key] ?? ''"
          class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
          @input="updateValue(field.key, ($event.target as HTMLInputElement).value)"
        />
        <div v-else-if="field.ui === 'daterange'" class="grid grid-cols-2 gap-2">
          <label class="space-y-1">
            <span class="text-[10px] text-muted-foreground block">{{ t('pluginForm.dateStart') }}</span>
            <input
              :id="`tool-field-${field.key}-start`"
              type="date"
              :value="getDateRangeValue(field.key).start"
              class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
              @input="setDateRangeValue(field.key, 'start', ($event.target as HTMLInputElement).value)"
            />
          </label>
          <label class="space-y-1">
            <span class="text-[10px] text-muted-foreground block">{{ t('pluginForm.dateEnd') }}</span>
            <input
              type="date"
              :value="getDateRangeValue(field.key).end"
              class="w-full px-3 py-1.5 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
              @input="setDateRangeValue(field.key, 'end', ($event.target as HTMLInputElement).value)"
            />
          </label>
        </div>
        <div v-else-if="field.ui === 'upload'" class="space-y-1.5">
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-accent"
              :disabled="fileState[field.key]?.uploading || !selectedFunction"
              @click="openFilePicker(field.key)"
            >
              <Upload v-if="!fileState[field.key]?.uploading" class="w-3.5 h-3.5" />
              <Loader2 v-else class="w-3.5 h-3.5 animate-spin" />
              {{ fileState[field.key]?.uploading ? t('pluginForm.uploadingFile') : t('pluginForm.uploadFile') }}
            </button>
            <input
              :ref="setFileInputRef(field.key)"
              type="file"
              class="hidden"
              :accept="acceptAttrFor(field)"
              @change="handleFileChange(field, $event)"
            />
            <div
              v-if="fileState[field.key]?.uploaded"
              class="flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <FileText class="w-3.5 h-3.5" />
              <span class="truncate max-w-[200px]">
                {{ t('pluginForm.uploadedFile', { name: fileState[field.key]!.uploaded!.name }) }}
              </span>
              <button
                type="button"
                class="text-muted-foreground hover:text-destructive"
                @click="clearFile(field.key)"
              >
                <X class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <p v-if="fileState[field.key]?.error" class="text-xs text-destructive">
            {{ fileState[field.key]!.error }}
          </p>
        </div>
        <!-- 字段级错误回显 -->
        <p
          v-if="fieldErrors[field.key]"
          class="text-xs text-destructive flex items-center gap-1"
        >
          <CircleAlert class="w-3 h-3" />
          {{ fieldErrors[field.key] }}
        </p>
      </div>

      <div
        v-if="topLevelError"
        class="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
      >
        {{ topLevelError }}
      </div>

      <div class="flex items-center gap-2 pt-2 border-t border-border">
        <button
          type="submit"
          class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          :disabled="submitting"
        >
          <Loader2 v-if="submitting" class="w-4 h-4 animate-spin" />
          <RefreshCw v-else class="w-4 h-4" />
          {{ submitting ? t('pluginForm.submitting') : t('pluginForm.submit') }}
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg border border-border text-foreground px-3 py-2 text-sm hover:bg-accent"
          :disabled="submitting"
          @click="reset"
        >
          {{ t('pluginForm.reset') }}
        </button>
      </div>
    </form>

    <!-- 结果展示：上下布局（表单在上，结果在下）；大结果（长文本/宽表）内滚动不撑爆页面 -->
    <section v-if="response" class="rounded-xl border border-border bg-card">
      <div class="flex items-center gap-2 px-4 py-2.5 border-b border-border">
        <FileText class="w-4 h-4 text-primary" />
        <h3 class="text-sm font-medium text-foreground">{{ t('pluginForm.resultTitle') }}</h3>
      </div>
      <div class="p-4 max-h-[70vh] overflow-auto" data-testid="invoke-result-area">
        <PluginResult
          :response="response"
          :display="display"
          :items-path="itemsPath"
          :field-errors="fieldErrors"
          :show-retry="response?.success === false"
          @retry="retry"
        />
      </div>
    </section>

    <!-- 调用历史：可折叠卡片，当前用户在该插件下的最近调用（仅本人可见） -->
    <section class="rounded-xl border border-border bg-card" data-testid="invoke-history">
      <button
        type="button"
        class="flex w-full items-center gap-2 px-4 py-2.5 text-left border-b border-border"
        data-testid="invoke-history-toggle"
        @click="historyOpen = !historyOpen"
      >
        <Clock class="w-4 h-4 text-primary" />
        <h3 class="flex-1 text-sm font-medium text-foreground">
          {{ t('pluginForm.history.title') }}
        </h3>
        <ChevronUp v-if="historyOpen" class="w-4 h-4 text-muted-foreground" />
        <ChevronDown v-else class="w-4 h-4 text-muted-foreground" />
      </button>
      <div v-if="historyOpen">
        <div
          v-if="historyLoading"
          class="flex items-center gap-2 px-4 py-3 text-sm text-muted-foreground"
        >
          <Loader2 class="w-4 h-4 animate-spin" />
          <span>{{ t('common.loading') }}</span>
        </div>
        <p
          v-else-if="historyError"
          class="px-4 py-3 text-xs text-destructive"
          data-testid="invoke-history-error"
        >
          {{ historyError }}
        </p>
        <p
          v-else-if="history.length === 0"
          class="px-4 py-6 text-center text-sm text-muted-foreground"
          data-testid="invoke-history-empty"
        >
          {{ t('pluginForm.history.empty') }}
        </p>
        <ul v-else class="divide-y divide-border" data-testid="invoke-history-list">
          <li v-for="item in history" :key="item.id">
            <button
              type="button"
              class="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-accent"
              :data-testid="`invoke-history-item-${item.id}`"
              @click="toggleHistoryItem(item.id)"
            >
              <span
                class="inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-medium"
                :class="item.success
                  ? 'bg-emerald-500/10 text-emerald-600'
                  : 'bg-destructive/10 text-destructive'"
              >
                {{ item.success ? t('pluginForm.history.success') : t('pluginForm.history.failed') }}
              </span>
              <span class="shrink-0 text-xs text-muted-foreground">
                {{ formatHistoryTime(item.created_at) }}
              </span>
              <span class="shrink-0 text-xs text-foreground">{{ item.function_name }}</span>
              <span class="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                {{ truncateSummary(item.params_summary) }}
              </span>
              <span
                v-if="item.latency_ms != null"
                class="shrink-0 text-[10px] text-muted-foreground"
              >
                {{ item.latency_ms }}ms
              </span>
              <ChevronDown
                class="w-3.5 h-3.5 shrink-0 text-muted-foreground transition-transform"
                :class="expandedHistoryId === item.id && 'rotate-180'"
              />
            </button>
            <!-- 展开重放：result_data 即当时的完整 invoke 响应 -->
            <div
              v-if="expandedHistoryId === item.id"
              class="space-y-2 px-4 pb-4"
              :data-testid="`invoke-history-detail-${item.id}`"
            >
              <p class="text-xs text-muted-foreground">
                {{
                  item.result_data?.truncated
                    ? t('pluginForm.history.truncated')
                    : t('pluginForm.history.clickToView')
                }}
              </p>
              <p v-if="item.error_message" class="text-xs text-destructive">
                {{ item.error_message }}
              </p>
              <div class="max-h-[50vh] overflow-auto rounded-md border border-border bg-background p-3">
                <PluginResult
                  :response="historyResponse(item)"
                  :display="historyDisplay(item)"
                  :items-path="historyItemsPath(item)"
                  :show-retry="false"
                />
              </div>
            </div>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>