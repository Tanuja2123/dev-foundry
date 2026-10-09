// Line-based Markdown block parser for chat messages.
// Every line is classified on its own, so headings, dividers and lists render correctly even when the
// model puts them on adjacent lines without blank lines in between.

export type ListItem = { text: string; depth: number }

export type Block =
  | { type: 'code'; language: string; content: string }
  | { type: 'heading'; level: 1 | 2 | 3 | 4; text: string }
  | { type: 'hr' }
  | { type: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { type: 'quote'; lines: string[] }
  | { type: 'table'; header: string[]; rows: string[][] }
  | { type: 'paragraph'; lines: string[] }

const FENCE = /^\s*```\s*([\w+-]*)\s*$/
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/
const RULE = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/
const TRAILING_RULE = /^(.*\S)\s+-{3,}\s*$/
const BULLET = /^(\s*)[-*+•]\s+(.*)$/
const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/
const QUOTE = /^\s*>\s?(.*)$/
const TABLE_ROW = /^\s*\|.*\|\s*$/
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
}

export function parseMarkdown(content: string): Block[] {
  const lines = (content ?? '').replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let paragraph: string[] = []

  function flushParagraph() {
    if (paragraph.length) blocks.push({ type: 'paragraph', lines: paragraph })
    paragraph = []
  }

  function lastBlock(): Block | undefined {
    return blocks[blocks.length - 1]
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Fenced code block (an unterminated fence runs to the end)
    const fence = line.match(FENCE)
    if (fence) {
      flushParagraph()
      const code: string[] = []
      i++
      while (i < lines.length && !FENCE.test(lines[i])) {
        code.push(lines[i])
        i++
      }
      blocks.push({ type: 'code', language: fence[1] || '', content: code.join('\n').replace(/\s+$/, '') })
      continue
    }

    if (!line.trim()) {
      flushParagraph()
      continue
    }

    if (RULE.test(line)) {
      flushParagraph()
      blocks.push({ type: 'hr' })
      continue
    }

    const heading = line.match(HEADING)
    if (heading) {
      flushParagraph()
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4
      blocks.push({ type: 'heading', level, text: heading[2] })
      continue
    }

    // Table: header row followed by a separator row
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
      flushParagraph()
      const header = splitRow(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(splitRow(lines[i]))
        i++
      }
      i--
      blocks.push({ type: 'table', header, rows })
      continue
    }

    const bullet = line.match(BULLET)
    const ordered = bullet ? null : line.match(ORDERED)
    if (bullet || ordered) {
      flushParagraph()
      const indent = (bullet ? bullet[1] : ordered![1]).replace(/\t/g, '    ').length
      const item: ListItem = { text: bullet ? bullet[2] : ordered![3], depth: Math.min(Math.floor(indent / 2), 4) }
      const isOrdered = Boolean(ordered)
      const previous = lastBlock()
      // Nested items join the list above them; top-level items join a list of the same kind
      if (previous && previous.type === 'list' && (item.depth > 0 || previous.ordered === isOrdered)) {
        previous.items.push(item)
      } else {
        blocks.push({ type: 'list', ordered: isOrdered, start: ordered ? Number(ordered[2]) : 1, items: [item] })
      }
      continue
    }

    const quote = line.match(QUOTE)
    if (quote) {
      flushParagraph()
      const previous = lastBlock()
      if (previous && previous.type === 'quote') previous.lines.push(quote[1])
      else blocks.push({ type: 'quote', lines: [quote[1]] })
      continue
    }

    // "Some text ---": text followed by a divider on the same line
    const trailingRule = line.match(TRAILING_RULE)
    if (trailingRule) {
      paragraph.push(trailingRule[1])
      flushParagraph()
      blocks.push({ type: 'hr' })
      continue
    }

    // Indented continuation of a list item
    const previous = lastBlock()
    if (!paragraph.length && previous && previous.type === 'list' && /^\s{2,}\S/.test(line) && i > 0 && lines[i - 1].trim()) {
      previous.items[previous.items.length - 1].text += ' ' + line.trim()
      continue
    }

    paragraph.push(line.trim())
  }
  flushParagraph()
  return blocks
}

// Inline tokens: `code`, [link](https://...), **bold**, *italic*
export type InlineToken =
  | { type: 'text'; text: string }
  | { type: 'code'; text: string }
  | { type: 'link'; text: string; href: string }
  | { type: 'bold'; text: string }
  | { type: 'italic'; text: string }

// Italic needs non-space characters right inside both asterisks, so "*.py and *.md" stays plain text
const INLINE = /(`[^`]+`)|(\[[^\]]+\]\(https?:\/\/[^)\s]+\))|(\*\*[^*]+\*\*)|(\*[^*\s](?:[^*]*[^*\s])?\*)/g

export function parseInline(text: string): InlineToken[] {
  const tokens: InlineToken[] = []
  let last = 0
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0
    if (index > last) tokens.push({ type: 'text', text: text.slice(last, index) })
    const value = match[0]
    if (match[1]) {
      tokens.push({ type: 'code', text: value.slice(1, -1) })
    } else if (match[2]) {
      const link = value.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/)!
      tokens.push({ type: 'link', text: link[1], href: link[2] })
    } else if (match[3]) {
      tokens.push({ type: 'bold', text: value.slice(2, -2) })
    } else {
      tokens.push({ type: 'italic', text: value.slice(1, -1) })
    }
    last = index + value.length
  }
  if (last < text.length) tokens.push({ type: 'text', text: text.slice(last) })
  return tokens
}
