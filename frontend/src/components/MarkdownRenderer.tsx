import { useState, type ReactNode } from 'react'
import { parseInline, parseMarkdown, type Block } from './markdownBlocks'

type MarkdownRendererProps = {
  content: string
}

function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard failed
    }
  }

  return (
    <div className="message-code-block">
      <div className="message-code-header">
        <span>{language || 'text'}</span>
        <button type="button" className="copy-button" onClick={() => void handleCopy()}>
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

function formatInline(text: string, keyPrefix = ''): ReactNode[] {
  return parseInline(text).map((token, i) => {
    const key = `${keyPrefix}${i}`
    switch (token.type) {
      case 'code':
        return <code key={key}>{token.text}</code>
      case 'link':
        // Only http(s) URLs are turned into anchors
        return (
          <a key={key} href={token.href} target="_blank" rel="noopener noreferrer">
            {formatInline(token.text, `${key}-`)}
          </a>
        )
      case 'bold':
        return <strong key={key}>{formatInline(token.text, `${key}-`)}</strong>
      case 'italic':
        return <em key={key}>{token.text}</em>
      default:
        return token.text
    }
  })
}

function renderBlock(block: Block, key: number): ReactNode {
  switch (block.type) {
    case 'code':
      return <CodeBlock key={key} code={block.content} language={block.language} />
    case 'heading': {
      const Tag = `h${block.level}` as 'h1' | 'h2' | 'h3' | 'h4'
      return <Tag key={key}>{formatInline(block.text)}</Tag>
    }
    case 'hr':
      return <hr key={key} />
    case 'quote':
      return (
        <blockquote key={key}>
          {block.lines.map((line, i) => (
            <span key={i}>
              {formatInline(line)}
              {i < block.lines.length - 1 && <br />}
            </span>
          ))}
        </blockquote>
      )
    case 'list': {
      const items = block.items.map((item, i) => (
        <li key={i} className={item.depth ? `md-depth-${item.depth}` : undefined}>
          {formatInline(item.text)}
        </li>
      ))
      return block.ordered ? (
        <ol key={key} start={block.start}>
          {items}
        </ol>
      ) : (
        <ul key={key}>{items}</ul>
      )
    }
    case 'table':
      return (
        <div key={key} className="message-table-wrapper">
          <table>
            <thead>
              <tr>
                {block.header.map((cell, i) => (
                  <th key={i}>{formatInline(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{formatInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    default:
      return (
        <p key={key}>
          {block.lines.map((line, i) => (
            <span key={i}>
              {formatInline(line)}
              {i < block.lines.length - 1 && <br />}
            </span>
          ))}
        </p>
      )
  }
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  if (!content) return null
  return <div className="message-body">{parseMarkdown(content).map(renderBlock)}</div>
}
