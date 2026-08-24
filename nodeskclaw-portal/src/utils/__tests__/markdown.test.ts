/**
 * Tests for src/utils/markdown.ts — exercises renderMarkdown() and verifies
 * the spec §9.3 / §7.2 XSS contract.
 *
 * 覆盖：
 * 1. markdown 元素：行内 code / bold / code block / list / heading
 * 2. XSS 净化：<script> / onclick / onerror / javascript: URL / thinking 字段
 *
 * 已知环境差异：DOMPurify 3.x 在 happy-dom（vitest 默认环境）对混合 inline
 * 内容的深度 sanitize 不完整，因此 renderMarkdown 在 DOMPurify 输出后再过
 * 一遍 regex 兜底（详见 src/utils/markdown.ts）。浏览器生产环境两层同时
 * 工作；测试环境靠 regex 兜底保证安全。
 */
import { describe, expect, it } from 'vitest'
import { renderMarkdown } from '../markdown'

describe('renderMarkdown – markdown 元素（保证内容出现在输出里）', () => {
  it('行内 `code` 与 **bold** 内容均在', () => {
    const out = renderMarkdown('inline `code` and **bold**')
    expect(out).toContain('inline')
    expect(out).toContain('code')
    expect(out).toContain('bold')
  })

  it('代码块内容在输出里', () => {
    const out = renderMarkdown('```bash\necho hi\n```')
    expect(out).toContain('echo hi')
  })

  it('列表项文本在输出里', () => {
    const out = renderMarkdown('- item A\n- item B')
    expect(out).toContain('item A')
    expect(out).toContain('item B')
  })

  it('标题文本在输出里', () => {
    const out = renderMarkdown('# Heading')
    expect(out).toContain('Heading')
  })
})

describe('renderMarkdown – XSS 净化（spec §9.3 强制项）', () => {
  it('<script> 整块被剥离', () => {
    const out = renderMarkdown('hello <script>alert(1)</script> world')
    expect(out).not.toMatch(/<script/i)
    expect(out).not.toMatch(/alert\(1\)/i)
  })

  it('onclick 事件属性被剥除，href 保留', () => {
    const out = renderMarkdown(
      '<a href="https://example.com" onclick="alert(1)">click</a>',
    )
    expect(out).not.toMatch(/onclick=/i)
    expect(out).toMatch(/href="https:\/\/example\.com"/)
  })

  it('onerror 属性被剥除（混合 inline 内容场景）', () => {
    const out = renderMarkdown('text <img src=x onerror=alert(1)> more')
    expect(out).not.toMatch(/onerror=/i)
    // 原始无害内容仍保留
    expect(out).toContain('text')
    expect(out).toContain('more')
  })

  it('javascript: URL 被禁用', () => {
    const out = renderMarkdown('[click](javascript:alert(1))')
    expect(out).not.toMatch(/href="javascript:/i)
  })

  it('thinking 字段同样走净化（与 content 同一管线）', () => {
    const out = renderMarkdown('分析中 <img src=x onerror=alert(1)>')
    expect(out).not.toMatch(/onerror=/i)
    // 原始无害文字仍在
    expect(out).toContain('分析中')
  })
})