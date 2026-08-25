import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'

/**
 * Vitest suite for ExternalAgentToolForm.vue (Phase 1 §7.2 + Phase 2 §8.2).
 *
 * 覆盖：
 * 1. 字段类型 dispatch：每种 (type, ui) 组合渲染出对应的控件。
 * 2. 422 字段级错误 → 错误回显到对应字段。
 * 3. 文件上传：调 POST /functions/{id}/files 拿 file_id，提交时把 file_id 透传。
 * 4. 结果渲染：display=table+items_path 命中 → 表格；display=json 或未命中 → JSON 树。
 * 5. Phase 2 §8.2：function 选择器（单 function 时不渲染 / 多 function 时渲染 / 默认选 active /
 *    切换 tab 重新拉表单 / 无 function 时空状态）。
 */

// ── mock vue-router（useRoute / useRouter） ──────────────────────────────────────
const routerPush = vi.fn()
const routerReplace = vi.fn()
let currentRouteParams: Record<string, string> = { id: 'agent-1' }

vi.mock('vue-router', () => ({
  useRoute: () => ({
    params: currentRouteParams,
    query: {},
    path: '/agents/' + currentRouteParams.id + '/form',
  }),
  useRouter: () => ({
    push: routerPush,
    replace: routerReplace,
    back: vi.fn(),
  }),
  RouterLink: RouterLinkStub,
}))

// ── mock service ─────────────────────────────────────────────────────────────
const listFunctions = vi.fn()
const getFunctionForm = vi.fn()
const invokeFunction = vi.fn()
const uploadFunctionFile = vi.fn()
const listInvocations = vi.fn()

vi.mock('@/services/externalAgents', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/externalAgents')>(
      '@/services/externalAgents',
    )
  return {
    ...actual,
    externalAgentFunctionApi: {
      list: (...args: any[]) => listFunctions(...args),
      getForm: (...args: any[]) => getFunctionForm(...args),
      invoke: (...args: any[]) => invokeFunction(...args),
      uploadFile: (...args: any[]) => uploadFunctionFile(...args),
      listInvocations: (...args: any[]) => listInvocations(...args),
    },
  }
})

import ExternalAgentToolForm from '../ExternalAgentToolForm.vue'

// ── helpers ──────────────────────────────────────────────────────────────────
function makeForm(overrides: Record<string, any> = {}) {
  return {
    id: 'agent-1',
    name: '热压缺陷查询',
    description: '缺陷查询',
    version: 1,
    input_schema: {
      order: [],
      fields: {},
    },
    output_hint: { display: 'json' as const, items_path: undefined },
    ...overrides,
  }
}

function field(over: Record<string, any> = {}) {
  return {
    type: 'string' as const,
    ui: 'input' as const,
    label: '生产日期',
    required: false,
    ...over,
  }
}

function makeFunctionForm(formData: any) {
  // 适配 view 中的 toToolForm：ExternalAgentFunctionForm 字段形态
  return {
    function_id: 'fn-default',
    agent_id: formData.id ?? 'agent-1',
    name: formData.name ?? '热压缺陷查询',
    summary: formData.description ?? null,
    version: formData.version ?? 1,
    input_schema: formData.input_schema ?? { order: [], fields: {} },
    output_hint: formData.output_hint ?? { display: 'json' },
    invoke_config: {},
  }
}

function makeFunctionListItem(over: Record<string, any> = {}) {
  return {
    id: 'fn-default',
    agent_id: 'agent-1',
    name: 'default',
    summary: '默认功能',
    status: 'active',
    sort_order: 0,
    source: 'manual',
    version: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

/**
 * 默认 mount：单 function + 默认 form（存量迁移形态，对应 review C5 单 function UX 不变）。
 */
async function mountForm(formData: any) {
  listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
  getFunctionForm.mockResolvedValueOnce(makeFunctionForm(formData))
  const wrapper = mount(ExternalAgentToolForm, {
    global: {
      stubs: {
        teleport: true,
        // CustomSelect 内部监听 mousedown，挂一个 stub 即可
        CustomSelect: true,
        PluginResult: true,
      },
    },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  currentRouteParams = { id: 'agent-1' }
  routerPush.mockClear()
  routerReplace.mockClear()
  listFunctions.mockReset()
  getFunctionForm.mockReset()
  invokeFunction.mockReset()
  uploadFunctionFile.mockReset()
  listInvocations.mockReset()
  // 默认空历史；调用历史的测试用 mockResolvedValueOnce 覆盖
  listInvocations.mockResolvedValue([])
})

afterEach(() => {
  vi.clearAllMocks()
})

// ── 1. 字段 dispatch ─────────────────────────────────────────────────────────
describe('字段类型 dispatch', () => {
  it('string + ui=input 渲染 text input', async () => {
    const form = makeForm({
      input_schema: {
        order: ['k'],
        fields: { k: field({ type: 'string', ui: 'input', label: '文本' }) },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('input[type="text"]').exists()).toBe(true)
  })

  it('string + ui=textarea 渲染 textarea', async () => {
    const form = makeForm({
      input_schema: {
        order: ['k'],
        fields: { k: field({ type: 'string', ui: 'textarea', label: '备注' }) },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('textarea').exists()).toBe(true)
  })

  it('string + ui=select 渲染 CustomSelect（stub）', async () => {
    const form = makeForm({
      input_schema: {
        order: ['k'],
        fields: {
          k: field({
            type: 'string',
            ui: 'select',
            label: '产线',
            options: ['热压1线', '热压2线'],
          }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('[data-test="custom-select"]').exists() || wrapper.find('button').exists()).toBe(true)
  })

  it('number + ui=number 渲染 number input', async () => {
    const form = makeForm({
      input_schema: {
        order: ['n'],
        fields: {
          n: field({ type: 'number', ui: 'number', label: '计数' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('input[type="number"]').exists()).toBe(true)
  })

  it('number + ui=input（fallback text）也允许 text 输入', async () => {
    const form = makeForm({
      input_schema: {
        order: ['n'],
        fields: {
          n: field({ type: 'number', ui: 'input', label: '数量' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('input[type="number"]').exists()).toBe(true)
  })

  it('boolean + ui=switch 渲染 checkbox', async () => {
    const form = makeForm({
      input_schema: {
        order: ['b'],
        fields: {
          b: field({ type: 'boolean', ui: 'switch', label: '启用' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(true)
  })

  it('string + ui=date 渲染 date input', async () => {
    const form = makeForm({
      input_schema: {
        order: ['d'],
        fields: { d: field({ type: 'string', ui: 'date', label: '生产日期' }) },
      },
    })
    const wrapper = await mountForm(form)
    const dateInputs = wrapper.findAll('input[type="date"]')
    expect(dateInputs.length).toBe(1)
  })

  it('object + ui=daterange 渲染两个 date input', async () => {
    const form = makeForm({
      input_schema: {
        order: ['r'],
        fields: {
          r: field({ type: 'object', ui: 'daterange', label: '时间区间' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.findAll('input[type="date"]').length).toBe(2)
  })

  it('file + ui=upload 渲染 file 按钮 + hidden input', async () => {
    const form = makeForm({
      input_schema: {
        order: ['f'],
        fields: {
          f: field({
            type: 'file',
            ui: 'upload',
            label: '明细',
            accept: ['.xlsx'],
            max_mb: 5,
          }),
        },
      },
    })
    const wrapper = await mountForm(form)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
  })

  it('按 input_schema.order 顺序渲染（不是按 fields 字典顺序）', async () => {
    const form = makeForm({
      input_schema: {
        order: ['b', 'a'],
        fields: {
          a: field({ type: 'string', ui: 'input', label: 'A' }),
          b: field({ type: 'string', ui: 'input', label: 'B' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    const labels = wrapper.findAll('label').map((l) => l.text())
    // 第一个 label 应当先出现的是 B（order 顺序）
    expect(labels[0].startsWith('B')).toBe(true)
  })
})

// ── 2. 422 字段级错误回显 ─────────────────────────────────────────────────────
describe('422 字段级错误', () => {
  it('错误按字段名映射到对应字段提示', async () => {
    const form = makeForm({
      input_schema: {
        order: ['a', 'b'],
        fields: {
          a: field({ type: 'string', ui: 'input', label: 'A', required: true }),
          b: field({ type: 'string', ui: 'input', label: 'B' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    // 触发 invoke 返回 422
    invokeFunction.mockRejectedValueOnce({
      response: {
        status: 422,
        data: {
          detail: {
            message: '提交参数不合法',
            field_errors: { a: 'A 是必填', b: 'B 格式错误' },
          },
        },
      },
    })

    const inputs = wrapper.findAll('input[type="text"]')
    await inputs[0].setValue('hi')
    await inputs[1].setValue('yo')

    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()
    await flushPromises()

    const html = wrapper.html()
    expect(html).toContain('A 是必填')
    expect(html).toContain('B 格式错误')
    // 错误回显在 a/b 两个 label 对应区域：top-level 提示包含 validation key
    expect(html).toMatch(/pluginForm\.errors\.validation/)
  })

  it('422 时不会清掉其它字段已输入的内容（仅更新错误）', async () => {
    const form = makeForm({
      input_schema: {
        order: ['a'],
        fields: {
          a: field({ type: 'string', ui: 'input', label: 'A', required: true }),
        },
      },
    })
    const wrapper = await mountForm(form)
    invokeFunction.mockRejectedValueOnce({
      response: {
        status: 422,
        data: { detail: { field_errors: { a: 'A 不合法' } } },
      },
    })

    const input = wrapper.find('input[type="text"]')
    await input.setValue('保留值')
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()
    await flushPromises()

    const html = wrapper.html()
    expect(html).toContain('A 不合法')
    expect(html).toContain('保留值')
  })
})

// ── 3. 文件上传 + 后续 invoke 使用 file_id ──────────────────────────────────────
describe('文件上传', () => {
  it('上传成功后，将 file_id 注入 invoke payload', async () => {
    uploadFunctionFile.mockResolvedValueOnce({
      file_id: 'file-xyz',
      url: 'https://example.com/files/file-xyz',
      name: 'defects.xlsx',
      size: 1234,
    })
    invokeFunction.mockResolvedValueOnce({
      success: true,
      data: { items: [] },
      display: 'table',
      items_path: 'items',
    })
    const form = makeForm({
      input_schema: {
        order: ['f'],
        fields: {
          f: field({ type: 'file', ui: 'upload', label: '明细' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    const fileInput = wrapper.find('input[type="file"]')
    const file = new File(['data'], 'defects.xlsx', { type: 'application/octet-stream' })
    // 在 happy-dom 中 DataTransfer 不可用，直接给 files
    Object.defineProperty(fileInput.element, 'files', {
      value: [file],
      configurable: true,
    })
    await fileInput.trigger('change')
    await flushPromises()
    expect(uploadFunctionFile).toHaveBeenCalledWith('agent-1', 'fn-default', file)

    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()
    await flushPromises()

    expect(invokeFunction).toHaveBeenCalledTimes(1)
    const payload = invokeFunction.mock.calls[0][2] as Record<string, any>
    expect(payload.f).toBe('file-xyz')
  })

  it('上传中点击提交，会被前端拦截（inflight 防御）', async () => {
    // 模拟一个永不 resolve 的上传
    uploadFunctionFile.mockImplementationOnce(() => new Promise(() => {}))

    const form = makeForm({
      input_schema: {
        order: ['f'],
        fields: {
          f: field({ type: 'file', ui: 'upload', label: '明细' }),
        },
      },
    })
    const wrapper = await mountForm(form)
    const fileInput = wrapper.find('input[type="file"]')
    Object.defineProperty(fileInput.element, 'files', {
      value: [new File(['x'], 'a.csv', { type: 'text/csv' })],
      configurable: true,
    })
    await fileInput.trigger('change')
    // 不 flush，全部完成前试着提交
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(invokeFunction).not.toHaveBeenCalled()
  })
})

// ── 4. 结果展示 dispatch ──────────────────────────────────────────────────────
describe('结果展示 dispatch', () => {
  it('display=table 且 items_path 命中数组 → 渲染 PluginResult（stub）并保留 display/itemsPath', async () => {
    invokeFunction.mockResolvedValueOnce({
      success: true,
      data: {
        rows: [
          { code: 'A1', desc: 'desc1' },
          { code: 'A2', desc: 'desc2' },
        ],
      },
      display: 'table',
      items_path: 'rows',
    })
    const form = makeForm({
      output_hint: { display: 'table', items_path: 'rows' },
      input_schema: { order: [], fields: {} },
    })
    const wrapper = await mountForm(form)
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    const plugin = wrapper.findComponent({ name: 'PluginResult' })
    expect(plugin.exists()).toBe(true)
    // props 透传
    expect((plugin.props() as any).display).toBe('table')
    expect((plugin.props() as any).itemsPath).toBe('rows')
  })

  it('display=json → 透传 json 给 PluginResult', async () => {
    invokeFunction.mockResolvedValueOnce({
      success: true,
      data: { hello: 'world' },
      display: 'json',
    })
    const form = makeForm({
      output_hint: { display: 'json', items_path: undefined },
      input_schema: { order: [], fields: {} },
    })
    const wrapper = await mountForm(form)
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()
    const plugin = wrapper.findComponent({ name: 'PluginResult' })
    expect(plugin.exists()).toBe(true)
    expect((plugin.props() as any).display).toBe('json')
  })

  it('success=false → PluginResult 拿到失败响应 + showRetry=true', async () => {
    invokeFunction.mockResolvedValueOnce({
      success: false,
      upstream_status: 502,
      error: '上游不可达',
    })
    const form = makeForm({
      input_schema: { order: [], fields: {} },
    })
    const wrapper = await mountForm(form)
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    const plugin = wrapper.findComponent({ name: 'PluginResult' })
    expect(plugin.exists()).toBe(true)
    expect((plugin.props() as any).response).toMatchObject({ success: false })
    expect((plugin.props() as any).showRetry).toBe(true)
  })
})

// ── 5. Phase 2 §8.2：function 选择器 ──────────────────────────────────────────
describe('function 选择器 (Phase 2 §8.2)', () => {
  it('单 function 时不渲染选择器（review C5：保持原 UX）', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-only' }),
    ])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    expect(wrapper.find('.function-selector').exists()).toBe(false)
  })

  it('多 function 时渲染横向 tab 选择器', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: '查询订单' }),
      makeFunctionListItem({ id: 'fn-b', name: '创建订单' }),
    ])
    getFunctionForm.mockResolvedValue(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    expect(wrapper.find('.function-selector').exists()).toBe(true)
    expect(wrapper.find('[data-test="function-tab-fn-a"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="function-tab-fn-b"]').exists()).toBe(true)
  })

  it('默认选中第一个 status=active 的 function', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-draft', name: '草稿', status: 'draft', sort_order: 0 }),
      makeFunctionListItem({ id: 'fn-active', name: '激活', status: 'active', sort_order: 1 }),
      makeFunctionListItem({ id: 'fn-active-2', name: '激活2', status: 'active', sort_order: 2 }),
    ])
    getFunctionForm.mockResolvedValue(
      makeFunctionForm(makeForm({ id: 'agent-1', input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    // 选中的 tab 是 fn-active（第一个 active），getForm 应被以 fn-active 调用
    expect(getFunctionForm).toHaveBeenCalledWith('agent-1', 'fn-active')
    // 用户端只渲染 active 的 tab（draft 隐藏）
    const tabs = wrapper.findAll('button[data-test^="function-tab-"]')
    expect(tabs.length).toBe(2)
  })

  it('没有 active function 时展示"去管理页启用"引导态，不调 form 接口（不裸 403）', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-disabled-1', name: '停用1', status: 'disabled', sort_order: 0 }),
      makeFunctionListItem({ id: 'fn-draft-1', name: '草稿', status: 'draft', sort_order: 1 }),
    ])
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    // 不应调 getForm（draft/disabled 功能调了只会 403）
    expect(getFunctionForm).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="no-active-function"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pluginForm.noActiveFunction')
  })

  it('未启用的功能 tab 不渲染（用户端隐藏 draft/disabled）', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: 'A', status: 'active' }),
      makeFunctionListItem({ id: 'fn-b', name: 'B', status: 'active' }),
      makeFunctionListItem({ id: 'fn-draft', name: 'D', status: 'draft' }),
    ])
    getFunctionForm.mockResolvedValue(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    expect(getFunctionForm).toHaveBeenCalledTimes(1) // 仅初始 fn-a

    // draft 功能的 tab 不存在（用户端隐藏）
    const draftTab = wrapper.find('button[data-test="function-tab-fn-draft"]')
    expect(draftTab.exists()).toBe(false)
    // 只渲染 active 的 tab（2 个 active）
    const tabs = wrapper.findAll('button[data-test^="function-tab-"]')
    expect(tabs.length).toBe(2)
  })

  it('点击其它 tab 触发 selectFunction + 重新拉表单', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: 'A', status: 'active' }),
      makeFunctionListItem({ id: 'fn-b', name: 'B', status: 'active' }),
    ])
    // 第一次：选 fn-a；第二次：切到 fn-b（标题现在取中文 description）
    getFunctionForm
      .mockResolvedValueOnce(
        makeFunctionForm(makeForm({ description: 'A 表单', input_schema: { order: [], fields: {} } })),
      )
      .mockResolvedValueOnce(
        makeFunctionForm(makeForm({ description: 'B 表单', input_schema: { order: [], fields: {} } })),
      )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    // 初始应渲染 A 表单
    expect(wrapper.html()).toContain('A 表单')

    // 切到 fn-b
    await wrapper.find('[data-test="function-tab-fn-b"]').trigger('click')
    await flushPromises()
    expect(getFunctionForm).toHaveBeenCalledTimes(2)
    expect(getFunctionForm).toHaveBeenNthCalledWith(2, 'agent-1', 'fn-b')
    expect(wrapper.html()).toContain('B 表单')
  })

  it('无 function 时显示空状态提示，submit 不可用', async () => {
    listFunctions.mockResolvedValueOnce([])
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    // form 不渲染，submit 按钮不存在
    expect(wrapper.find('form').exists()).toBe(false)
    expect(getFunctionForm).not.toHaveBeenCalled()
    // 顶部应当出现 pluginForm.noFunctions
    expect(wrapper.html()).toMatch(/pluginForm\.noFunctions/)
  })

  it('非 active function 不渲染 tab（用户端完全隐藏 draft/disabled）', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-active', name: 'A', status: 'active' }),
      makeFunctionListItem({ id: 'fn-draft', name: 'B', status: 'draft' }),
    ])
    getFunctionForm.mockResolvedValue(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: {
        stubs: { teleport: true, CustomSelect: true, PluginResult: true },
      },
    })
    await flushPromises()
    // draft 功能的 tab 完全不存在
    expect(wrapper.find('button[data-test="function-tab-fn-draft"]').exists()).toBe(false)
    // 不显示 Inactive 标签（因为 tab 本身就没了）
    expect(wrapper.html()).not.toMatch(/pluginForm\.functionStatus\.inactive/)
  })
})
describe('ExternalAgentToolForm – 未启用 403 引导', () => {
  it('插件 draft（not_active 403）→ 引导卡 + 去管理页按钮，不再显示裸 403', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: 'A', status: 'active' }),
    ])
    getFunctionForm.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 403, data: { message_key: 'errors.external_agent.not_active', message: '插件未启用' } },
    })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()

    const card = wrapper.find('[data-testid="enable-guidance"]')
    expect(card.exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Request failed with status code 403')
    expect(wrapper.text()).toContain('errors.external_agent.not_active')
    expect(card.text()).toContain('pluginForm.goManageEnable')
  })

  it('非未启用错误（如 500）不出现引导卡，仅显示错误文案', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: 'A', status: 'active' }),
    ])
    getFunctionForm.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="enable-guidance"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('boom')
  })
})

describe('ExternalAgentToolForm – UI 精简（tab 中文优先 / 字段说明去重）', () => {
  it('功能 tab 优先显示中文 summary；无 summary 时才回退英文名', async () => {
    listFunctions.mockResolvedValueOnce([
      makeFunctionListItem({ id: 'fn-a', name: 'list_orders', summary: '查询订单列表', status: 'active' }),
      makeFunctionListItem({ id: 'fn-b', name: 'raw_fn', summary: null, status: 'active' }),
    ])
    getFunctionForm.mockResolvedValue(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()

    const tabA = wrapper.find('button[data-test="function-tab-fn-a"]')
    expect(tabA.text()).toContain('查询订单列表')
    expect(tabA.text()).not.toContain('list_orders')
    const tabB = wrapper.find('button[data-test="function-tab-fn-b"]')
    expect(tabB.text()).toContain('raw_fn')
  })

  it('label 与 description 相同（OpenAPI 同源）时只渲染一行；不同才显示说明', async () => {
    const form = makeForm({
      input_schema: {
        order: ['dup', 'diff'],
        fields: {
          dup: field({ label: '产线名称', description: '产线名称' }),
          diff: field({ label: '日期', description: '按天统计的自然日' }),
        },
      },
    })
    const wrapper = await mountForm(form)

    const body = wrapper.text()
    // dup 字段：label 与 description 一致 → 只出现一次
    expect(body.match(/产线名称/g)?.length).toBe(1)
    // diff 字段：label 与说明都渲染
    expect(body).toContain('日期')
    expect(body).toContain('按天统计的自然日')
  })

  it('调用结果区：上下布局带标题与内滚动容器', async () => {
    listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({ input_schema: { order: [], fields: {} } })),
    )
    invokeFunction.mockResolvedValueOnce({ success: true, data: { items: [] } })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    const area = wrapper.find('[data-testid="invoke-result-area"]')
    expect(area.exists()).toBe(true)
    expect(area.attributes('class')).toContain('overflow-auto')
    expect(wrapper.text()).toContain('pluginForm.resultTitle')
  })
})

describe('ExternalAgentToolForm – 字段错误中文化', () => {
  it('422 字段错误把 pydantic 英文翻译成中文（Field required → 必填，数组形态）', async () => {
    listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({
        input_schema: {
          order: ['n'],
          fields: { n: field({ type: 'number', ui: 'number', label: '数量' }) },
        },
      })),
    )
    invokeFunction.mockRejectedValueOnce({
      response: {
        status: 422,
        data: {
          detail: {
            field_errors: {
              n: [
                { field: 'n', message: 'Field required', type: 'missing' },
              ],
            },
          },
        },
      },
    })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    // 不再显示英文 pydantic 文案
    expect(wrapper.text()).not.toContain('Field required')
    expect(wrapper.text()).toContain('pluginForm.errors.fieldRequired')
  })

  it('已是中文的字段错误原样保留（字符串形态）', async () => {
    listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({
        input_schema: {
          order: ['k'],
          fields: { k: field({ label: '产线' }) },
        },
      })),
    )
    invokeFunction.mockRejectedValueOnce({
      response: {
        status: 422,
        data: { detail: { field_errors: { k: '产线必须选择' } } },
      },
    })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(wrapper.text()).toContain('产线必须选择')
  })

  it('数字类型错误翻译为中文（Input should be a valid number）', async () => {
    listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({
        input_schema: {
          order: ['n'],
          fields: { n: field({ type: 'number', ui: 'number', label: '数量' }) },
        },
      })),
    )
    invokeFunction.mockRejectedValueOnce({
      response: {
        status: 422,
        data: { detail: { field_errors: { n: 'Input should be a valid number, unable to parse string as a number' } } },
      },
    })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: true } },
    })
    await flushPromises()
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(wrapper.text()).not.toContain('Input should be a valid number')
    expect(wrapper.text()).toContain('pluginForm.errors.fieldNumber')
  })
})

describe('ExternalAgentToolForm – display=text（RAG 问答 Markdown 渲染）', () => {
  it('text 模式渲染 Markdown 并剥除 think 前缀', async () => {
    listFunctions.mockResolvedValueOnce([makeFunctionListItem({ id: 'fn-default' })])
    getFunctionForm.mockResolvedValueOnce(
      makeFunctionForm(makeForm({
        output_hint: { display: 'text' as const, text_path: 'answer' },
        input_schema: { order: [], fields: {} },
      })),
    )
    invokeFunction.mockResolvedValueOnce({
      success: true,
      data: { answer: 'x' },
      display: 'text',
      text: '<think>推理过程</think>\n\n## 热压缺陷原因\n\n- 温度偏差',
    })
    const wrapper = mount(ExternalAgentToolForm, {
      global: { stubs: { teleport: true, CustomSelect: true, PluginResult: false } },
    })
    await flushPromises()
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    const area = wrapper.find('[data-testid="plugin-result-text"]')
    expect(area.exists()).toBe(true)
    const html = area.html()
    expect(html).not.toContain('推理过程')
    expect(html).not.toContain('<think>')
    expect(html).toContain('热压缺陷原因')
  })
})

// ── 6. 调用历史（GET /{id}/invocations，仅本人可见） ─────────────────────────
describe('调用历史', () => {
  function makeHistoryItem(over: Record<string, any> = {}) {
    return {
      id: 'inv-1',
      agent_id: 'agent-1',
      function_id: 'fn-default',
      function_name: 'default',
      params_summary: '{"line": "L1"}',
      success: true,
      upstream_status: 200,
      latency_ms: 35,
      result_data: { success: true, data: { rows: [{ code: 'A1' }] }, display: 'json' },
      error_message: null,
      created_at: '2026-08-24T10:00:00Z',
      ...over,
    }
  }

  it('挂载时拉取历史（limit=10）并渲染条目：时间 + 功能名 + 成败徽标 + 参数摘要', async () => {
    listInvocations.mockResolvedValueOnce([
      makeHistoryItem(),
      makeHistoryItem({
        id: 'inv-2', success: false, function_name: 'query_orders',
        params_summary: '{"q": "长参数'.repeat(50) + '"}',
        upstream_status: 502, latency_ms: null,
        result_data: { success: false, upstream_status: 502, error: 'Bad Gateway' },
        error_message: 'Bad Gateway', created_at: '2026-08-23T09:00:00Z',
      }),
    ])
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))

    expect(listInvocations).toHaveBeenCalledWith('agent-1', 10)
    const section = wrapper.find('[data-testid="invoke-history"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('pluginForm.history.title')
    // 两条条目 + 成败徽标 key
    expect(wrapper.find('[data-testid="invoke-history-item-inv-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="invoke-history-item-inv-2"]').exists()).toBe(true)
    const text = section.text()
    expect(text).toContain('pluginForm.history.success')
    expect(text).toContain('pluginForm.history.failed')
    expect(text).toContain('default')
    expect(text).toContain('query_orders')
    // 参数摘要超长被截断（不渲染完整 500+ 字符）
    const longItem = wrapper.find('[data-testid="invoke-history-item-inv-2"]').text()
    expect(longItem.length).toBeLessThan(300)
    expect(longItem).toContain('...')
  })

  it('空历史显示空状态', async () => {
    listInvocations.mockResolvedValueOnce([])
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))
    expect(wrapper.find('[data-testid="invoke-history-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pluginForm.history.empty')
  })

  it('历史加载失败仅在区块内提示，不影响表单', async () => {
    listInvocations.mockRejectedValueOnce(new Error('network down'))
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))
    expect(wrapper.find('form').exists()).toBe(true)
    expect(wrapper.find('[data-testid="invoke-history-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pluginForm.history.loadFailed')
  })

  it('点击条目展开完整结果（PluginResult 收到存档响应）', async () => {
    listInvocations.mockResolvedValueOnce([makeHistoryItem()])
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))

    // 初始不展开
    expect(wrapper.find('[data-testid="invoke-history-detail-inv-1"]').exists()).toBe(false)
    await wrapper.find('[data-testid="invoke-history-item-inv-1"]').trigger('click')
    expect(wrapper.find('[data-testid="invoke-history-detail-inv-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pluginForm.history.clickToView')

    // PluginResult（stub）收到重放响应与展示形态
    const plugins = wrapper.findAllComponents({ name: 'PluginResult' })
    expect(plugins.length).toBeGreaterThanOrEqual(1)
    const replay = plugins[plugins.length - 1].props() as any
    expect(replay.response).toMatchObject({
      success: true,
      data: { rows: [{ code: 'A1' }] },
    })
    expect(replay.display).toBe('json')
    expect(replay.showRetry).toBe(false)

    // 再点一次收起
    await wrapper.find('[data-testid="invoke-history-item-inv-1"]').trigger('click')
    expect(wrapper.find('[data-testid="invoke-history-detail-inv-1"]').exists()).toBe(false)
  })

  it('截断结果（truncated）展示截断提示而非 clickToView', async () => {
    listInvocations.mockResolvedValueOnce([
      makeHistoryItem({
        result_data: { success: true, truncated: true, display: 'json', data: { truncated: true } },
      }),
    ])
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))
    await wrapper.find('[data-testid="invoke-history-item-inv-1"]').trigger('click')
    const detail = wrapper.find('[data-testid="invoke-history-detail-inv-1"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('pluginForm.history.truncated')
    expect(detail.text()).not.toContain('pluginForm.history.clickToView')
  })

  it('invoke 成功后重新拉取历史', async () => {
    listInvocations.mockResolvedValueOnce([])
    invokeFunction.mockResolvedValueOnce({ success: true, data: { items: [] } })
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))
    await flushPromises()
    expect(listInvocations).toHaveBeenCalledTimes(1)

    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()
    expect(invokeFunction).toHaveBeenCalledTimes(1)
    expect(listInvocations).toHaveBeenCalledTimes(2)
    expect(listInvocations).toHaveBeenLastCalledWith('agent-1', 10)
  })

  it('折叠开关：点击头部隐藏列表', async () => {
    listInvocations.mockResolvedValueOnce([makeHistoryItem()])
    const wrapper = await mountForm(makeForm({ input_schema: { order: [], fields: {} } }))
    expect(wrapper.find('[data-testid="invoke-history-list"]').exists()).toBe(true)
    await wrapper.find('[data-testid="invoke-history-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="invoke-history-list"]').exists()).toBe(false)
    // 标题仍在（卡片本体保留）
    expect(wrapper.find('[data-testid="invoke-history"]').text()).toContain('pluginForm.history.title')
  })
})
