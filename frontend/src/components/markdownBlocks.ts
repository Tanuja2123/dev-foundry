// Line-based Markdown block parser for chat messages.
// Every line is classified on its own, so headings, dividers and lists render correctly even when the
// model puts them on adjacent lines without blank lines in between. Indented list items become real
// nested lists inside their parent item (so "1." question + "- option" bullets keep the numbering 1, 2, 3).

export type ListBlock = { type: 'list'; ordered: boolean; start: number; items: ListItem[] }
export type ListItem = { text: string; children: ListBlock[] }

export type Block =
  | { type: 'code'; language: string; content: string }
  | { type: 'heading'; level: 1 | 2 | 3 | 4; text: string }
  | { type: 'hr' }
  | ListBlock
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

function indentOf(spaces: string): number {
  return spaces.replace(/\t/g, '    ').length
}

export function parseMarkdown(content: string): Block[] {
  const lines = (content ?? '').replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let paragraph: string[] = []
  // Open lists, outermost first, with the indentation of their items
  let listStack: { list: ListBlock; indent: number }[] = []

  function flushParagraph() {
    if (paragraph.length) blocks.push({ type: 'paragraph', lines: paragraph })
    paragraph = []
  }

  function closeLists() {
    listStack = []
  }

  function addListItem(indent: number, ordered: boolean, start: number, text: string) {
    const item: ListItem = { text, children: [] }
    while (listStack.length && listStack[listStack.length - 1].indent > indent) listStack.pop()
    const top = listStack[listStack.length - 1]

    if (top && top.indent === indent && top.list.ordered === ordered) {
      top.list.items.push(item)
      return
    }
    const list: ListBlock = { type: 'list', ordered, start, items: [item] }
    if (top && top.indent < indent) {
      // Deeper indentation: a nested list inside the current item
      top.list.items[top.list.items.length - 1].children.push(list)
      listStack.push({ list, indent })
      return
    }
    if (top && top.indent === indent && listStack.length > 1) {
      // Same level but a different list kind: a sibling list inside the same parent item
      listStack.pop()
      const parent = listStack[listStack.length - 1]
      parent.list.items[parent.list.items.length - 1].children.push(list)
      listStack.push({ list, indent })
      return
    }
    blocks.push(list)
    listStack = [{ list, indent }]
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Fenced code block (an unterminated fence runs to the end)
    const fence = line.match(FENCE)
    if (fence) {
      flushParagraph()
      closeLists()
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
      flushParagraph() // lists stay open across blank lines ("loose" lists)
      continue
    }

    if (RULE.test(line)) {
      flushParagraph()
      closeLists()
      blocks.push({ type: 'hr' })
      continue
    }

    const heading = line.match(HEADING)
    if (heading) {
      flushParagraph()
      closeLists()
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4
      blocks.push({ type: 'heading', level, text: heading[2] })
      continue
    }

    // Table: header row followed by a separator row
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
      flushParagraph()
      closeLists()
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
    if (bullet) {
      flushParagraph()
      addListItem(indentOf(bullet[1]), false, 1, bullet[2])
      continue
    }
    if (ordered) {
      flushParagraph()
      addListItem(indentOf(ordered[1]), true, Number(ordered[2]), ordered[3])
      continue
    }

    // Indented continuation of the current list item (no blank line before it)
    if (!paragraph.length && listStack.length && /^\s{2,}\S/.test(line) && i > 0 && lines[i - 1].trim()) {
      const list = listStack[listStack.length - 1].list
      list.items[list.items.length - 1].text += ' ' + line.trim()
      continue
    }

    closeLists()

    const quote = line.match(QUOTE)
    if (quote) {
      flushParagraph()
      const previous = blocks[blocks.length - 1]
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
