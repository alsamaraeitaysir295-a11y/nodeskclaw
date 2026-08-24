import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

/**
 * Vitest suite for ExternalAgentChat.vue (Phase 1 任务 #9 §7.2)。
 *
 * 覆盖：
 * 1. Markdown 渲染：assistant 内容走 renderMarkdown()（DOMPurify + regex 兜底）。
 * 2. XSS 净化：注入 <script>alert(1)</script> / onclick / onerror / javascript:
 *    URL 应被剥除（spec §9.3 强制项）。
 * 3. Thinking 折叠：<details> / summary 渲染 + i18n label。
 *
 * 设计：
 * - 当前 ExternalAgentChat.vue 含较大副作用（SSE 流、外部 store），本测试聚焦
 *   markdown 渲染与 thinking 折叠两块 DOM 行为：直接构造 messages 即可。
 * - i18n 已在 tests/setup.ts 里 mock 成"返回 key"，可对具体 key 做断言。
 * - 已知环境差异：DOMPurify 3.x + happy-dom 对某些 markdown 块标签（<pre>/
 *   <ul>/<h1>）解析时会被剥离（浏览器生产环境正常）；断言只要求"内容文本
 *   出现在输出里"，不要求具体包裹标签。
 */

const routerPush = vi.fn()
const routerReplace = vi.fn()
let currentRouteParams: Record<string, string> = { id: 'agent-1' }

vi.mock('vue-router', () => ({
  useRoute: () => ({
    params: currentRouteParams,
    query: {},
    path: `/agents/${currentRouteParams.id}/chat`,
  }),
  useRouter: () => ({
    push: routerPush,
    replace: routerReplace,
    back: vi.fn(),
  }),
  RouterLink: { template: '<a><slot /></a>' },
}))

const listAgents = vi.fn().mockResolvedValue([])
const listSessions = vi.fn().mockResolvedValue([])
const getMessages = vi.fn().mockResolvedValue([])

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
      listSessions: (...args: any[]) => listSessions(...args),
      getMessages: (...args: any[]) => getMessages(...args),
      createSession: vi.fn().mockResolvedValue({
        id: 'sess-new', agent_id: 'agent-1', user_id: 'u1', org_id: 'o1',
        title: null, created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }),
      deleteSession: vi.fn().mockResolvedValue(undefined),
      chatStream: vi.fn().mockResolvedValue({ ok: true, body: null }),
      uploadAttachment: vi.fn(),
    },
  }
})

vi.mock('@/stores/externalAgents', () => ({
  useExternalAgentStore: () => ({
    agents: [
      {
        id: 'agent-1', name: 'Test Agent', description: null,
        endpoint: 'http://test', protocol: 'rag_standard',
        capabilities: [], icon_emoji: null, theme_color: null,
        is_reachable: true, last_checked_at: null,
        type: 'chat', status: 'active',
      },
    ],
    fetchAgents: vi.fn().mockResolvedValue([]),
  }),
}))

import ExternalAgentChat from '../ExternalAgentChat.vue'

beforeEach(() => {
  currentRouteParams = { id: 'agent-1' }
  listAgents.mockClear()
  listSessions.mockClear()
  getMessages.mockClear()
})

afterEach(() => {
  vi.clearAllMocks()
})

function stubHistory(messages: any[]) {
  getMessages.mockImplementation(async () => messages)
}

async function mountWithMessages(messages: any[]) {
  stubHistory(messages)
  listSessions.mockResolvedValueOnce([
    {
      id: 'sess-1', agent_id: 'agent-1', user_id: 'u1', org_id: 'o1',
      title: 'Test session',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    },
  ])
  const wrapper = mount(ExternalAgentChat, {
    global: {
      stubs: {
        teleport: true,
      },
    },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('ExternalAgentChat – Markdown 渲染（renderMarkdown + DOMPurify）', () => {
  it('代码块内容出现在输出里', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '```bash\necho hi\n```',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    expect(wrapper.html()).toContain('echo hi')
  })

  it('列表项文本出现在输出里', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '- item A\n- item B',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(html).toContain('item A')
    expect(html).toContain('item B')
  })

  it('行内 code 与 bold 内容均在', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '使用 `npm` 运行 **测试**',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    expect(wrapper.html()).toContain('npm')
    expect(wrapper.html()).toContain('测试')
  })

  it('链接被保留 href（DOMPurify hook 注入 rel）', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '见 [官网](https://example.com)',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    expect(wrapper.html()).toMatch(/href="https:\/\/example\.com"/)
  })

  it('标题文本在输出里', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '# 总结',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    expect(wrapper.html()).toContain('总结')
  })
})

describe('ExternalAgentChat – XSS 净化（DOMPurify + regex 兜底）', () => {
  it('<script>alert(1)</script> 被完全剥离', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant',
        content: 'hello <script>alert(1)</script> world',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(html).not.toMatch(/<script\b/i)
    // 原始无害文字保留
    expect(html).toContain('hello')
    expect(html).toContain('world')
  })

  it('onclick 事件属性被剥除，href 保留', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant',
        content: '<a href="https://example.com" onclick="alert(1)">click</a>',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(html).not.toMatch(/onclick=/i)
    expect(html).toMatch(/href="https:\/\/example\.com"/)
  })

  it('onerror 属性被剥除', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant',
        content: 'text <img src=x onerror=alert(1)> more',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(html).not.toMatch(/onerror=/i)
  })

  it('javascript: URL 链接被禁用', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant',
        content: '[click](javascript:alert(1))',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(html).not.toMatch(/href="javascript:/i)
  })

  it('thinking 字段同样走净化；<img onerror> 被剥除', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: 'ok',
        thinking: '分析中 <img src=x onerror=alert(1)>',
        attachments: null, created_at: new Date().toISOString() },
    ])
    const html = wrapper.html()
    expect(wrapper.findAll('details').length).toBeGreaterThanOrEqual(1)
    expect(html).not.toMatch(/onerror=/i)
    expect(html).not.toMatch(/<img[^>]*onerror/i)
  })
})

describe('ExternalAgentChat – Thinking 折叠 (i18n + details)', () => {
  it('带 thinking 的 assistant 消息有 <details> 与 i18n 标签', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '答复',
        thinking: '正在思考',
        attachments: null, created_at: new Date().toISOString() },
    ])
    const details = wrapper.findAll('details')
    expect(details.length).toBe(1)
    const summary = details[0].find('summary')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toBe('externalAgentChat.thinkingSummary')
  })

  it('没有 thinking 的 assistant 消息不渲染 <details>', async () => {
    const wrapper = await mountWithMessages([
      { id: 'm1', session_id: 'sess-1', role: 'assistant', content: '纯答复',
        thinking: null, attachments: null, created_at: new Date().toISOString() },
    ])
    expect(wrapper.findAll('details').length).toBe(0)
  })
})