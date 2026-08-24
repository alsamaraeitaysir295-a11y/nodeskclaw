<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  AlertCircle,
  Bot,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  Power,
  Save,
  Trash2,
  Wifi,
  X,
} from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import {
  externalAgentApi,
  externalAgentFunctionApi,
  externalAgentPluginApi,
  PLUGIN_FIELD_UIS,
  type ExternalAgent,
  type ExternalAgentFunction,
  type OpenapiImportConfirmRequest,
} from '@/services/externalAgents'
import ExternalAgentImportPanel from './ExternalAgentImportPanel.vue'

/**
 * 外部智能体插件：创建向导 + 管理页共用本组件。
 *  - /org-settings/external-agents/new      → 创建向导（三步：基本信息 → OpenAPI 导入 → 导入并试调）
 *  - /org-settings/external-agents/:id/edit → 管理页（基本信息 / 插件状态 / 功能管理 + 追加导入）
 *
 * 新建插件一律 tool 型（后端 import confirm 创建 type=tool，前端不再提交类型字段）。
 * 手动配置模式与类型选择已下线，接口能力统一由 OpenAPI 导入产生。
 */
const { t } = useI18n()
const router = useRouter()
const route = useRoute()

const routeId = typeof route.params.id === 'string' ? route.params.id : undefined
const isEdit = !!routeId

// ═══════════════════════════════════════════════════════════════════════════
// 创建向导（!isEdit）
// ═══════════════════════════════════════════════════════════════════════════

const currentStep = ref<1 | 2 | 3>(1)
const error = ref<string | null>(null)
const stepError = ref<string | null>(null)
const saving = ref(false)
const probing = ref(false)

const wizard = reactive({
  name: '',
  description: '',
  icon_emoji: '',
  theme_color: '#7c3aed',
})

// 导入面板（第 2 步）。step 2 用 v-show 渲染，跨步骤切换不丢解析状态。
const wizardPanel = ref<InstanceType<typeof ExternalAgentImportPanel> | null>(null)

const step1Valid = computed(() => !!wizard.name.trim())
const step2ImportValid = computed(() => wizardPanel.value?.checkValid() ?? false)

// 第 3 步：导入产物与逐功能试调结果
const importedAgentId = ref<string | null>(null)
const importProbeResults = ref<Array<{ name: string; ok: boolean; latency?: number; error?: string }>>([])

const STEP_LABELS = computed(() => ({
  1: t('externalAgentWizard.steps.basic'),
  2: t('externalAgentWizard.steps.openapi'),
  3: t('externalAgentWizard.steps.importProbe'),
}))

function goNext() {
  stepError.value = null
  if (currentStep.value === 1) {
    if (!step1Valid.value) {
      stepError.value = t('externalAgentWizard.basic.requiredHint')
      return
    }
    currentStep.value = 2
  } else if (currentStep.value === 2) {
    if (!step1Valid.value || !step2ImportValid.value) {
      stepError.value = t('externalAgentWizard.import.confirmHint')
      return
    }
    currentStep.value = 3
  }
}

function goPrev() {
  stepError.value = null
  if (currentStep.value > 1) currentStep.value = (currentStep.value - 1) as 1 | 2 | 3
}

function gotoStep(step: 1 | 2 | 3) {
  // Allow forward navigation only if previous steps valid; backward always allowed
  if (step === 2 && !step1Valid.value) return
  if (step === 3) {
    if (!step1Valid.value || !step2ImportValid.value) return
  }
  currentStep.value = step
}

/** 创建模式 confirm body：基本信息 + 导入面板载荷。 */
function buildWizardConfirmBody(): OpenapiImportConfirmRequest | null {
  const payload = wizardPanel.value?.buildPayload()
  if (!payload) return null
  const body: OpenapiImportConfirmRequest = {
    name: wizard.name.trim(),
    auth: payload.auth,
    selected: payload.selected,
  }
  if (wizard.description.trim()) body.description = wizard.description.trim()
  if (wizard.icon_emoji.trim()) body.icon_emoji = wizard.icon_emoji.trim()
  if (wizard.theme_color.trim()) body.theme_color = wizard.theme_color.trim()
  if (payload.doc_url) body.doc_url = payload.doc_url
  else if (payload.doc) body.doc = payload.doc
  return body
}

/**
 * 第 3 步「导入并逐功能试调」：confirm 一次（创建插件 + functions，draft），
 * 再对每个 function 发起一次真实 probe 并逐行展示结果。
 * 注意不要把 confirm body 喂给 validateManifest——它不是 PluginManifest 形态。
 */
async function runImportAndProbe() {
  error.value = null
  stepError.value = null
  const body = buildWizardConfirmBody()
  if (!body) {
    stepError.value = t('externalAgentWizard.import.confirmHint')
    return
  }
  probing.value = true
  try {
    const resp = await externalAgentPluginApi.importOpenapiConfirm(body)
    importedAgentId.value = resp.agent.id
    importProbeResults.value = []
    for (const fn of resp.functions || []) {
      try {
        const r = await externalAgentFunctionApi.probe(resp.agent.id, fn.id)
        importProbeResults.value.push({
          name: fn.name,
          ok: Boolean(r && (r.reachable ?? r.ok)),
          latency: r?.latency_ms ?? undefined,
          error: r?.error ?? undefined,
        })
      } catch (e: unknown) {
        importProbeResults.value.push({
          name: fn.name,
          ok: false,
          error: e instanceof Error ? e.message : undefined,
        })
      }
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
  } finally {
    probing.value = false
  }
}

/** 「仅导入（草稿）」/「前往编辑页」：导入过则直接跳编辑页（防重复导入守卫）。 */
async function importOnly() {
  error.value = null
  stepError.value = null
  if (importedAgentId.value) {
    router.push({ name: 'ExternalAgentEdit', params: { id: importedAgentId.value } })
    return
  }
  const body = buildWizardConfirmBody()
  if (!body) {
    stepError.value = t('externalAgentWizard.import.confirmHint')
    return
  }
  saving.value = true
  try {
    const resp = await externalAgentPluginApi.importOpenapiConfirm(body)
    saving.value = false
    router.push({ name: 'ExternalAgentEdit', params: { id: resp.agent.id } })
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
    saving.value = false
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 管理页（isEdit）：基本信息 / 插件状态 / 功能管理 + 追加导入
// ═══════════════════════════════════════════════════════════════════════════

const agent = ref<ExternalAgent | null>(null)
const functions = ref<ExternalAgentFunction[]>([])
const mgmtLoading = ref(false)
const mgmtError = ref<string | null>(null)

const mgmtForm = reactive({
  name: '',
  description: '',
  icon_emoji: '',
  theme_color: '#7c3aed',
})
const savingBasic = ref(false)
const basicSaved = ref(false)
const basicError = ref<string | null>(null)

const statusUpdating = ref(false)
const statusError = ref<string | null>(null)

// 追加导入（添加功能）
const mgmtImportOpen = ref(false)
const appending = ref(false)
const appendError = ref<string | null>(null)
const appendSuccessCount = ref<number | null>(null)
// 追加去重反馈：confirm 响应中被跳过（已存在，按 method+path 去重）的 operation 数
const appendSkippedCount = ref<number | null>(null)
const mgmtPanel = ref<InstanceType<typeof ExternalAgentImportPanel> | null>(null)

// 功能行级操作
const probingFunctionId = ref<string | null>(null)
const probeResults = reactive<Record<string, { ok: boolean; latency?: number; error?: string }>>({})
const updatingFunctionId = ref<string | null>(null)
const deletingFunctionId = ref<string | null>(null)
const confirmingDeleteId = ref<string | null>(null)
const fnActionError = ref<string | null>(null)
const enablingAll = ref(false)

// ── 单功能字段编辑器（一次只展开一个功能） ─────────────────────────────
/** 字段编辑草稿：orig 保存后端原始定义，保存时仅覆盖可编辑属性。 */
interface FnFieldDraft {
  key: string
  type: string
  ui: string
  label: string
  description: string
  required: boolean
  /** string/number/select 默认值文本草稿（'' = 清除默认值） */
  text: string
  /** object 默认值的 JSON 文本草稿 */
  jsonText: string
  jsonError: string | null
  /** boolean 默认值：仅用户改动后写入，避免把「无默认值」误存成 false */
  boolValue: boolean
  boolDirty: boolean
  orig: Record<string, any>
}

const editingFunctionId = ref<string | null>(null)
const fnEditSaving = ref(false)
const fnEditError = ref<string | null>(null)
const fnUpdateSaved = ref(false)
const fnEdit = reactive<{
  summary: string
  sortOrder: number
  order: string[]
  fields: FnFieldDraft[]
}>({
  summary: '',
  sortOrder: 0,
  order: [],
  fields: [],
})

const fnEditHasJsonError = computed(() => fnEdit.fields.some((d) => !!d.jsonError))

const sortedFunctions = computed(() =>
  [...functions.value].sort((a, b) => a.sort_order - b.sort_order),
)

/** 是否存在未启用功能（决定「全部启用」按钮显隐）。 */
const hasInactiveFunctions = computed(() =>
  functions.value.some((f) => f.status !== 'active'),
)

/** 一键启用全部 draft/disabled 功能（逐个 PATCH，完成后刷新列表）。 */
async function enableAllFunctions() {
  if (!routeId) return
  fnActionError.value = null
  enablingAll.value = true
  let enabled = 0
  try {
    for (const f of functions.value) {
      if (f.status === 'active') continue
      await externalAgentFunctionApi.update(routeId, f.id, { status: 'active' })
      enabled += 1
    }
    await loadFunctions()
    if (enabled > 0) {
      appendSuccessCount.value = enabled
      appendSkippedCount.value = null
    }
  } catch (e: unknown) {
    fnActionError.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
  } finally {
    enablingAll.value = false
  }
}

/** 追加导入预览的「已存在」标记数据：来自各功能 origin_meta 的 method+path。 */
const existingOps = computed<Array<{ method: string; path: string }>>(() => {
  const ops: Array<{ method: string; path: string }> = []
  for (const f of functions.value) {
    const om = f.origin_meta
    if (om && om.method && om.path) {
      ops.push({ method: String(om.method).toUpperCase(), path: String(om.path) })
    }
  }
  return ops
})

/** method+path 优先取 openapi 原始引用，兜底走 invoke_config.endpoint。 */
function functionMethodPath(f: ExternalAgentFunction): string {
  const origin = f.origin_meta
  if (origin && (origin.method || origin.path)) {
    const method = String(origin.method ?? '').toUpperCase() || 'GET'
    return `${method} ${String(origin.path ?? '')}`.trim()
  }
  const endpoint = f.invoke_config?.endpoint
  return endpoint ? String(endpoint) : ''
}

function statusBadgeClass(status?: string): string {
  if (status === 'active') return 'bg-green-100 text-green-700'
  if (status === 'disabled') return 'bg-gray-100 text-gray-500'
  return 'bg-amber-100 text-amber-700'
}

function statusBadgeLabel(status?: string): string {
  return t(`externalAgentWizard.status.${status ?? 'draft'}`)
}

async function loadFunctions() {
  if (!routeId) return
  functions.value = await externalAgentFunctionApi.list(routeId)
}

async function loadManagement() {
  if (!routeId) return
  mgmtLoading.value = true
  mgmtError.value = null
  try {
    const agents = await externalAgentApi.list()
    const found = agents.find((a) => a.id === routeId)
    if (!found) {
      mgmtError.value = t('externalAgentWizard.loadFailed')
      return
    }
    agent.value = found
    mgmtForm.name = found.name
    mgmtForm.description = found.description ?? ''
    mgmtForm.icon_emoji = found.icon_emoji ?? ''
    mgmtForm.theme_color = found.theme_color ?? '#7c3aed'
    await loadFunctions()
  } catch {
    mgmtError.value = t('externalAgentWizard.loadFailed')
  } finally {
    mgmtLoading.value = false
  }
}

async function saveBasic() {
  if (!routeId) return
  if (!mgmtForm.name.trim()) {
    basicError.value = t('externalAgentWizard.basic.requiredHint')
    return
  }
  basicError.value = null
  savingBasic.value = true
  try {
    agent.value = await externalAgentApi.update(routeId, {
      name: mgmtForm.name.trim(),
      description: mgmtForm.description.trim() || undefined,
      icon_emoji: mgmtForm.icon_emoji.trim() || undefined,
      theme_color: mgmtForm.theme_color.trim() || undefined,
    })
    basicSaved.value = true
    setTimeout(() => (basicSaved.value = false), 2000)
  } catch (e: unknown) {
    basicError.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
  } finally {
    savingBasic.value = false
  }
}

async function setAgentStatus(status: 'active' | 'disabled') {
  if (!routeId) return
  statusError.value = null
  statusUpdating.value = true
  try {
    agent.value = await externalAgentApi.update(routeId, { status })
  } catch (e: unknown) {
    statusError.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
  } finally {
    statusUpdating.value = false
  }
}

async function probeManagedFunction(f: ExternalAgentFunction) {
  if (!routeId) return
  delete probeResults[f.id]
  probingFunctionId.value = f.id
  try {
    const r = await externalAgentFunctionApi.probe(routeId, f.id)
    probeResults[f.id] = {
      ok: Boolean(r && (r.reachable ?? r.ok)),
      latency: r?.latency_ms ?? undefined,
      error: r?.error ?? undefined,
    }
  } catch (e: unknown) {
    probeResults[f.id] = {
      ok: false,
      error: e instanceof Error ? e.message : undefined,
    }
  } finally {
    probingFunctionId.value = null
  }
}

/** 一键试调全部功能：逐个 probe，行内展示结果 + 汇总统计。 */
const probingAll = ref(false)
const probeAllSummary = ref<string | null>(null)

async function probeAllFunctions() {
  if (!routeId || probingAll.value) return
  probingAll.value = true
  probeAllSummary.value = null
  fnActionError.value = null
  let okCount = 0
  let failCount = 0
  try {
    for (const f of sortedFunctions.value) {
      // 清旧结果再测，让用户看到逐条刷新的过程
      delete probeResults[f.id]
      try {
        const r = await externalAgentFunctionApi.probe(routeId, f.id)
        const ok = Boolean(r && (r.reachable ?? r.ok))
        probeResults[f.id] = {
          ok,
          latency: r?.latency_ms ?? undefined,
          error: r?.error ?? undefined,
        }
        if (ok) okCount++
        else failCount++
      } catch (e: unknown) {
        probeResults[f.id] = {
          ok: false,
          error: e instanceof Error ? e.message : undefined,
        }
        failCount++
      }
    }
    probeAllSummary.value = t('externalAgentWizard.manage.probeAllSummary', {
      ok: okCount, total: okCount + failCount, fail: failCount,
    })
  } finally {
    probingAll.value = false
  }
}

async function toggleFunctionStatus(f: ExternalAgentFunction) {
  if (!routeId) return
  fnActionError.value = null
  confirmingDeleteId.value = null
  updatingFunctionId.value = f.id
  try {
    await externalAgentFunctionApi.update(routeId, f.id, {
      status: f.status === 'active' ? 'disabled' : 'active',
    })
    await loadFunctions()
  } catch (e: unknown) {
    fnActionError.value = extractActionError(e)
  } finally {
    updatingFunctionId.value = null
  }
}

/** 两步确认删除：第一次点击仅进入确认态，第二次才真正调用 DELETE。 */
function requestDeleteFunction(f: ExternalAgentFunction) {
  if (confirmingDeleteId.value !== f.id) {
    confirmingDeleteId.value = f.id
    fnActionError.value = null
    return
  }
  void doDeleteFunction(f)
}

function cancelDeleteFunction() {
  confirmingDeleteId.value = null
}

async function doDeleteFunction(f: ExternalAgentFunction) {
  if (!routeId) return
  fnActionError.value = null
  deletingFunctionId.value = f.id
  try {
    await externalAgentFunctionApi.remove(routeId, f.id)
    confirmingDeleteId.value = null
    await loadFunctions()
  } catch (e: unknown) {
    fnActionError.value = extractActionError(e)
  } finally {
    deletingFunctionId.value = null
  }
}

/** 从 axios 错误中提取可读信息；403（需组织 admin）给专门文案。 */
function extractActionError(e: unknown): string {
  const err = e as {
    response?: { status?: number; data?: { detail?: { message?: string } } }
  }
  if (err?.response?.status === 403) {
    return t('externalAgentWizard.manage.deleteForbidden')
  }
  const msg = err?.response?.data?.detail?.message
  if (msg) return msg
  if (e instanceof Error && e.message) return e.message
  return t('externalAgentWizard.manage.actionFailed')
}

// ── 单功能字段编辑器逻辑 ──────────────────────────────────────────────

/** 展开或收起某功能的字段编辑器；展开时从 input_schema 构建草稿。 */
function startEditFunction(f: ExternalAgentFunction) {
  fnEditError.value = null
  if (editingFunctionId.value === f.id) {
    editingFunctionId.value = null
    return
  }
  const schema = f.input_schema
  const fieldsRaw: Record<string, any> = (schema?.fields as Record<string, any>) ?? {}
  const declared: string[] = Array.isArray(schema?.order) ? schema.order : []
  // order 优先；order 缺失或漏掉的字段按 fields 键序补齐，避免保存时丢字段
  const seen = new Set<string>()
  const keys: string[] = []
  for (const k of [...declared, ...Object.keys(fieldsRaw)]) {
    if (seen.has(k)) continue
    seen.add(k)
    keys.push(k)
  }
  const drafts: FnFieldDraft[] = []
  for (const key of keys) {
    const orig = fieldsRaw[key]
    if (!orig || typeof orig !== 'object') continue
    const type = String(orig.type ?? 'string')
    const v = orig.default
    drafts.push({
      key,
      type,
      ui: String(orig.ui ?? 'input'),
      label: String(orig.label ?? key),
      description: String(orig.description ?? ''),
      required: !!orig.required,
      text: v === undefined || v === null ? '' : type === 'boolean' ? (v ? 'true' : 'false') : String(v),
      jsonText: v === undefined || v === null ? '' : JSON.stringify(v, null, 2),
      jsonError: null,
      boolValue: v === true,
      boolDirty: false,
      orig,
    })
  }
  fnEdit.summary = f.summary ?? ''
  fnEdit.sortOrder = f.sort_order
  fnEdit.order = keys
  fnEdit.fields = drafts
  editingFunctionId.value = f.id
}

/** select 型默认值下拉仅在 ui=select 且 options 非空时可用。 */
function isSelectFieldDraft(d: FnFieldDraft): boolean {
  return d.ui === 'select' && Array.isArray(d.orig.options) && d.orig.options.length > 0
}

function onFieldBoolChange(d: FnFieldDraft, checked: boolean) {
  d.boolValue = checked
  d.boolDirty = true
}

/** object 默认值 JSON 校验：非法 JSON 置错误并阻止保存。 */
function onFieldJsonInput(d: FnFieldDraft, raw: string) {
  d.jsonText = raw
  const trimmed = raw.trim()
  if (!trimmed) {
    d.jsonError = null
    return
  }
  try {
    JSON.parse(trimmed)
    d.jsonError = null
  } catch {
    d.jsonError = t('externalAgentWizard.interface.fieldDefaultInvalidJson')
  }
}

/** 组装保存用 input_schema：原始字段定义 + 草稿覆盖（label/description/ui/required/default）。 */
function buildEditSchema(): Record<string, any> {
  const fields: Record<string, any> = {}
  for (const d of fnEdit.fields) {
    const nf: Record<string, any> = { ...d.orig }
    nf.ui = d.ui
    nf.label = d.label
    if (d.description) nf.description = d.description
    else delete nf.description
    if (d.required) nf.required = true
    else delete nf.required
    let def: unknown = undefined
    if (d.type === 'boolean') {
      def = d.boolDirty ? d.boolValue : d.orig.default
    } else if (d.type === 'object') {
      // 非法 JSON 已由 fnEditHasJsonError 阻止保存
      def = d.jsonText.trim() === '' ? undefined : JSON.parse(d.jsonText)
    } else if (d.type === 'file') {
      // 文件字段不支持默认值，保留原值
      def = d.orig.default
    } else if (d.text === '') {
      def = undefined
    } else if (d.type === 'number') {
      def = Number(d.text)
    } else {
      def = d.text
    }
    if (def === undefined) delete nf.default
    else nf.default = def
    fields[d.key] = nf
  }
  return { order: [...fnEdit.order], fields }
}

async function saveFunctionEdit() {
  if (!routeId || !editingFunctionId.value) return
  if (fnEditHasJsonError.value) return
  fnEditError.value = null
  fnEditSaving.value = true
  try {
    await externalAgentFunctionApi.update(routeId, editingFunctionId.value, {
      input_schema: buildEditSchema(),
      summary: fnEdit.summary.trim(),
      sort_order: Number.isFinite(Number(fnEdit.sortOrder)) ? Number(fnEdit.sortOrder) : 0,
    })
    editingFunctionId.value = null
    fnUpdateSaved.value = true
    setTimeout(() => (fnUpdateSaved.value = false), 2000)
    await loadFunctions()
  } catch (e: unknown) {
    fnEditError.value = extractActionError(e)
  } finally {
    fnEditSaving.value = false
  }
}

function cancelFunctionEdit() {
  editingFunctionId.value = null
  fnEditError.value = null
}

function openImportSection() {
  appendError.value = null
  appendSuccessCount.value = null
  appendSkippedCount.value = null
  mgmtImportOpen.value = true
}

function closeImportSection() {
  mgmtImportOpen.value = false
  mgmtPanel.value?.reset()
}

/** 追加模式 confirm：带 agent_id，不新建插件；成功后刷新功能列表并收起区块。 */
async function appendFunctions() {
  if (!routeId) return
  appendError.value = null
  const payload = mgmtPanel.value?.buildPayload()
  if (!payload) {
    appendError.value = t('externalAgentWizard.import.confirmHint')
    return
  }
  appending.value = true
  try {
    const body: OpenapiImportConfirmRequest = {
      agent_id: routeId,
      auth: payload.auth,
      selected: payload.selected,
    }
    if (payload.doc_url) body.doc_url = payload.doc_url
    else if (payload.doc) body.doc = payload.doc
    const resp = await externalAgentPluginApi.importOpenapiConfirm(body)
    appendSuccessCount.value = resp.functions?.length ?? 0
    appendSkippedCount.value = resp.skipped_existing?.length ?? 0
    mgmtImportOpen.value = false
    mgmtPanel.value?.reset()
    await loadFunctions()
  } catch (e: unknown) {
    appendError.value = e instanceof Error ? e.message : t('externalAgentWizard.saveFailed')
  } finally {
    appending.value = false
  }
}

onMounted(() => {
  if (isEdit) void loadManagement()
})
</script>

<template>
  <!-- ═══ 管理页（编辑路由）：功能管理 ═══ -->
  <div v-if="isEdit" class="max-w-4xl mx-auto px-6 py-8">
    <!-- Header -->
    <div class="flex items-center gap-2 mb-1">
      <button
        type="button"
        class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        data-testid="mgmt-back"
        @click="router.push('/agents')"
      >
        <ChevronLeft class="w-3.5 h-3.5" />
        {{ t('externalAgentWizard.manage.backToList') }}
      </button>
    </div>
    <div class="flex items-center gap-3 mb-1">
      <Bot class="w-6 h-6 text-primary" />
      <h1 class="text-xl font-semibold text-foreground" data-testid="mgmt-agent-name">
        {{ agent?.name ?? t('externalAgentWizard.manage.title') }}
      </h1>
      <span
        v-if="agent"
        class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
        :class="statusBadgeClass(agent.status)"
        data-testid="mgmt-agent-status"
      >
        {{ statusBadgeLabel(agent.status) }}
      </span>
    </div>
    <p class="text-sm text-muted-foreground mb-6">{{ t('externalAgentWizard.manage.subtitle') }}</p>

    <div v-if="mgmtError" class="mb-4 rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive flex items-start gap-2">
      <AlertCircle class="w-4 h-4 mt-0.5 shrink-0" />
      <span class="flex-1">{{ mgmtError }}</span>
    </div>

    <div v-if="mgmtLoading" class="text-sm text-muted-foreground text-center py-16">
      {{ t('externalAgentWizard.manage.loading') }}
    </div>

    <template v-else>
      <!-- 基本信息 card -->
      <section class="rounded-xl border border-border bg-card p-6 space-y-4 mb-6" data-testid="mgmt-basic-card">
        <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.manage.basicTitle') }}</h2>

        <div>
          <label class="block text-sm font-medium text-foreground mb-1">
            {{ t('externalAgentWizard.basic.name') }} <span class="text-destructive">*</span>
          </label>
          <input
            v-model="mgmtForm.name"
            class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            :placeholder="t('externalAgentWizard.basic.namePlaceholder')"
            data-testid="mgmt-name-input"
          />
        </div>

        <div>
          <label class="block text-sm font-medium text-foreground mb-1">
            {{ t('externalAgentWizard.basic.description_field') }}
          </label>
          <textarea
            v-model="mgmtForm.description"
            rows="3"
            class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 resize-none"
            :placeholder="t('externalAgentWizard.basic.descriptionPlaceholder')"
          />
          <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.basic.descriptionHint') }}</p>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-foreground mb-1">
              {{ t('externalAgentWizard.basic.icon') }}
            </label>
            <input
              v-model="mgmtForm.icon_emoji"
              class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              maxlength="2"
            />
            <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.basic.iconHint') }}</p>
          </div>
          <div>
            <label class="block text-sm font-medium text-foreground mb-1">
              {{ t('externalAgentWizard.basic.themeColor') }}
            </label>
            <div class="flex gap-2 items-center">
              <input
                v-model="mgmtForm.theme_color"
                type="color"
                class="h-9 w-12 rounded-lg border border-input cursor-pointer bg-background p-0.5"
              />
              <input
                v-model="mgmtForm.theme_color"
                class="flex-1 rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
                placeholder="#7c3aed"
              />
            </div>
          </div>
        </div>

        <div v-if="basicError" class="text-xs text-destructive">{{ basicError }}</div>

        <div class="flex items-center justify-end gap-3">
          <span v-if="basicSaved" class="text-xs text-green-600" data-testid="mgmt-basic-saved">
            {{ t('externalAgentWizard.manage.saveSuccess') }}
          </span>
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50"
            :disabled="savingBasic"
            data-testid="mgmt-basic-save"
            @click="saveBasic"
          >
            <Save v-if="!savingBasic" class="w-4 h-4" />
            <Loader2 v-else class="w-4 h-4 animate-spin" />
            {{ savingBasic ? t('externalAgentWizard.manage.saving') : t('externalAgentWizard.manage.save') }}
          </button>
        </div>
      </section>

      <!-- 插件状态 card -->
      <section class="rounded-xl border border-border bg-card p-6 space-y-3 mb-6" data-testid="mgmt-status-card">
        <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.manage.statusTitle') }}</h2>
        <p class="text-xs text-muted-foreground">{{ t('externalAgentWizard.manage.statusHint') }}</p>
        <div class="flex items-center gap-3">
          <span
            class="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="statusBadgeClass(agent?.status)"
          >
            <Power class="w-3 h-3" />
            {{ statusBadgeLabel(agent?.status) }}
          </span>
          <button
            v-if="agent"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50"
            :class="agent.status === 'active'
              ? 'border-border text-foreground hover:bg-muted/50'
              : 'bg-primary text-primary-foreground hover:opacity-90'"
            :disabled="statusUpdating"
            data-testid="mgmt-status-toggle"
            @click="setAgentStatus(agent.status === 'active' ? 'disabled' : 'active')"
          >
            <Loader2 v-if="statusUpdating" class="w-3.5 h-3.5 animate-spin" />
            {{ agent.status === 'active'
              ? t('externalAgentWizard.manage.disable')
              : t('externalAgentWizard.manage.enable') }}
          </button>
        </div>
        <div v-if="statusError" class="text-xs text-destructive">{{ statusError }}</div>
      </section>

      <!-- 功能列表 card -->
      <section class="rounded-xl border border-border bg-card p-6 space-y-4" data-testid="mgmt-function-list">
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-base font-semibold text-foreground">
              {{ t('externalAgentWizard.manage.functionsTitle') }}
              <span class="text-xs text-muted-foreground font-normal ml-2">
                {{ t('externalAgentWizard.manage.functionsCount', { count: sortedFunctions.length }) }}
              </span>
            </h2>
            <p
              v-if="appendSuccessCount != null && appendSuccessCount > 0"
              class="text-xs text-green-600 mt-1"
              data-testid="mgmt-append-success"
            >
              {{ t('externalAgentWizard.manage.appendSuccess', { count: appendSuccessCount }) }}
            </p>
            <p
              v-if="appendSkippedCount != null && appendSkippedCount > 0"
              class="text-xs text-amber-600 dark:text-amber-400 mt-1"
              data-testid="mgmt-append-skipped"
            >
              {{ t('externalAgentWizard.manage.appendSkipped', { count: appendSkippedCount }) }}
            </p>
            <p
              v-if="appendSuccessCount === 0 && (appendSkippedCount ?? 0) > 0"
              class="text-xs text-amber-600 dark:text-amber-400 mt-1"
              data-testid="mgmt-append-all-skipped"
            >
              {{ t('externalAgentWizard.manage.appendAllSkipped') }}
            </p>
            <p v-if="fnUpdateSaved" class="text-xs text-green-600 mt-1" data-testid="mgmt-fn-saved">
              {{ t('externalAgentWizard.manage.fnUpdateSuccess') }}
            </p>
          </div>
          <div class="flex items-center gap-2">
            <button
              v-if="hasInactiveFunctions"
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-border text-foreground px-3 py-1.5 text-xs font-medium hover:bg-muted/50 disabled:opacity-50"
              :disabled="enablingAll"
              data-testid="mgmt-enable-all"
              @click="enableAllFunctions"
            >
              <Loader2 v-if="enablingAll" class="w-3.5 h-3.5 animate-spin" />
              <Power v-else class="w-3.5 h-3.5" />
              {{ enablingAll ? t('common.saving') : t('externalAgentWizard.manage.enableAll') }}
            </button>
            <button
              v-if="sortedFunctions.length > 0"
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-border text-foreground px-3 py-1.5 text-xs font-medium hover:bg-muted/50 disabled:opacity-50"
              :disabled="probingAll"
              data-testid="mgmt-probe-all"
              @click="probeAllFunctions"
            >
              <Loader2 v-if="probingAll" class="w-3.5 h-3.5 animate-spin" />
              <Wifi v-else class="w-3.5 h-3.5" />
              {{ probingAll ? t('externalAgentWizard.import.probing') : t('externalAgentWizard.manage.probeAll') }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-xs font-medium hover:opacity-90"
              data-testid="mgmt-add-function"
              @click="openImportSection"
            >
              <Plus class="w-3.5 h-3.5" />
              {{ t('externalAgentWizard.manage.addFunction') }}
            </button>
          </div>
        </div>

        <!-- 一键试调汇总 -->
        <p
          v-if="probeAllSummary"
          class="text-xs"
          :class="probeAllSummary.includes('0') && !probeAllSummary.includes('/0') ? 'text-green-600' : 'text-muted-foreground'"
          data-testid="mgmt-probe-all-summary"
        >
          {{ probeAllSummary }}
        </p>

        <div v-if="fnActionError" class="rounded-lg bg-destructive/10 px-4 py-3 text-xs text-destructive flex items-start gap-2">
          <AlertCircle class="w-4 h-4 mt-0.5 shrink-0" />
          <span class="flex-1">{{ fnActionError }}</span>
          <button class="text-destructive/70 hover:text-destructive" @click="fnActionError = null">
            <X class="w-4 h-4" />
          </button>
        </div>

        <div v-if="sortedFunctions.length === 0" class="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
          {{ t('externalAgentWizard.manage.noFunctions') }}
        </div>

        <div v-else class="space-y-2">
          <div
            v-for="f in sortedFunctions"
            :key="f.id"
            class="rounded-lg border border-border bg-background p-3"
            :data-testid="`mgmt-fn-row-${f.id}`"
          >
            <div class="flex items-start gap-3">
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="text-sm font-medium text-foreground">{{ f.name }}</span>
                  <span
                    v-if="functionMethodPath(f)"
                    class="inline-flex items-center px-1.5 py-0.5 rounded bg-muted text-foreground font-mono text-[10px]"
                  >
                    {{ functionMethodPath(f) }}
                  </span>
                  <span
                    class="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium"
                    :class="statusBadgeClass(f.status)"
                  >
                    {{ statusBadgeLabel(f.status) }}
                  </span>
                </div>
                <p v-if="f.summary" class="text-xs text-muted-foreground mt-1 line-clamp-2">{{ f.summary }}</p>
                <p class="text-[10px] text-muted-foreground mt-1">
                  {{ t('externalAgentWizard.manage.sortLabel') }}: {{ f.sort_order }}
                  · {{ t('externalAgentWizard.manage.versionLabel') }}: v{{ f.version }}
                </p>
              </div>

              <div class="flex items-center gap-1.5 shrink-0">
                <!-- 编辑（单功能字段编辑器） -->
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-2.5 py-1 text-xs hover:bg-muted/50"
                  :data-testid="`mgmt-fn-edit-${f.id}`"
                  @click="startEditFunction(f)"
                >
                  <Pencil class="w-3.5 h-3.5" />
                  {{ t('externalAgentWizard.manage.fnEdit') }}
                </button>
                <!-- 试调 -->
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-2.5 py-1 text-xs hover:bg-muted/50 disabled:opacity-50"
                  :disabled="probingFunctionId === f.id"
                  :data-testid="`mgmt-fn-probe-${f.id}`"
                  @click="probeManagedFunction(f)"
                >
                  <Loader2 v-if="probingFunctionId === f.id" class="w-3.5 h-3.5 animate-spin" />
                  <Wifi v-else class="w-3.5 h-3.5" />
                  {{ probingFunctionId === f.id
                    ? t('externalAgentWizard.manage.probing')
                    : t('externalAgentWizard.manage.probe') }}
                </button>
                <!-- 启用/停用 -->
                <button
                  type="button"
                  class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-2.5 py-1 text-xs hover:bg-muted/50 disabled:opacity-50"
                  :disabled="updatingFunctionId === f.id"
                  :data-testid="`mgmt-fn-toggle-${f.id}`"
                  @click="toggleFunctionStatus(f)"
                >
                  <Loader2 v-if="updatingFunctionId === f.id" class="w-3.5 h-3.5 animate-spin" />
                  {{ f.status === 'active'
                    ? t('externalAgentWizard.manage.fnDisable')
                    : t('externalAgentWizard.manage.fnEnable') }}
                </button>
                <!-- 删除（两步确认） -->
                <template v-if="confirmingDeleteId === f.id">
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded-lg bg-destructive text-white px-2.5 py-1 text-xs font-medium hover:opacity-90 disabled:opacity-50"
                    :disabled="deletingFunctionId === f.id"
                    :data-testid="`mgmt-fn-delete-confirm-${f.id}`"
                    @click="requestDeleteFunction(f)"
                  >
                    <Loader2 v-if="deletingFunctionId === f.id" class="w-3.5 h-3.5 animate-spin" />
                    {{ t('externalAgentWizard.manage.fnDeleteConfirm') }}
                  </button>
                  <button
                    type="button"
                    class="rounded-lg border border-border text-muted-foreground px-2.5 py-1 text-xs hover:bg-muted/50"
                    :data-testid="`mgmt-fn-delete-cancel-${f.id}`"
                    @click="cancelDeleteFunction"
                  >
                    {{ t('externalAgentWizard.manage.fnDeleteCancel') }}
                  </button>
                </template>
                <button
                  v-else
                  type="button"
                  class="inline-flex items-center gap-1 rounded-lg border border-transparent text-muted-foreground px-2.5 py-1 text-xs hover:text-destructive hover:bg-destructive/10"
                  :data-testid="`mgmt-fn-delete-${f.id}`"
                  @click="requestDeleteFunction(f)"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                  {{ t('externalAgentWizard.manage.fnDelete') }}
                </button>
              </div>
            </div>

            <!-- 行内试调结果 -->
            <div
              v-if="probeResults[f.id]"
              class="mt-2 flex items-center gap-1.5 text-xs"
              :data-testid="`mgmt-fn-probe-result-${f.id}`"
            >
              <CheckCircle2 v-if="probeResults[f.id].ok" class="w-3.5 h-3.5 text-green-600" />
              <X v-else class="w-3.5 h-3.5 text-destructive" />
              <span v-if="probeResults[f.id].ok" class="text-green-600">
                {{ t('externalAgentWizard.manage.probeOk') }}<template v-if="probeResults[f.id].latency != null"> · {{ probeResults[f.id].latency }}ms</template>
              </span>
              <span v-else class="text-destructive">
                {{ t('externalAgentWizard.manage.probeFail') }}<template v-if="probeResults[f.id].error"> · {{ probeResults[f.id].error }}</template>
              </span>
            </div>

            <!-- 单功能字段编辑器 -->
            <div
              v-if="editingFunctionId === f.id"
              class="mt-3 border-t border-border pt-3 space-y-3"
              :data-testid="`fn-edit-${f.id}`"
            >
              <!-- 功能级：简介 + 排序 -->
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-[10px] text-muted-foreground mb-1">
                    {{ t('externalAgentWizard.manage.fnEditSummary') }}
                  </label>
                  <input
                    :value="fnEdit.summary"
                    class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
                    :data-testid="`fn-edit-summary-${f.id}`"
                    @input="(e) => (fnEdit.summary = (e.target as HTMLInputElement).value)"
                  />
                </div>
                <div>
                  <label class="block text-[10px] text-muted-foreground mb-1">
                    {{ t('externalAgentWizard.manage.sortLabel') }}
                  </label>
                  <input
                    type="number"
                    :value="fnEdit.sortOrder"
                    class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
                    :data-testid="`fn-edit-sort-${f.id}`"
                    @input="(e) => (fnEdit.sortOrder = Number((e.target as HTMLInputElement).value))"
                  />
                </div>
              </div>

              <!-- 字段列表 -->
              <div>
                <div class="text-[10px] font-semibold text-muted-foreground uppercase mb-1.5">
                  {{ t('externalAgentWizard.manage.fnEditFieldsTitle') }}
                </div>
                <div v-if="fnEdit.fields.length === 0" class="text-xs text-muted-foreground">
                  {{ t('externalAgentWizard.manage.fnEditNoFields') }}
                </div>
                <div v-else class="space-y-2">
                  <div
                    v-for="d in fnEdit.fields"
                    :key="d.key"
                    class="rounded-md border border-border bg-muted/20 p-2.5 space-y-2"
                    :data-testid="`fn-edit-field-${d.key}`"
                  >
                    <div class="flex items-center gap-2 flex-wrap">
                      <span class="text-xs font-mono font-medium text-foreground">{{ d.key }}</span>
                      <span class="text-[10px] text-muted-foreground">{{ d.type }}</span>
                    </div>
                    <div class="grid grid-cols-2 gap-2">
                      <div>
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.interface.fieldLabel') }}
                        </label>
                        <input
                          :value="d.label"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px] focus:outline-none focus:ring-2 focus:ring-primary/30"
                          :data-testid="`fn-edit-label-${d.key}`"
                          @input="(e) => (d.label = (e.target as HTMLInputElement).value)"
                        />
                      </div>
                      <div>
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.import.fieldOverrideDescription') }}
                        </label>
                        <input
                          :value="d.description"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px] focus:outline-none focus:ring-2 focus:ring-primary/30"
                          :placeholder="t('externalAgentWizard.manage.fnEditFieldDescPlaceholder')"
                          :data-testid="`fn-edit-desc-${d.key}`"
                          @input="(e) => (d.description = (e.target as HTMLInputElement).value)"
                        />
                      </div>
                    </div>
                    <div class="grid grid-cols-3 gap-2 items-start">
                      <!-- 默认值编辑器（按 type 分发；file 无默认值编辑器） -->
                      <div v-if="d.type !== 'file'">
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.import.fieldOverrideDefault') }}
                        </label>
                        <select
                          v-if="isSelectFieldDraft(d)"
                          :value="d.text"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                          :data-testid="`fn-edit-default-${d.key}`"
                          @change="(e) => (d.text = (e.target as HTMLSelectElement).value)"
                        >
                          <option value=""></option>
                          <option v-for="opt in d.orig.options ?? []" :key="opt" :value="opt">{{ opt }}</option>
                        </select>
                        <input
                          v-else-if="d.type === 'number'"
                          type="number"
                          :value="d.text"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                          :data-testid="`fn-edit-default-${d.key}`"
                          @input="(e) => (d.text = (e.target as HTMLInputElement).value)"
                        />
                        <input
                          v-else-if="d.type === 'boolean'"
                          type="checkbox"
                          class="accent-primary mt-1.5"
                          :checked="d.boolValue"
                          :data-testid="`fn-edit-default-${d.key}`"
                          @change="(e) => onFieldBoolChange(d, (e.target as HTMLInputElement).checked)"
                        />
                        <textarea
                          v-else-if="d.type === 'object'"
                          rows="2"
                          :value="d.jsonText"
                          class="w-full rounded-md border bg-background text-foreground px-2 py-1 text-[11px] font-mono resize-y"
                          :class="d.jsonError ? 'border-destructive' : 'border-input'"
                          :data-testid="`fn-edit-default-${d.key}`"
                          @input="(e) => onFieldJsonInput(d, (e.target as HTMLTextAreaElement).value)"
                        />
                        <input
                          v-else
                          type="text"
                          :value="d.text"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                          :data-testid="`fn-edit-default-${d.key}`"
                          @input="(e) => (d.text = (e.target as HTMLInputElement).value)"
                        />
                        <p v-if="d.type === 'object' && d.jsonError" class="text-[10px] text-destructive mt-1">
                          {{ d.jsonError }}
                        </p>
                      </div>
                      <div v-else>
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.import.fieldOverrideDefault') }}
                        </label>
                        <p class="text-[10px] text-muted-foreground mt-1.5">
                          {{ t('externalAgentWizard.manage.fnEditFileNoDefault') }}
                        </p>
                      </div>
                      <!-- ui 控件选择（后端校验取值合法性） -->
                      <div>
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.interface.fieldUi') }}
                        </label>
                        <select
                          v-model="d.ui"
                          class="w-full rounded-md border border-input bg-background text-foreground px-2 py-1 text-[11px]"
                          :data-testid="`fn-edit-ui-${d.key}`"
                        >
                          <option v-for="u in PLUGIN_FIELD_UIS" :key="u" :value="u">{{ u }}</option>
                        </select>
                      </div>
                      <!-- 必填 -->
                      <div>
                        <label class="block text-[10px] text-muted-foreground mb-1">
                          {{ t('externalAgentWizard.interface.fieldRequired') }}
                        </label>
                        <input
                          v-model="d.required"
                          type="checkbox"
                          class="accent-primary mt-1.5"
                          :data-testid="`fn-edit-required-${d.key}`"
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div v-if="fnEditError" class="text-xs text-destructive">{{ fnEditError }}</div>

              <div class="flex items-center justify-end gap-2">
                <button
                  type="button"
                  class="rounded-lg border border-border text-foreground px-3 py-1.5 text-xs hover:bg-muted/50"
                  :data-testid="`fn-edit-cancel-${f.id}`"
                  @click="cancelFunctionEdit"
                >
                  {{ t('externalAgentWizard.manage.fnEditCancel') }}
                </button>
                <button
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-xs font-medium hover:opacity-90 disabled:opacity-50"
                  :disabled="fnEditSaving || fnEditHasJsonError"
                  :data-testid="`fn-edit-save-${f.id}`"
                  @click="saveFunctionEdit"
                >
                  <Loader2 v-if="fnEditSaving" class="w-3.5 h-3.5 animate-spin" />
                  <Save v-else class="w-3.5 h-3.5" />
                  {{ t('externalAgentWizard.manage.fnEditSave') }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 添加功能：内联 OpenAPI 导入（追加模式） -->
      <section
        v-if="mgmtImportOpen"
        class="rounded-xl border border-primary/40 bg-card p-6 space-y-4 mt-6"
        data-testid="mgmt-import-section"
      >
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.manage.importTitle') }}</h2>
            <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.manage.appendHint') }}</p>
          </div>
          <button
            type="button"
            class="rounded-lg border border-border text-muted-foreground px-2.5 py-1 text-xs hover:bg-muted/50"
            data-testid="mgmt-import-collapse"
            @click="closeImportSection"
          >
            {{ t('externalAgentWizard.manage.importCollapse') }}
          </button>
        </div>

        <ExternalAgentImportPanel ref="mgmtPanel" :existing-ops="existingOps" />

        <div v-if="appendError" class="rounded-lg bg-destructive/10 px-4 py-3 text-xs text-destructive">
          {{ appendError }}
        </div>

        <div class="flex items-center justify-end">
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50"
            :disabled="appending"
            data-testid="mgmt-import-confirm"
            @click="appendFunctions"
          >
            <Loader2 v-if="appending" class="w-4 h-4 animate-spin" />
            <Check v-else class="w-4 h-4" />
            {{ t('externalAgentWizard.manage.importConfirm') }}
          </button>
        </div>
      </section>
    </template>
  </div>

  <!-- ═══ 创建向导 ═══ -->
  <div v-else class="max-w-4xl mx-auto px-6 py-8">
    <!-- Header -->
    <div class="flex items-center gap-3 mb-4">
      <Bot class="w-6 h-6 text-primary" />
      <h1 class="text-xl font-semibold text-foreground">{{ t('externalAgentWizard.title') }}</h1>
    </div>
    <p class="text-sm text-muted-foreground mb-6">{{ t('externalAgentWizard.subtitle') }}</p>

    <!-- Stepper -->
    <ol class="flex items-center gap-2 mb-6">
      <li
        v-for="step in ([1, 2, 3] as const)"
        :key="step"
        class="flex items-center gap-2"
      >
        <button
          type="button"
          class="flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium transition-colors"
          :class="
            currentStep === step
              ? 'bg-primary text-primary-foreground'
              : step < currentStep
                ? 'bg-primary/20 text-primary hover:bg-primary/30'
                : 'bg-muted text-muted-foreground'
          "
          @click="gotoStep(step)"
        >
          <span class="inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px]"
            :class="currentStep === step ? 'bg-primary-foreground/20' : 'bg-foreground/10'">
            {{ step }}
          </span>
          {{ STEP_LABELS[step] }}
        </button>
        <ChevronRight v-if="step < 3" class="w-3.5 h-3.5 text-muted-foreground" />
      </li>
    </ol>

    <div v-if="error" class="mb-4 rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive flex items-start gap-2">
      <AlertCircle class="w-4 h-4 mt-0.5 shrink-0" />
      <span class="flex-1">{{ error }}</span>
      <button class="text-destructive/70 hover:text-destructive" @click="error = null">
        <X class="w-4 h-4" />
      </button>
    </div>

    <div v-if="stepError" class="mb-4 rounded-lg bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
      {{ stepError }}
    </div>

    <!-- ── Step 1: Basic info ─────────────────────────────────────────── -->
    <section
      v-if="currentStep === 1"
      class="rounded-xl border border-border bg-card p-6 space-y-5"
      data-testid="wizard-step-1"
    >
      <div>
        <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.basic.title') }}</h2>
        <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.basic.description') }}</p>
      </div>

      <div>
        <label class="block text-sm font-medium text-foreground mb-1">
          {{ t('externalAgentWizard.basic.name') }} <span class="text-destructive">*</span>
        </label>
        <input
          v-model="wizard.name"
          class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
          :placeholder="t('externalAgentWizard.basic.namePlaceholder')"
        />
      </div>

      <div>
        <label class="block text-sm font-medium text-foreground mb-1">
          {{ t('externalAgentWizard.basic.description_field') }}
        </label>
        <textarea
          v-model="wizard.description"
          rows="3"
          class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 resize-none"
          :placeholder="t('externalAgentWizard.basic.descriptionPlaceholder')"
        />
        <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.basic.descriptionHint') }}</p>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div>
          <label class="block text-sm font-medium text-foreground mb-1">
            {{ t('externalAgentWizard.basic.icon') }}
          </label>
          <input
            v-model="wizard.icon_emoji"
            class="w-full rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            maxlength="2"
          />
          <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.basic.iconHint') }}</p>
        </div>
        <div>
          <label class="block text-sm font-medium text-foreground mb-1">
            {{ t('externalAgentWizard.basic.themeColor') }}
          </label>
          <div class="flex gap-2 items-center">
            <input
              v-model="wizard.theme_color"
              type="color"
              class="h-9 w-12 rounded-lg border border-input cursor-pointer bg-background p-0.5"
            />
            <input
              v-model="wizard.theme_color"
              class="flex-1 rounded-lg border border-input bg-background text-foreground px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
              placeholder="#7c3aed"
            />
          </div>
        </div>
      </div>
    </section>

    <!-- ── Step 2: OpenAPI import（v-show 保持面板跨步骤状态） ──────────── -->
    <section
      v-show="currentStep === 2"
      class="rounded-xl border border-border bg-card p-6 space-y-5"
      data-testid="wizard-step-2"
    >
      <div>
        <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.import.stepTitle') }}</h2>
        <p class="text-xs text-muted-foreground mt-1">{{ t('externalAgentWizard.import.stepDescription') }}</p>
      </div>

      <ExternalAgentImportPanel ref="wizardPanel" />
    </section>

    <!-- ── Step 3: Import & probe ─────────────────────────────────────── -->
    <section
      v-if="currentStep === 3"
      class="rounded-xl border border-border bg-card p-6 space-y-5"
      data-testid="wizard-step-3"
    >
      <div>
        <h2 class="text-base font-semibold text-foreground">{{ t('externalAgentWizard.import.probeStepTitle') }}</h2>
        <p class="text-xs text-muted-foreground mt-1">
          {{ t('externalAgentWizard.import.probeAfterImportHint') }}
        </p>
      </div>

      <div v-if="!importedAgentId && !probing" class="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
        {{ t('externalAgentWizard.import.probeAfterImportHint') }}
      </div>
      <div v-else-if="importProbeResults.length" class="space-y-2" data-testid="import-probe-results">
        <div
          v-for="r in importProbeResults"
          :key="r.name"
          class="flex items-center justify-between rounded-lg border border-border px-4 py-2.5"
        >
          <div class="flex items-center gap-2">
            <CheckCircle2 v-if="r.ok" class="w-4 h-4 text-green-600" />
            <X v-else class="w-4 h-4 text-destructive" />
            <span class="text-sm font-medium text-foreground">{{ r.name }}</span>
          </div>
          <div class="text-xs">
            <span v-if="r.ok" class="text-green-600">
              {{ t('externalAgentWizard.import.probeFnOk') }}<template v-if="r.latency != null"> · {{ r.latency }}ms</template>
            </span>
            <span v-else class="text-destructive">
              {{ t('externalAgentWizard.import.probeFnFail') }}<template v-if="r.error"> · {{ r.error }}</template>
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- ── Footer: action buttons ───────────────────────────────────────── -->
    <div class="mt-6 flex items-center justify-between gap-3">
      <button
        type="button"
        class="rounded-lg border border-border text-foreground px-4 py-2 text-sm hover:bg-muted/50"
        @click="router.push('/agents')"
      >
        {{ t('externalAgentWizard.cancel') }}
      </button>

      <div class="flex items-center gap-2">
        <button
          v-if="currentStep > 1"
          type="button"
          class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-3 py-2 text-sm hover:bg-muted/50"
          @click="goPrev"
        >
          <ChevronLeft class="w-4 h-4" />
          {{ t('externalAgentWizard.prev') }}
        </button>

        <button
          v-if="currentStep < 3"
          type="button"
          class="inline-flex items-center gap-1 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90"
          @click="goNext"
        >
          {{ t('externalAgentWizard.next') }}
          <ChevronRight class="w-4 h-4" />
        </button>

        <!-- Step 3 footer -->
        <template v-if="currentStep === 3">
          <!-- 已导入：唯一动作是去编辑页（importOnly 有防重复导入守卫） -->
          <button
            v-if="importedAgentId"
            type="button"
            class="inline-flex items-center gap-1 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90"
            data-testid="import-goto-edit"
            @click="importOnly"
          >
            <Check class="w-4 h-4" />
            {{ t('externalAgentWizard.import.goToEdit') }}
          </button>
          <template v-else>
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-lg border border-border text-foreground px-3 py-2 text-sm hover:bg-muted/50 disabled:opacity-50"
              :disabled="saving"
              @click="importOnly"
            >
              <Loader2 v-if="saving" class="w-3.5 h-3.5 animate-spin" />
              {{ saving ? t('common.saving') : t('externalAgentWizard.import.importDraftOnly') }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-1 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50"
              :disabled="saving || probing"
              @click="runImportAndProbe"
            >
              <Loader2 v-if="probing" class="w-4 h-4 animate-spin" />
              <Check v-else class="w-4 h-4" />
              {{ t('externalAgentWizard.import.importAndProbe') }}
            </button>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>
