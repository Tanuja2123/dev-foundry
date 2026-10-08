import { useState } from 'react'

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

function formatInline(text: string) {
  // Process inline code first: `foo`
  const codeParts = text.split(/(`[^`]+`)/g)
  return codeParts.map((part, i) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={i}>{part.slice(1, -1)}</code>
    }

    // Process bold text: **bold**
    const boldParts = part.split(/(\*\*[^*]+\*\*)/g)
    return boldParts.map((subPart, j) => {
      if (subPart.startsWith('**') && subPart.endsWith('**') && subPart.length > 4) {
        return <strong key={`${i}-${j}`}>{subPart.slice(2, -2)}</strong>
      }
      return subPart
    })
  })
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  if (!content) return null

  // Split code blocks from text
  const tokens: Array<
    | { type: 'code'; language: string; content: string }
    | { type: 'text'; content: string }
  > = []

  const codeBlockRegex = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = codeBlockRegex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({
        type: 'text',
        content: content.slice(lastIndex, match.index),
      })
    }
    tokens.push({
      type: 'code',
      language: match[1] || '',
      content: match[2].trimEnd(),
    })
    lastIndex = match.index + match[0].length
  }

  if (lastIndex < content.length) {
    tokens.push({
      type: 'text',
      content: content.slice(lastIndex),
    })
  }

  return (
    <div className="message-body">
      {tokens.map((token, tokenIdx) => {
        if (token.type === 'code') {
          return (
            <CodeBlock
              key={tokenIdx}
              code={token.content}
              language={token.language}
            />
          )
        }

        // Parse paragraphs and line blocks
        const paragraphs = token.content.split(/\n\n+/)
        return paragraphs.map((para, paraIdx) => {
          const trimmed = para.trim()
          if (!trimmed) return null

          // Headings
          if (trimmed.startsWith('#### ')) {
            return <h4 key={`${tokenIdx}-${paraIdx}`}>{formatInline(trimmed.slice(5))}</h4>
          }
          if (trimmed.startsWith('### ')) {
            return <h3 key={`${tokenIdx}-${paraIdx}`}>{formatInline(trimmed.slice(4))}</h3>
          }
          if (trimmed.startsWith('## ')) {
            return <h2 key={`${tokenIdx}-${paraIdx}`}>{formatInline(trimmed.slice(3))}</h2>
          }
          if (trimmed.startsWith('# ')) {
            return <h1 key={`${tokenIdx}-${paraIdx}`}>{formatInline(trimmed.slice(2))}</h1>
          }

          // Blockquote
          if (trimmed.startsWith('> ')) {
            return (
              <blockquote key={`${tokenIdx}-${paraIdx}`}>
                {formatInline(trimmed.replace(/^>\s*/gm, ''))}
              </blockquote>
            )
          }

          // Bullet lists
          const lines = trimmed.split('\n')
          const isBulletList = lines.every((l) => /^\s*([*•-]\s|\d+\.\s)/.test(l))
          if (isBulletList && lines.length > 0) {
            return (
              <ul key={`${tokenIdx}-${paraIdx}`}>
                {lines.map((line, liIdx) => (
                  <li key={liIdx}>
                    {formatInline(line.replace(/^\s*([*•-]\s|\d+\.\s)/, ''))}
                  </li>
                ))}
              </ul>
            )
          }

          // Standard paragraph with line breaks
          return (
            <p key={`${tokenIdx}-${paraIdx}`}>
              {lines.map((line, lineIdx) => (
                <span key={lineIdx}>
                  {formatInline(line)}
                  {lineIdx < lines.length - 1 && <br />}
                </span>
              ))}
            </p>
          )
        })
      })}
    </div>
  )
}
