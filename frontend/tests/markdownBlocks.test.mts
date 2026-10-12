import assert from 'node:assert/strict'

const mod = await import(new URL("../src/components/markdownBlocks.ts", import.meta.url).href)
const { parseMarkdown, parseInline } = mod
let passed = 0
function test(name: string, fn: () => void) {
  fn()
  passed++
  console.log('ok -', name)
}
const types = (md: string) => parseMarkdown(md).map((b: { type: string }) => b.type)

// The exact reply from the screenshot
const screenshot = `Here's a summary of your proposed Azure DevOps project based on your requirements and planned architecture:

---
### **Proposed Project Name**
Clinic Appointment Manager

### **Project Description**
Web application for a small clinic enabling patients to book, reschedule, and cancel appointments online. ---

This project proposal has been crafted to match your outlined needs.

**Next steps:**
Please review the project name and description above.
- If you'd like any changes, just let me know what you want to modify!
- If everything looks good, simply confirm and I'll proceed with creating the Azure DevOps project.`

test('screenshot: headings, dividers and bullets are separate blocks', () => {
  const blocks = parseMarkdown(screenshot)
  assert.deepEqual(blocks.map((b: { type: string }) => b.type), [
    'paragraph', 'hr', 'heading', 'paragraph', 'heading', 'paragraph', 'hr', 'paragraph', 'paragraph', 'list'])
  assert.equal(blocks[2].text, '**Proposed Project Name**')
  assert.equal(blocks[3].lines[0], 'Clinic Appointment Manager')
  assert.equal(blocks[4].level, 3)
  assert.ok(!blocks[5].lines[0].includes('---'))
  assert.deepEqual(blocks[8].lines, ['**Next steps:**', 'Please review the project name and description above.'])
  assert.equal(blocks[9].items.length, 2)
  assert.ok(blocks[9].items[0].text.startsWith("If you'd like"))
})

test('previous behaviour kept: blank-line separated paragraphs, headings, lists', () => {
  assert.deepEqual(types('## Objective\nText line one\nline two\n\n- a\n- b\n\n1. one\n2. two'),
    ['heading', 'paragraph', 'list', 'list'])
  const blocks = parseMarkdown('1. one\n2. two')
  assert.equal(blocks[0].ordered, true)
  assert.equal(parseMarkdown('3. three\n4. four')[0].start, 3)
})

test('nested bullets become a sub-list of their parent item', () => {
  const [list] = parseMarkdown('- **Framework:**\n  - React\n- **Language:** TypeScript')
  assert.equal(list.type, 'list')
  assert.equal(list.items.length, 2)
  assert.equal(list.items[0].children[0].items[0].text, 'React')
  assert.equal(list.items[1].children.length, 0)
})

test('screenshot: numbered questions with option bullets keep numbering 1, 2, 3, 4', () => {
  const md = [
    'Please help clarify the following:',
    '',
    '1. **What is the main purpose?** (For example: compliance, payroll)',
    '2. **Who are the primary user groups?**',
    '   - Employees',
    '   - Managers',
    '   - HR personnel',
    '   - System Administrators',
    '   - Other (please specify)',
    '3. **What are the three most important things users must be able to do?**',
    '4. **What is the primary platform?**',
    '   - Web application',
    '   - Mobile app',
    '',
    'You can answer in one message.',
  ].join('\n')
  const blocks = parseMarkdown(md)
  assert.deepEqual(blocks.map((b: { type: string }) => b.type), ['paragraph', 'list', 'paragraph'])
  const list = blocks[1]
  assert.equal(list.ordered, true)
  assert.equal(list.items.length, 4)                                  // 4 questions, not 11 items
  assert.equal(list.items[1].children[0].items.length, 5)            // options nested under question 2
  assert.equal(list.items[1].children[0].ordered, false)
  assert.equal(list.items[3].children[0].items.length, 2)
  assert.equal(list.items[2].children.length, 0)
})

test('loose numbered list with blank lines and nested options stays one list', () => {
  const [list] = parseMarkdown('1. First\n\n   - a\n   - b\n\n2. Second\n\n3. Third')
  assert.equal(list.items.length, 3)
  assert.equal(list.items[0].children[0].items.length, 2)
})

test('a paragraph between numbered items keeps the original number', () => {
  const blocks = parseMarkdown('1. One\nSome text\n2. Two')
  assert.deepEqual(blocks.map((b: { type: string }) => b.type), ['list', 'paragraph', 'list'])
  assert.equal(blocks[2].start, 2)
})

test('indented continuation text joins the current item', () => {
  const [list] = parseMarkdown('1. Question\n   (for example: X)')
  assert.equal(list.items[0].text, 'Question (for example: X)')
})

test('fenced code block (structure tree) is kept verbatim', () => {
  const tree = '**Proposed repository structure** (version 1, 3 files)\n\n```text\nrepo/\n├── src/\n│   └── main.go\n└── README.md\n```\nInvite text'
  const blocks = parseMarkdown(tree)
  assert.deepEqual(blocks.map((b: { type: string }) => b.type), ['paragraph', 'code', 'paragraph'])
  assert.equal(blocks[1].language, 'text')
  assert.equal(blocks[1].content, 'repo/\n├── src/\n│   └── main.go\n└── README.md')
})

test('markdown inside code blocks is not interpreted', () => {
  const blocks = parseMarkdown('```\n# not a heading\n- not a list\n---\n```')
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].content, '# not a heading\n- not a list\n---')
})

test('unterminated fence runs to the end', () => {
  const blocks = parseMarkdown('Intro\n```json\n{"a": 1}')
  assert.deepEqual(blocks.map((b: { type: string }) => b.type), ['paragraph', 'code'])
  assert.equal(blocks[1].content, '{"a": 1}')
})

test('tables', () => {
  const [table] = parseMarkdown('| Layer | Tech |\n|---|:---:|\n| Backend | FastAPI |\n| DB | PostgreSQL |')
  assert.equal(table.type, 'table')
  assert.deepEqual(table.header, ['Layer', 'Tech'])
  assert.deepEqual(table.rows, [['Backend', 'FastAPI'], ['DB', 'PostgreSQL']])
})

test('quotes and windows line endings', () => {
  assert.deepEqual(types('> note one\r\n> note two\r\n\r\nText'), ['quote', 'paragraph'])
})

test('a pipe line without separator is a paragraph', () => {
  assert.deepEqual(types('a | b | c'), ['paragraph'])
})

test('inline: code, link, bold, italic', () => {
  const tokens = parseInline('Use `npm run dev`, see [docs](https://example.com/x), **bold** and *italic*.')
  assert.deepEqual(tokens.map((t: { type: string }) => t.type), ['text', 'code', 'text', 'link', 'text', 'bold', 'text', 'italic', 'text'])
  assert.equal(tokens[3].href, 'https://example.com/x')
})

test('inline: unsafe links and globs stay text', () => {
  assert.deepEqual(parseInline('[x](javascript:alert(1))').map((t: { type: string }) => t.type), ['text'])
  assert.deepEqual(parseInline('match *.py and *.md files').map((t: { type: string }) => t.type), ['text'])
  assert.deepEqual(parseInline('/backend/app/__init__.py').map((t: { type: string }) => t.type), ['text'])
})

test('empty input', () => {
  assert.deepEqual(parseMarkdown(''), [])
})

console.log(`\n${passed} markdown tests passed`)
