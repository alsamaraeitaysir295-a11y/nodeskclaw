import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

/**
 * Vitest suite for ExternalAgentFormView.vue（创建向导 + 功能管理页）。
 *
 * 产品方向（import-only）后本组件只有两条路径：
 *  1. 创建向导（/new）：基本信息 → OpenAPI 导入 → 导入并试调。
 *     手动配置模式与类型选择已删除，不再调用 validateManifest。
 *  2. 管理页（/:id/edit）：基本信息 / 插件状态 / 功能列表（试调、启停、两步删除）
 *     + 添加功能（OpenAPI 追加导入，confirm 带 agent_id）。
 *
 * 注意：tests/setup.ts 把 vue-i18n 的 t 模拟为直接返回 key；
 *      wizard step 2 用 v-show 渲染（保持导入面板跨步骤状态），
 *      步骤判定用 section 级 data-testid 而非文本匹配。
 */

const routerPush = vi.fn()
const routerReplace = vi.fn()
let currentRouteParams: Record<string, string> = {}

vi.mock('vue-router', () => ({
  useRoute: () => ({
    params: currentRouteParams,
    query: {},
    path: currentRouteParams.id
      ? `/org-settings/external-agents/${currentRouteParams.id}/edit`
      : '/org-settings/external-agents/new',
  }),
  useRouter: () => ({
    push: routerPush,
    replace: routerReplace,
    back: vi.fn(),
  }),
}))

const listAgents = vi.fn()
const updateAgent = vi.fn()
const importOpenapiPreview = vi.fn()
const importOpenapiConfirm = vi.fn()
const probeFunction = vi.fn()
const listFunctions = vi.fn()
const updateFunction = vi.fn()
const removeFunction = vi.fn()

vi.mock('@/services/externalAgents', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/externalAgents')>(
      '@/services/externalAgents',
    )
  return {
    ...actual,
    externalAgentApi: {
      ...actual.externalAgentApi,
      list: (...args: any[]) => listAgents(...args),
      update: (...args: any[]) => updateAgent(...args),
    },
    externalAgentPluginApi: {
      ...actual.externalAgentPluginApi,
      importOpenapiPreview: (...args: any[]) => importOpenapiPreview(...args),
      importOpenapiConfirm: (...args: any[]) => importOpenapiConfirm(...args),
    },
    externalAgentFunctionApi: {
      ...actual.externalAgentFunctionApi,
      list: (...args: any[]) => listFunctions(...args),
      probe: (...args: any[]) => probeFunction(...args),
      update: (...args: any[]) => updateFunction(...args),
      remove: (...args: any[]) => removeFunction(...args),
    },
  }
})

import ExternalAgentFormView from '../ExternalAgentFormView.vue'
import zhCN from '@/i18n/locales/zh-CN'
import enUS from '@/i18n/locales/en-US'

const K = {
  NEXT: 'externalAgentWizard.next',
  PREV: 'externalAgentWizard.prev',
  PARSE_BUTTON: 'externalAgentWizard.import.parseButton',
  IMPORT_AND_PROBE: 'externalAgentWizard.import.importAndProbe',
  GO_TO_EDIT: 'externalAgentWizard.import.goToEdit',
  NO_FUNCTIONS: 'externalAgentWizard.import.noFunctions',
  PROBE_FN_OK: 'externalAgentWizard.import.probeFnOk',
  PROBE_FN_FAIL: 'externalAgentWizard.import.probeFnFail',
  MGMT_PROBE_OK: 'externalAgentWizard.manage.probeOk',
  STATUS_ACTIVE: 'externalAgentWizard.status.active',
  ADD_FUNCTION: 'externalAgentWizard.manage.addFunction',
}

beforeEach(() => {
  currentRouteParams = {}
  routerPush.mockClear()
  routerReplace.mockClear()
  listAgents.mockReset()
  updateAgent.mockReset()
  importOpenapiPreview.mockReset()
  importOpenapiConfirm.mockReset()
  probeFunction.mockReset()
  listFunctions.mockReset()
  updateFunction.mockReset()
  removeFunction.mockReset()
  listAgents.mockResolvedValue([])
})

afterEach(() => {
  vi.clearAllMocks()
})

async function mountView() {
  const wrapper = mount(ExternalAgentFormView, {
    global: {
      stubs: {
        teleport: true,
      },
    },
  })
  await flushPromises()
  return wrapper
}

function getButton(wrapper: any, textContains: string) {
  return wrapper
    .findAll('button')
    .find((b: any) => (b.text() || '').includes(textContains))
}

async function setName(wrapper: any, name: string) {
  const nameInput = wrapper
    .findAll('input')
    .find((i: any) => (i.attributes('placeholder') || '').includes('namePlaceholder'))
  expect(nameInput).toBeTruthy()
  await nameInput!.setValue(name)
  await flushPromises()
}

async function clickNext(wrapper: any) {
  const nextBtn = getButton(wrapper, K.NEXT)
  expect(nextBtn).toBeTruthy()
  await nextBtn!.trigger('click')
  await flushPromises()
}

async function clickPrev(wrapper: any) {
  const prevBtn = getButton(wrapper, K.PREV)
  expect(prevBtn).toBeTruthy()
  await prevBtn!.trigger('click')
  await flushPromises()
}

// step 1/3 是 v-if（exists 判定），step 2 是 v-show（isVisible 判定）
function currentStep(wrapper: any): 1 | 2 | 3 | null {
  if (wrapper.find('[data-testid="wizard-step-1"]').exists()) return 1
  if (wrapper.find('[data-testid="wizard-step-3"]').exists()) return 3
  const s2 = wrapper.find('[data-testid="wizard-step-2"]')
  if (s2.exists() && s2.isVisible()) return 2
  return null
}

function previewResponse(functions: any[]) {
  return {
    spec_version: '3.0.0',
    servers: [{ url: 'https://api.example.com' }],
    functions,
    warnings: [],
  }
}

async function goToStep2(wrapper: any) {
  await setName(wrapper, 'Import Plugin')
  await clickNext(wrapper)
  expect(currentStep(wrapper)).toBe(2)
}

async function parseUrl(wrapper: any, url = 'http://api.example.com/v3/api-docs') {
  const urlInput = wrapper.find('[data-testid="import-url-input"]')
  expect(urlInput.exists()).toBe(true)
  await urlInput.setValue(url)
  await wrapper.find('[data-testid="import-parse-button"]').trigger('click')
  await flushPromises()
  await flushPromises()
}

// ═══════════════════════════════════════════════════════════════════════════
// 创建向导（import-only）
// ═══════════════════════════════════════════════════════════════════════════

describe('ExternalAgentFormView wizard – 基本信息 → OpenAPI 导入', () => {
  it('没有类型选择；步骤 1 缺名称时 Next 停在原地', async () => {
    const wrapper = await mountView()
    expect(currentStep(wrapper)).toBe(1)

    // 类型选择已删除（不再有 chat/tool 单选）
    expect(wrapper.find('input[type="radio"][value="tool"]').exists()).toBe(false)
    expect(wrapper.find('input[type="radio"][value="chat"]').exists()).toBe(false)
    // 手动配置/导入模式切换已删除
    expect(wrapper.find('[data-testid="mode-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mode-import"]').exists()).toBe(false)

    await clickNext(wrapper)
    expect(currentStep(wrapper)).toBe(1)
  })

  it('填名称后进入步骤 2（导入 UI），Prev 可回步骤 1', async () => {
    const wrapper = await mountView()
    await goToStep2(wrapper)

    // 步骤 2 即导入 UI：URL/JSON 标签 + 解析按钮
    expect(wrapper.find('[data-testid="import-source-url"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="import-source-json"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="import-url-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="import-parse-button"]').exists()).toBe(true)

    await clickPrev(wrapper)
    expect(currentStep(wrapper)).toBe(1)
  })

  it('未解析文档时 Next 停在步骤 2', async () => {
    const wrapper = await mountView()
    await goToStep2(wrapper)

    await clickNext(wrapper)
    expect(currentStep(wrapper)).toBe(2)
    expect(wrapper.find('[data-testid="wizard-step-3"]').exists()).toBe(false)
  })

  it('贴 URL 解析后渲染 function 列表（method + path + 默认全选）', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'list_orders',
          summary: 'List orders',
          method: 'GET',
          path: '/api/v1/orders',
          fields: [
            { key: 'limit', type: 'number', ui: 'number', label: 'Limit', required: false },
          ],
          output_hint_suggestion: { display: 'table', primary_key: 'id' },
          warnings: [],
        },
        {
          name: 'create_order',
          summary: 'Create a new order',
          method: 'POST',
          path: '/api/v1/orders',
          fields: [
            { key: 'line', type: 'string', ui: 'input', label: 'Line', required: true },
          ],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
      ]),
    )

    const wrapper = await mountView()
    await goToStep2(wrapper)
    await parseUrl(wrapper)

    expect(importOpenapiPreview).toHaveBeenCalledTimes(1)
    expect(importOpenapiPreview).toHaveBeenCalledWith({
      doc_url: 'http://api.example.com/v3/api-docs',
    })

    expect(wrapper.find('[data-testid="import-preview"]').isVisible()).toBe(true)
    // 解析后统一鉴权配置出现
    expect(wrapper.text()).toContain('externalAgentWizard.import.authSection')
    const list = wrapper.find('[data-testid="import-function-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('list_orders')
    expect(list.text()).toContain('create_order')
    expect(list.text()).toContain('GET /api/v1/orders')
    expect(list.text()).toContain('POST /api/v1/orders')
    // 默认全选 → 已选 2 个
    expect(wrapper.text()).toContain('externalAgentWizard.import.selectedCount')
  })

  it('贴 JSON 解析正常（doc 对象透传 + server URL 渲染）', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'get_thing',
          summary: 'Get thing',
          method: 'GET',
          path: '/thing',
          fields: [],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
      ]),
    )

    const wrapper = await mountView()
    await goToStep2(wrapper)

    await wrapper.find('[data-testid="import-source-json"]').trigger('click')
    await flushPromises()
    const jsonInput = wrapper.find('[data-testid="import-json-input"]')
    expect(jsonInput.exists()).toBe(true)
    await jsonInput.setValue(
      JSON.stringify({
        openapi: '3.0.0',
        paths: { '/thing': { get: { operationId: 'get_thing', responses: {} } } },
      }),
    )
    await wrapper.find('[data-testid="import-parse-button"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(importOpenapiPreview).toHaveBeenCalledTimes(1)
    expect(importOpenapiPreview.mock.calls[0][0]).toEqual({
      doc: expect.objectContaining({ openapi: '3.0.0' }),
    })
    expect(wrapper.text()).toContain('get_thing')
    expect(wrapper.text()).toContain('https://api.example.com')
    expect(wrapper.text()).not.toContain('[object Object]')
  })

  it('全不选后 Next 被挡在步骤 2', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        { name: 'a', summary: null, method: 'GET', path: '/a', fields: [], warnings: [] },
      ]),
    )

    const wrapper = await mountView()
    await goToStep2(wrapper)
    await parseUrl(wrapper, 'http://api.example.com')

    const deselectBtn = wrapper.find('[data-testid="import-deselect-all"]')
    expect(deselectBtn.exists()).toBe(true)
    await deselectBtn.trigger('click')
    await flushPromises()

    await clickNext(wrapper)
    expect(currentStep(wrapper)).toBe(2)
    expect(wrapper.find('[data-testid="wizard-step-3"]').exists()).toBe(false)
  })
})

describe('ExternalAgentFormView wizard – 导入并试调（step 3）', () => {
  async function goImportStep3(wrapper: any) {
    await goToStep2(wrapper)
    await parseUrl(wrapper, 'http://host.docker.internal:4100/openapi.json')
    await clickNext(wrapper)
    expect(currentStep(wrapper)).toBe(3)
  }

  it('点击「导入并逐功能试调」：confirm 一次 + 每功能各 probe 一次 + 逐行结果', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'fn_a', summary: 'A', method: 'GET', path: '/a',
          fields: [], output_hint_suggestion: { display: 'json' }, warnings: [],
        },
        {
          name: 'fn_b', summary: 'B', method: 'POST', path: '/b',
          fields: [], output_hint_suggestion: { display: 'json' }, warnings: [],
        },
      ]),
    )
    importOpenapiConfirm.mockResolvedValueOnce({
      agent: { id: 'agent-x' },
      functions: [
        { id: 'f1', name: 'fn_a' },
        { id: 'f2', name: 'fn_b' },
      ],
    })
    probeFunction
      .mockResolvedValueOnce({ ok: true, reachable: true, latency_ms: 12, error: null })
      .mockResolvedValueOnce({ ok: false, reachable: false, latency_ms: 3, error: 'conn refused' })

    const wrapper = await mountView()
    await goImportStep3(wrapper)

    const probeBtn = wrapper.findAll('button').find((b: any) =>
      b.text().includes(K.IMPORT_AND_PROBE),
    )
    expect(probeBtn).toBeTruthy()
    await probeBtn!.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(importOpenapiConfirm).toHaveBeenCalledTimes(1)
    const body = importOpenapiConfirm.mock.calls[0][0]
    expect(body.name).toBe('Import Plugin')
    expect(body.doc_url).toBe('http://host.docker.internal:4100/openapi.json')
    expect(body.auth).toEqual({ type: 'none' })
    expect(body.selected).toHaveLength(2)

    expect(probeFunction).toHaveBeenCalledTimes(2)
    expect(probeFunction).toHaveBeenNthCalledWith(1, 'agent-x', 'f1')
    expect(probeFunction).toHaveBeenNthCalledWith(2, 'agent-x', 'f2')

    const results = wrapper.find('[data-testid="import-probe-results"]')
    expect(results.exists()).toBe(true)
    expect(results.text()).toContain('fn_a')
    expect(results.text()).toContain('fn_b')
    expect(results.text()).toContain(K.PROBE_FN_OK)
    expect(results.text()).toContain(K.PROBE_FN_FAIL)
    expect(results.text()).toContain('conn refused')
  })

  it('导入完成后底部只显示「前往编辑页」，再次点击不会重复导入', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        { name: 'fn_a', summary: 'A', method: 'GET', path: '/a', fields: [], output_hint_suggestion: {}, warnings: [] },
      ]),
    )
    importOpenapiConfirm.mockResolvedValueOnce({
      agent: { id: 'agent-y' },
      functions: [{ id: 'f1', name: 'fn_a' }],
    })
    probeFunction.mockResolvedValue({ ok: true, reachable: true, latency_ms: 5, error: null })

    const wrapper = await mountView()
    await goImportStep3(wrapper)
    const probeBtn = wrapper.findAll('button').find((b: any) =>
      b.text().includes(K.IMPORT_AND_PROBE),
    )
    await probeBtn!.trigger('click')
    await flushPromises()
    await flushPromises()

    // 底部按钮切换为「前往编辑页」
    const gotoBtn = wrapper.find('[data-testid="import-goto-edit"]')
    expect(gotoBtn.exists()).toBe(true)
    expect(gotoBtn.text()).toContain(K.GO_TO_EDIT)

    await gotoBtn.trigger('click')
    await flushPromises()
    // confirm 仍只调了一次（防重复导入守卫）
    expect(importOpenapiConfirm).toHaveBeenCalledTimes(1)
    expect(routerPush).toHaveBeenCalledWith({
      name: 'ExternalAgentEdit',
      params: { id: 'agent-y' },
    })
  })
})

// ═══════════════════════════════════════════════════════════════════════════
// 管理页（edit 路由）：功能管理
// ═══════════════════════════════════════════════════════════════════════════

const AGENT = {
  id: 'a-1',
  org_id: 'org-1',
  name: 'MES Plugin',
  description: 'MES 缺陷系统',
  endpoint: 'http://10.50.54.233:9000',
  protocol: 'openai_compatible',
  capabilities: [],
  icon_emoji: null,
  theme_color: null,
  is_reachable: true,
  last_checked_at: null,
  created_at: '2026-08-20T00:00:00Z',
  updated_at: '2026-08-20T00:00:00Z',
  type: 'tool',
  status: 'active',
  version: 3,
  function_count: 2,
}

function makeFunction(id: string, over: Record<string, any> = {}) {
  return {
    id,
    agent_id: 'a-1',
    name: `fn_${id}`,
    summary: `Summary ${id}`,
    status: 'draft',
    sort_order: 1,
    source: 'openapi_import',
    version: 1,
    created_at: '2026-08-20T00:00:00Z',
    updated_at: '2026-08-20T00:00:00Z',
    invoke_config: null,
    input_schema: null,
    origin_meta: null,
    ...over,
  }
}

async function mountManage(functionsOverride?: any[]) {
  currentRouteParams = { id: 'a-1' }
  listAgents.mockResolvedValue([AGENT])
  listFunctions.mockResolvedValue(
    functionsOverride ?? [
      makeFunction('f1', {
        name: 'query_defects',
        summary: '查询缺陷记录',
        sort_order: 2,
        invoke_config: { endpoint: 'http://10.50.54.233:9000/api/v1/defects/query', method: 'POST' },
      }),
      makeFunction('f2', {
        name: 'import_defect_file',
        summary: '批量导入缺陷明细',
        sort_order: 1,
        status: 'active',
        origin_meta: { method: 'post', path: '/api/v1/defects/import', spec_version: '3.0.0' },
      }),
    ],
  )
  updateAgent.mockResolvedValue({ ...AGENT })
  updateFunction.mockResolvedValue(makeFunction('f1'))
  removeFunction.mockResolvedValue(undefined)
  const wrapper = await mountView()
  return wrapper
}

describe('ExternalAgentFormView manage – 加载与渲染', () => {
  it('渲染插件信息 + 功能行（按 sort_order 排序，method/path 取 origin_meta 或 endpoint）', async () => {
    const wrapper = await mountManage()

    // 创建向导的 stepper 不应出现在管理页
    expect(wrapper.find('[data-testid="wizard-step-1"]').exists()).toBe(false)

    expect(wrapper.find('[data-testid="mgmt-agent-name"]').text()).toContain('MES Plugin')
    expect(wrapper.find('[data-testid="mgmt-agent-status"]').text()).toContain(K.STATUS_ACTIVE)

    const list = wrapper.find('[data-testid="mgmt-function-list"]')
    expect(list.exists()).toBe(true)
    const rows = wrapper.findAll('[data-testid^="mgmt-fn-row-"]')
    expect(rows.length).toBe(2)
    // f2 sort_order=1 排前面
    expect(rows[0].attributes('data-testid')).toBe('mgmt-fn-row-f2')
    expect(rows[1].attributes('data-testid')).toBe('mgmt-fn-row-f1')
    // f2 有 origin_meta → 渲染 POST /api/v1/defects/import（method 大写）
    expect(rows[0].text()).toContain('POST /api/v1/defects/import')
    // f1 无 origin_meta → 兜底 invoke_config.endpoint
    expect(rows[1].text()).toContain('http://10.50.54.233:9000/api/v1/defects/query')
    // summary + 版本号
    expect(list.text()).toContain('查询缺陷记录')
    expect(list.text()).toContain('v1')

    expect(listAgents).toHaveBeenCalledTimes(1)
    expect(listFunctions).toHaveBeenCalledWith('a-1')
  })
})

describe('ExternalAgentFormView manage – 基本信息与插件状态', () => {
  it('修改名称后点保存调用 externalAgentApi.update 并显示成功提示', async () => {
    const wrapper = await mountManage()

    const nameInput = wrapper.find('[data-testid="mgmt-name-input"]')
    expect(nameInput.exists()).toBe(true)
    await nameInput.setValue('MES Plugin v2')
    await wrapper.find('[data-testid="mgmt-basic-save"]').trigger('click')
    await flushPromises()

    expect(updateAgent).toHaveBeenCalledTimes(1)
    expect(updateAgent).toHaveBeenCalledWith(
      'a-1',
      expect.objectContaining({ name: 'MES Plugin v2' }),
    )
    expect(wrapper.find('[data-testid="mgmt-basic-saved"]').exists()).toBe(true)
  })

  it('插件状态卡片的启停按钮调用 update({ status })', async () => {
    const wrapper = await mountManage()

    // 当前 active → 按钮为「停用」
    await wrapper.find('[data-testid="mgmt-status-toggle"]').trigger('click')
    await flushPromises()

    expect(updateAgent).toHaveBeenCalledWith('a-1', { status: 'disabled' })
  })
})

describe('ExternalAgentFormView manage – 功能行操作', () => {
  it('试调按钮调用 probe 并在行内展示可达结果', async () => {
    probeFunction.mockResolvedValueOnce({ ok: true, reachable: true, latency_ms: 23, error: null })
    const wrapper = await mountManage()

    await wrapper.find('[data-testid="mgmt-fn-probe-f2"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(probeFunction).toHaveBeenCalledWith('a-1', 'f2')
    const result = wrapper.find('[data-testid="mgmt-fn-probe-result-f2"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain(K.MGMT_PROBE_OK)
    expect(result.text()).toContain('23')
  })

  it('启用/停用按钮调用 updateFunction 传目标 status', async () => {
    const wrapper = await mountManage()

    // f1 为 draft → 按钮为「启用」→ status=active
    await wrapper.find('[data-testid="mgmt-fn-toggle-f1"]').trigger('click')
    await flushPromises()

    expect(updateFunction).toHaveBeenCalledTimes(1)
    expect(updateFunction).toHaveBeenCalledWith('a-1', 'f1', { status: 'active' })
    // 成功后刷新功能列表
    expect(listFunctions.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('删除功能为两步确认：第一次仅进入确认态，第二次才调 removeFunction', async () => {
    const wrapper = await mountManage()

    await wrapper.find('[data-testid="mgmt-fn-delete-f1"]').trigger('click')
    await flushPromises()
    expect(removeFunction).not.toHaveBeenCalled()

    const confirmBtn = wrapper.find('[data-testid="mgmt-fn-delete-confirm-f1"]')
    expect(confirmBtn.exists()).toBe(true)
    await confirmBtn.trigger('click')
    await flushPromises()

    expect(removeFunction).toHaveBeenCalledTimes(1)
    expect(removeFunction).toHaveBeenCalledWith('a-1', 'f1')
    // 删除后刷新列表
    expect(listFunctions.mock.calls.length).toBeGreaterThanOrEqual(2)
  })
})

describe('ExternalAgentFormView manage – 添加功能（OpenAPI 追加导入）', () => {
  it('打开导入区块 → 解析 → confirm body 带 agent_id，成功后收起并刷新列表', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'close_defect',
          summary: 'Close defect',
          method: 'POST',
          path: '/api/v1/defects/close',
          fields: [],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
      ]),
    )
    importOpenapiConfirm.mockResolvedValueOnce({
      agent: AGENT,
      functions: [makeFunction('f9', { name: 'close_defect' })],
    })

    const wrapper = await mountManage()
    const listCallsBefore = listFunctions.mock.calls.length

    // 初始收起
    expect(wrapper.find('[data-testid="mgmt-import-section"]').exists()).toBe(false)

    await wrapper.find('[data-testid="mgmt-add-function"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="mgmt-import-section"]').exists()).toBe(true)
    // 追加提示出现
    expect(wrapper.text()).toContain('externalAgentWizard.manage.appendHint')

    // 复用导入面板：贴 URL → 解析
    await parseUrl(wrapper, 'http://10.50.54.233:9000/v3/api-docs')
    expect(importOpenapiPreview).toHaveBeenCalledWith({
      doc_url: 'http://10.50.54.233:9000/v3/api-docs',
    })

    // 确认导入（追加模式）
    await wrapper.find('[data-testid="mgmt-import-confirm"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(importOpenapiConfirm).toHaveBeenCalledTimes(1)
    const body = importOpenapiConfirm.mock.calls[0][0]
    expect(body.agent_id).toBe('a-1')
    expect(body.selected).toHaveLength(1)
    expect(body.selected[0]).toMatchObject({
      name: 'close_defect',
      method: 'POST',
      path: '/api/v1/defects/close',
    })

    // 成功后收起区块 + 刷新功能列表
    expect(wrapper.find('[data-testid="mgmt-import-section"]').exists()).toBe(false)
    expect(listFunctions.mock.calls.length).toBeGreaterThan(listCallsBefore)
  })
})

describe('ExternalAgentFormView manage – 追加去重反馈（skipped_existing）', () => {
  async function appendOnce(confirmResp: any) {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'close_defect',
          summary: 'Close defect',
          method: 'POST',
          path: '/api/v1/defects/close',
          fields: [],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
      ]),
    )
    importOpenapiConfirm.mockResolvedValueOnce(confirmResp)
    const wrapper = await mountManage()
    await wrapper.find('[data-testid="mgmt-add-function"]').trigger('click')
    await flushPromises()
    await parseUrl(wrapper, 'http://10.50.54.233:9000/v3/api-docs')
    await wrapper.find('[data-testid="mgmt-import-confirm"]').trigger('click')
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  it('1 创建 + 1 跳过：绿色创建数提示 + 琥珀跳过提示（count 走插值）', async () => {
    const wrapper = await appendOnce({
      agent: AGENT,
      functions: [makeFunction('f9', { name: 'close_defect' })],
      skipped_existing: [{ name: 'a', method: 'GET', path: '/a' }],
    })

    const success = wrapper.find('[data-testid="mgmt-append-success"]')
    expect(success.exists()).toBe(true)
    expect(success.text()).toContain('externalAgentWizard.manage.appendSuccess')

    const skipped = wrapper.find('[data-testid="mgmt-append-skipped"]')
    expect(skipped.exists()).toBe(true)
    expect(skipped.text()).toContain('externalAgentWizard.manage.appendSkipped')
    // 全跳过分支不出现
    expect(wrapper.find('[data-testid="mgmt-append-all-skipped"]').exists()).toBe(false)

    // 双语言消息均带 {count} 插值占位（t 被 mock 成返回 key，插值靠 locale 文件保证）
    expect((zhCN as any).externalAgentWizard.manage.appendSkipped).toContain('{count}')
    expect((enUS as any).externalAgentWizard.manage.appendSkipped).toContain('{count}')
  })

  it('0 创建 + 1 跳过：不显示「成功追加 0 个」，改用友好提示', async () => {
    const wrapper = await appendOnce({
      agent: AGENT,
      functions: [],
      skipped_existing: [{ name: 'a', method: 'GET', path: '/a' }],
    })

    expect(wrapper.find('[data-testid="mgmt-append-success"]').exists()).toBe(false)
    const allSkipped = wrapper.find('[data-testid="mgmt-append-all-skipped"]')
    expect(allSkipped.exists()).toBe(true)
    expect(allSkipped.text()).toContain('externalAgentWizard.manage.appendAllSkipped')
    // 跳过数提示仍然展示
    expect(wrapper.find('[data-testid="mgmt-append-skipped"]').exists()).toBe(true)
  })

  it('解析预览时已存在的 operation（method+path 命中）预取消勾选并打「已存在」标记', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          // f2 的 origin_meta 为 {method: 'post', path: '/api/v1/defects/import'} → 命中
          name: 'import_defect_file',
          summary: null,
          method: 'POST',
          path: '/api/v1/defects/import',
          fields: [],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
        {
          name: 'close_defect',
          summary: null,
          method: 'POST',
          path: '/api/v1/defects/close',
          fields: [],
          output_hint_suggestion: { display: 'json' },
          warnings: [],
        },
      ]),
    )

    const wrapper = await mountManage()
    await wrapper.find('[data-testid="mgmt-add-function"]').trigger('click')
    await flushPromises()
    await parseUrl(wrapper)

    const existsTag = wrapper.find('[data-testid="import-fn-exists-import_defect_file"]')
    expect(existsTag.exists()).toBe(true)
    expect(existsTag.text()).toContain('externalAgentWizard.import.fnExists')

    const existingCb = wrapper.find(
      '[data-testid="import-fn-checkbox-import_defect_file"]',
    ).element as HTMLInputElement
    expect(existingCb.checked).toBe(false)

    const newCb = wrapper.find(
      '[data-testid="import-fn-checkbox-close_defect"]',
    ).element as HTMLInputElement
    expect(newCb.checked).toBe(true)
    expect(wrapper.find('[data-testid="import-fn-exists-close_defect"]').exists()).toBe(false)
  })
})

describe('ExternalAgentFormView manage – 单功能字段编辑器', () => {
  const EDIT_SCHEMA = {
    order: ['status', 'limit'],
    fields: {
      status: {
        type: 'string',
        ui: 'select',
        label: 'Status',
        required: true,
        options: ['open', 'closed'],
        default: 'open',
        description: 'filter by status',
      },
      limit: { type: 'number', ui: 'number', label: 'Limit', default: 10 },
    },
  }

  async function mountEditable() {
    const f1 = makeFunction('f1', {
      name: 'query_defects',
      summary: '查询缺陷记录',
      sort_order: 2,
      input_schema: EDIT_SCHEMA,
    })
    return mountManage([f1])
  }

  it('点编辑展开编辑器：字段按 order 渲染且控件带当前值（select/number）', async () => {
    const wrapper = await mountEditable()

    expect(wrapper.find('[data-testid="fn-edit-f1"]').exists()).toBe(false)
    await wrapper.find('[data-testid="mgmt-fn-edit-f1"]').trigger('click')
    await flushPromises()

    const editor = wrapper.find('[data-testid="fn-edit-f1"]')
    expect(editor.exists()).toBe(true)
    expect(editor.find('[data-testid="fn-edit-field-status"]').exists()).toBe(true)
    expect(editor.find('[data-testid="fn-edit-field-limit"]').exists()).toBe(true)

    // status: string + ui=select + options → 下拉，当前值 open
    const statusDefault = editor.find('[data-testid="fn-edit-default-status"]')
    expect(statusDefault.exists()).toBe(true)
    expect((statusDefault.element as HTMLSelectElement).value).toBe('open')
    // limit: number → 数字输入，当前值 10
    const limitDefault = editor.find('[data-testid="fn-edit-default-limit"]')
    expect(limitDefault.exists()).toBe(true)
    expect((limitDefault.element as HTMLInputElement).value).toBe('10')

    // 取消收起
    await editor.find('[data-testid="fn-edit-cancel-f1"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="fn-edit-f1"]').exists()).toBe(false)
  })

  it('改默认值 + 说明后保存：updateFunction 提交完整 input_schema 并显示成功提示', async () => {
    const wrapper = await mountEditable()
    await wrapper.find('[data-testid="mgmt-fn-edit-f1"]').trigger('click')
    await flushPromises()
    const editor = wrapper.find('[data-testid="fn-edit-f1"]')

    await editor.find('[data-testid="fn-edit-default-limit"]').setValue('20')
    await editor.find('[data-testid="fn-edit-desc-status"]').setValue('按状态过滤')
    await editor.find('[data-testid="fn-edit-save-f1"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(updateFunction).toHaveBeenCalledTimes(1)
    expect(updateFunction).toHaveBeenCalledWith('a-1', 'f1', {
      input_schema: {
        order: ['status', 'limit'],
        fields: {
          status: {
            type: 'string',
            ui: 'select',
            label: 'Status',
            required: true,
            options: ['open', 'closed'],
            default: 'open',
            description: '按状态过滤',
          },
          limit: { type: 'number', ui: 'number', label: 'Limit', default: 20 },
        },
      },
      summary: '查询缺陷记录',
      sort_order: 2,
    })

    // 成功提示 + 编辑器收起 + 刷新列表
    expect(wrapper.find('[data-testid="mgmt-fn-saved"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="fn-edit-f1"]').exists()).toBe(false)
    expect(listFunctions.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('object 字段默认值非法 JSON：行内报错并禁用保存', async () => {
    const f1 = makeFunction('f1', {
      name: 'query_defects',
      summary: '查询缺陷记录',
      sort_order: 2,
      input_schema: {
        order: ['filter'],
        fields: { filter: { type: 'object', ui: 'textarea', label: 'Filter', default: { a: 1 } } },
      },
    })
    const wrapper = await mountManage([f1])
    await wrapper.find('[data-testid="mgmt-fn-edit-f1"]').trigger('click')
    await flushPromises()
    const editor = wrapper.find('[data-testid="fn-edit-f1"]')

    // 初始为合法 JSON（当前默认值序列化）
    const jsonArea = editor.find('[data-testid="fn-edit-default-filter"]')
    expect(jsonArea.exists()).toBe(true)
    expect((jsonArea.element as HTMLTextAreaElement).value).toContain('"a": 1')

    await jsonArea.setValue('{bad json')
    await flushPromises()
    expect(editor.text()).toContain('externalAgentWizard.interface.fieldDefaultInvalidJson')

    const saveBtn = editor.find('[data-testid="fn-edit-save-f1"]')
    expect(saveBtn.attributes('disabled')).toBeDefined()
    await saveBtn.trigger('click')
    await flushPromises()
    expect(updateFunction).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="mgmt-fn-saved"]').exists()).toBe(false)
  })
})

describe('ExternalAgentFormView 管理页 – 全部启用', () => {
  it('存在未启用功能时显示按钮；点击后逐个 PATCH active 并刷新列表', async () => {
    listAgents.mockResolvedValue([
      { id: 'a-1', name: 'Agent', org_id: 'o', endpoint: '', protocol: 'openai_compatible', capabilities: [], icon_emoji: null, theme_color: null, is_reachable: true, last_checked_at: null, created_at: '', updated_at: '', type: 'tool', status: 'active', version: 1 },
    ])
    listFunctions.mockResolvedValueOnce([
      { id: 'f1', agent_id: 'a-1', name: 'f1', summary: null, invoke_config: null, input_schema: null, status: 'draft', sort_order: 1, source: 'openapi_import', origin_meta: { method: 'get', path: '/x' }, version: 1, created_at: '', updated_at: '' },
      { id: 'f2', agent_id: 'a-1', name: 'f2', summary: null, invoke_config: null, input_schema: null, status: 'active', sort_order: 2, source: 'openapi_import', origin_meta: { method: 'post', path: '/y' }, version: 1, created_at: '', updated_at: '' },
    ]).mockResolvedValueOnce([
      { id: 'f1', agent_id: 'a-1', name: 'f1', summary: null, invoke_config: null, input_schema: null, status: 'active', sort_order: 1, source: 'openapi_import', origin_meta: { method: 'get', path: '/x' }, version: 1, created_at: '', updated_at: '' },
      { id: 'f2', agent_id: 'a-1', name: 'f2', summary: null, invoke_config: null, input_schema: null, status: 'active', sort_order: 2, source: 'openapi_import', origin_meta: { method: 'post', path: '/y' }, version: 1, created_at: '', updated_at: '' },
    ])
    updateFunction.mockResolvedValue({})

    currentRouteParams = { id: 'a-1' }
    const wrapper = await mountView()

    const btn = wrapper.find('[data-testid="mgmt-enable-all"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    await flushPromises()

    // 仅对 draft 的 f1 发了 PATCH，active 的 f2 不动
    expect(updateFunction).toHaveBeenCalledTimes(1)
    expect(updateFunction).toHaveBeenCalledWith('a-1', 'f1', { status: 'active' })
    // 刷新后全部 active → 按钮消失
    expect(wrapper.find('[data-testid="mgmt-enable-all"]').exists()).toBe(false)
  })
})

describe('导入面板 – 字段中文名(label)覆盖', () => {
  it('展开功能行可编辑字段中文名，confirm body 的 field_overrides 携带 label', async () => {
    importOpenapiPreview.mockResolvedValueOnce(
      previewResponse([
        {
          name: 'create_order', summary: 'Create', method: 'POST', path: '/orders',
          fields: [
            { key: 'sku', type: 'string', ui: 'input', label: 'sku', required: true },
          ],
          output_hint_suggestion: {}, warnings: [],
        },
      ]),
    )
    importOpenapiConfirm.mockResolvedValueOnce({
      agent: { id: 'a-label' },
      functions: [{ id: 'f1', name: 'create_order' }],
    })

    const wrapper = await mountView()
    await goToStep2(wrapper)
    await parseUrl(wrapper)

    // 展开功能行 → 中文名输入框填中文
    await wrapper.find('[data-testid="import-fn-expand-create_order"]').trigger('click')
    await flushPromises()
    const labelInput = wrapper.find('[data-testid="import-field-label-override"]')
    expect(labelInput.exists()).toBe(true)
    await labelInput.setValue('商品编号')
    await flushPromises()

    await clickNext(wrapper)
    const probeBtn = wrapper.findAll('button').find((b: any) => b.text().includes(K.IMPORT_AND_PROBE))
    await probeBtn!.trigger('click')
    await flushPromises()
    await flushPromises()

    const body = importOpenapiConfirm.mock.calls[0][0]
    expect(body.selected[0].field_overrides.sku.label).toBe('商品编号')
  })
})

describe('ExternalAgentFormView 管理页 – 全部试调', () => {
  const makeFn = (id: string, name: string, status = 'active') => ({
    id, agent_id: 'a-1', name, summary: null, invoke_config: null,
    input_schema: null, status, sort_order: 1, source: 'openapi_import',
    origin_meta: null, version: 1, created_at: '', updated_at: '',
  })

  it('点击「全部试调」逐个 probe 全部功能并显示汇总', async () => {
    listAgents.mockResolvedValue([
      { id: 'a-1', name: 'Agent', org_id: 'o', endpoint: '', protocol: 'openai_compatible', capabilities: [], icon_emoji: null, theme_color: null, is_reachable: true, last_checked_at: null, created_at: '', updated_at: '', type: 'tool', status: 'active', version: 1 },
    ])
    listFunctions.mockResolvedValue([makeFn('f1', 'fn_a'), makeFn('f2', 'fn_b'), makeFn('f3', 'fn_c')])
    probeFunction
      .mockResolvedValueOnce({ ok: true, reachable: true, latency_ms: 10, error: null })
      .mockResolvedValueOnce({ ok: false, reachable: false, latency_ms: 5, error: 'timeout' })
      .mockResolvedValueOnce({ ok: true, reachable: true, latency_ms: 8, error: null })

    currentRouteParams = { id: 'a-1' }
    const wrapper = await mountView()

    const btn = wrapper.find('[data-testid="mgmt-probe-all"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(probeFunction).toHaveBeenCalledTimes(3)
    expect(probeFunction).toHaveBeenCalledWith('a-1', 'f1')
    expect(probeFunction).toHaveBeenCalledWith('a-1', 'f2')
    expect(probeFunction).toHaveBeenCalledWith('a-1', 'f3')

    const summary = wrapper.find('[data-testid="mgmt-probe-all-summary"]')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('externalAgentWizard.manage.probeAllSummary')
  })

  it('无功能时不显示「全部试调」按钮', async () => {
    listAgents.mockResolvedValue([
      { id: 'a-1', name: 'Agent', org_id: 'o', endpoint: '', protocol: 'openai_compatible', capabilities: [], icon_emoji: null, theme_color: null, is_reachable: true, last_checked_at: null, created_at: '', updated_at: '', type: 'tool', status: 'active', version: 1 },
    ])
    listFunctions.mockResolvedValue([])

    currentRouteParams = { id: 'a-1' }
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="mgmt-probe-all"]').exists()).toBe(false)
  })
})
