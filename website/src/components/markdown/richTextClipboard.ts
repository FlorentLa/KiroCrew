/**
 * Markdown -> clean HTML for "Copy as rich text".
 *
 * Pasting into a rich-text editor (Outlook, Word, Google Docs) wants HTML, but
 * the HTML the chat pane renders carries the dashboard's own chrome: utility
 * classes, inline styles, path chips, code-block toolbars and bordered
 * containers. Copying that selection drags every bit of it into the email.
 *
 * So this converts the reply's Markdown SOURCE afresh, with no dashboard
 * components in the way, and keeps only semantic tags: headings, paragraphs,
 * lists, tables, links, emphasis, quotes and code. Every class, style, data
 * attribute and event handler is dropped, because the receiving editor
 * supplies its own typography and must not inherit ours.
 *
 * Safety: raw HTML in the Markdown is never passed through (the hast
 * conversion runs without `allowDangerousHtml`), and a link or image keeps its
 * URL only when it is absolute and on an allowed scheme. A relative link points
 * into this dashboard and means nothing in an email, so it is unwrapped to its
 * text; a relative image becomes its alt text.
 */
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkCjkFriendly from 'remark-cjk-friendly'
import remarkCjkFriendlyGfmStrikethrough from 'remark-cjk-friendly-gfm-strikethrough'
import { toHast } from 'mdast-util-to-hast'
import { toHtml } from 'hast-util-to-html'
import type { Element, ElementContent, Properties, Root as HastRoot, RootContent } from 'hast'
import type { Root as MdastRoot } from 'mdast'
import { remarkBoundDepth } from '../../utils/markdownDepthBound'

/** Tags kept as themselves. Anything else is unwrapped to its children. */
const KEEP_TAGS = new Set([
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
  'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
  'strong', 'em', 'del', 'a', 'img',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'sup', 'section', 'input',
])

/** Attributes kept per tag; every other property is dropped. */
const KEEP_PROPS: Record<string, readonly string[]> = {
  a: ['href', 'id'],
  img: ['src', 'alt', 'title'],
  ol: ['start'],
  th: ['align'],
  td: ['align'],
  li: ['id'],
  sup: [],
  input: ['type', 'checked', 'disabled'],
}

const SAFE_SCHEMES = /^(https?|mailto):/i

/** Absolute URL on an allowed scheme, or a same-document fragment (GFM
 *  footnote back-references). Anything else is not a link worth pasting. */
function keepUrl(url: unknown, allowFragment: boolean): url is string {
  if (typeof url !== 'string') return false
  const trimmed = url.trim()
  if (allowFragment && trimmed.startsWith('#')) return true
  return SAFE_SCHEMES.test(trimmed)
}

function cleanProps(tag: string, props: Properties | undefined): Properties {
  const out: Properties = {}
  const allowed = KEEP_PROPS[tag]
  if (!allowed || !props) return out
  for (const key of allowed) {
    if (props[key] !== undefined && props[key] !== null) out[key] = props[key]
  }
  return out
}

function cleanChildren(children: readonly (RootContent | ElementContent)[]): ElementContent[] {
  const out: ElementContent[] = []
  // Iterative over siblings, recursive over depth: the tree's depth is already
  // bounded by `remarkBoundDepth`, so recursion here cannot overflow.
  for (const child of children) {
    if (child.type === 'text') { out.push(child); continue }
    if (child.type !== 'element') continue  // comments, doctype, raw
    out.push(...cleanElement(child))
  }
  return out
}

function cleanElement(el: Element): ElementContent[] {
  const tag = el.tagName
  const kids = cleanChildren(el.children)
  if (!KEEP_TAGS.has(tag)) return kids
  if (tag === 'a') {
    if (!keepUrl(el.properties?.href, true)) return kids
  }
  if (tag === 'img') {
    if (!keepUrl(el.properties?.src, false)) {
      const alt = el.properties?.alt
      return typeof alt === 'string' && alt ? [{ type: 'text', value: alt }] : []
    }
  }
  if (tag === 'input' && el.properties?.type !== 'checkbox') return []
  return [{ type: 'element', tagName: tag, properties: cleanProps(tag, el.properties), children: kids }]
}

const processor = unified()
  .use(remarkParse)
  .use(remarkBoundDepth)
  .use(remarkCjkFriendly)
  .use(remarkGfm)
  .use(remarkCjkFriendlyGfmStrikethrough)

/** Convert Markdown to an HTML fragment of semantic tags only. */
export function markdownToCleanHtml(markdown: string): string {
  const mdast = processor.runSync(processor.parse(markdown)) as MdastRoot
  const hast = toHast(mdast) as HastRoot
  const root: HastRoot = { type: 'root', children: cleanChildren(hast.children) }
  return toHtml(root)
}
