import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ breaks: true, gfm: true })

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

// ── 防御层（belt-and-suspenders，DOMPurify 之后再过一遍） ───────────────────
//
// 已知环境差异：
//   - 真实浏览器里 DOMPurify 3.x 自身就能正确净化 <script> / on*= / javascript:，
//     并保留 <pre> / <ul> / <h1> 等合法标签。
//   - happy-dom（vitest 默认测试环境）+ DOMPurify 3.x 在混合 inline 内容上
//     净化深度不够：``<p>foo <img onerror=...> bar`` 残留 onerror 不被剥离。
//     对应的 PR / issue 在 DOMPurify 与 happy-dom 仓库都有人报过。
//
// 因此 renderMarkdown 在 DOMPurify 输出后再用 regex 扫一次剩余的危险 pattern：
// script 整块、所有 on*= 事件属性、javascript: URL。这属于 defense-in-depth：
// 两层（DOMPurify + regex）同时被绕过才会发生 XSS。代码块里的字面量（"<pre><code>"
// 之内）不会被误伤，因为 pattern 只匹配真正 HTML 标签 / 属性，不动纯文本。
const _SCRIPT_BLOCK_RE = /<script\b[^>]*>[\s\S]*?<\/script\s*>/gi
const _EVENT_ATTR_RE = /\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi
const _JS_URL_RE = /\b(?:href|src|action|formaction|background|poster|cite|data)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]+)/gi

function _strip_dangerous_patterns(html: string): string {
  return html
    .replace(_SCRIPT_BLOCK_RE, '')
    .replace(_EVENT_ATTR_RE, '')
    .replace(_JS_URL_RE, '')
}

export function renderMarkdown(content: string): string {
  if (!content) return ''
  const raw = marked.parse(content, { async: false }) as string
  // DOMPurify 在前负责结构清理 + happy-dom 能识别的 XSS；
  // regex 在后兜底 happy-dom 漏掉的混合 inline XSS。
  const after_dompurify = DOMPurify.sanitize(raw)
  return _strip_dangerous_patterns(after_dompurify)
}