/**
 * 真实 vue-router 集成测试：验证创建向导 →「前往编辑页/仅导入」的跳转。
 *
 * 背景：主测试文件 mock 了 vue-router，测不出导航集成问题（用户实测
 * "点击前往编辑页没有跳转"）。本文件用 createRouter + memory history
 * 挂真实路由表（只挂外部 Agent 相关路由），不 mock useRouter/useRoute。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

const listAgents = vi.fn()
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

beforeEach(() => {
  listAgents.mockReset()
  importOpenapiPreview.mockReset()
  importOpenapiConfirm.mockReset()
  probeFunction.mockReset()
  listFunctions.mockReset()
  updateFunction.mockReset()
  removeFunction.mockReset()
  listAgents.mockResolvedValue([])
})

function buildRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div>home</div>' } },
      { path: '/agents', name: 'ExternalAgentList', component: { template: '<div>list</div>' } },
      {
        path: '/org-settings/external-agents/new',
        name: 'ExternalAgentNew',
        component: ExternalAgentFormView,
      },
      {
        path: '/org-settings/external-agents/:id/edit',
        name: 'ExternalAgentEdit',
        component: ExternalAgentFormView,
      },
    ],
  })
}

function previewResponse(functions: any[]) {
  return {
    spec_version: '3.0.0',
    servers: [{ url: 'https://api.example.com' }],
    functions,
    warnings: [],
  }
}

const ONE_FN = [
  { name: 'fn_a', summary: 'A', method: 'GET', path: '/a', fields: [], output_hint_suggestion: {}, warnings: [] },
]

async function mountAtNew() {
  const router = buildRouter()
  router.push('/org-settings/external-agents/new')
  await router.isReady()
  const wrapper = mount(ExternalAgentFormView, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

async function fillWizardToStep3(wrapper: any) {
  // step 1: 名称
  const nameInput = wrapper
    .findAll('input')
    .find((i: any) => (i.attributes('placeholder') || '').includes('namePlaceholder'))
  await nameInput!.setValue('Real Router Plugin')
  await flushPromises()
  await wrapper.findAll('button').find((b: any) => b.text().includes('externalAgentWizard.next'))!.trigger('click')
  await flushPromises()
  // step 2: URL 解析
  await wrapper.find('[data-testid="import-url-input"]').setValue('http://api.example.com/v3/api-docs')
  await wrapper.find('[data-testid="import-parse-button"]').trigger('click')
  await flushPromises()
  await flushPromises()
  // → step 3
  await wrapper.findAll('button').find((b: any) => b.text().includes('externalAgentWizard.next'))!.trigger('click')
  await flushPromises()
}

describe('ExternalAgentFormView 真实 router 导航', () => {
  it('导入并试调后,点「前往编辑页」真实跳转到编辑路由', async () => {
    listAgents.mockResolvedValue([])
    importOpenapiPreview.mockResolvedValue(previewResponse(ONE_FN))
    importOpenapiConfirm.mockResolvedValue({
      agent: { id: 'agent-real-1' },
      functions: [{ id: 'f1', name: 'fn_a' }],
    })
    probeFunction.mockResolvedValue({ ok: true, reachable: true, latency_ms: 8, error: null })

    const { wrapper, router } = await mountAtNew()
    await fillWizardToStep3(wrapper)

    const importBtn = wrapper
      .findAll('button')
      .find((b: any) => b.text().includes('externalAgentWizard.import.importAndProbe'))
    await importBtn!.trigger('click')
    await flushPromises()
    await flushPromises()

    const gotoBtn = wrapper.find('[data-testid="import-goto-edit"]')
    expect(gotoBtn.exists()).toBe(true)
    await gotoBtn.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('ExternalAgentEdit')
    expect(router.currentRoute.value.params.id).toBe('agent-real-1')
    expect(router.currentRoute.value.fullPath).toBe(
      '/org-settings/external-agents/agent-real-1/edit',
    )
  })

  it('点「仅导入（草稿）」:confirm 一次后真实跳转编辑路由', async () => {
    listAgents.mockResolvedValue([])
    importOpenapiPreview.mockResolvedValue(previewResponse(ONE_FN))
    importOpenapiConfirm.mockImplementation((body: any) => {
      // eslint-disable-next-line no-console
      console.log('== CONFIRM CALLED ==', JSON.stringify(body).slice(0, 120))
      return Promise.resolve({
        agent: { id: 'agent-real-2' },
        functions: [{ id: 'f1', name: 'fn_a' }],
      })
    })

    const { wrapper, router } = await mountAtNew()
    await fillWizardToStep3(wrapper)

    const draftBtn = wrapper
      .findAll('button')
      .find((b: any) => b.text().includes('externalAgentWizard.import.importDraftOnly'))
    expect(draftBtn).toBeTruthy()
    await draftBtn!.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(importOpenapiConfirm).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('ExternalAgentEdit')
    expect(router.currentRoute.value.params.id).toBe('agent-real-2')
  })
})
