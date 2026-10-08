import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { ACCEPTED_REQUIREMENT_FILES } from '../constants'
import type { ChatMessage, Project } from '../types'
import { MarkdownRenderer } from './MarkdownRenderer'

type ChatPanelProps = {
  project: Project
  messages: ChatMessage[]
  files: File[]
  sending: boolean
  error: string | null
  onSend: (content: string) => void
  onFilesAdded: (files: FileList) => void
  onRemoveFile: (name: string) => void
}

const PROMPT_STARTERS = [
  {
    title: '📄 Analyze Uploaded Spec',
    desc: 'Extract key user stories, objective, and requirements from an attached PRD or doc.',
    prompt: 'Please analyze the attached specification and generate structured product requirements.',
  },
  {
    title: '🏗️ Technical Architecture (TRD)',
    desc: 'Recommend technology stacks (Frontend, Backend, Database) and infrastructure.',
    prompt: 'Would you recommend a comprehensive Technical Requirements Document (TRD) for this project?',
  },
  {
    title: '🎯 Clarify User Flows & Goals',
    desc: 'Identify gaps, edge cases, and define end-to-end user journeys.',
    prompt: 'Help me define the core objectives, problem statement, and step-by-step user flows.',
  },
  {
    title: '⚡ Modernize Tech Stack',
    desc: 'Evaluate framework tradeoffs: FastAPI, React, Node, PostgreSQL, and cloud services.',
    prompt: 'What are the recommended technology choices for high scalability and rapid development?',
  },
]

export function ChatPanel({
  project,
  messages,
  files,
  sending,
  error,
  onSend,
  onFilesAdded,
  onRemoveFile,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const canSend = useMemo(
    () => (draft.trim().length > 0 || files.length > 0) && !sending,
    [draft, files.length, sending],
  )

  function submit(customText?: string) {
    const content = (customText ?? draft).trim()
    if (!content && files.length === 0) return
    onSend(content || 'Please analyze the uploaded document.')
    setDraft('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <section className="chat-panel">
      <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="copilot-hero">
            <div className="copilot-sparkle-badge">✨</div>
            <h2>DevFoundry Copilot</h2>
            <p>
              AI-Powered Requirements Engineering & Technical Architecture Orchestrator.
              Turn ideas and specifications into production-grade PRDs and TRDs.
            </p>

            <div className="copilot-starter-grid">
              {PROMPT_STARTERS.map((starter) => (
                <button
                  key={starter.title}
                  type="button"
                  className="copilot-starter-card"
                  onClick={() => setDraft(starter.prompt)}
                >
                  <span className="copilot-starter-title">{starter.title}</span>
                  <span className="copilot-starter-desc">{starter.desc}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message) => {
            const isUser = message.role === 'user'
            return (
              <article
                key={message.id}
                className={`bubble bubble-${isUser ? 'user' : 'assistant'}`}
              >
                <div className="bubble-header">
                  <div
                    className={`bubble-avatar-icon ${
                      isUser ? 'bubble-avatar-user' : 'bubble-avatar-assistant'
                    }`}
                  >
                    {isUser ? '👤' : '✨'}
                  </div>
                  <span className="bubble-role-name">
                    {isUser ? 'You' : 'DevFoundry Copilot'}
                  </span>
                </div>
                <div className="bubble-content">
                  {isUser ? (
                    <div className="message-body">
                      <p style={{ whiteSpace: 'pre-wrap' }}>{message.content}</p>
                    </div>
                  ) : (
                    <MarkdownRenderer content={message.content} />
                  )}
                </div>
              </article>
            )
          })
        )}
      </div>

      {error && (
        <div className="chat-error">
          <span>⚠️</span>
          <span>{error}</span>
        </div>
      )}

      <div className="composer-container">
        <div className="composer">
          {files.length > 0 && (
            <ul className="file-chips">
              {files.map((file) => (
                <li key={file.name} className="file-chip">
                  <span className="file-chip-icon">📄</span>
                  <span>{file.name}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => onRemoveFile(file.name)}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="composer-row">
            <input
              ref={fileInputRef}
              className="sr-only"
              type="file"
              multiple
              accept={ACCEPTED_REQUIREMENT_FILES}
              onChange={(event) => {
                if (event.target.files?.length) {
                  onFilesAdded(event.target.files)
                }
                event.target.value = ''
              }}
            />

            <textarea
              rows={1}
              value={draft}
              placeholder={`Ask DevFoundry Copilot about ${project.name}…`}
              disabled={sending}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
            />

            <div className="composer-actions">
              <button
                type="button"
                className="upload-button"
                title="Attach specification file (.pdf, .docx, .txt)"
                onClick={() => fileInputRef.current?.click()}
              >
                <span>📎</span> Attach Spec
              </button>

              <button
                type="button"
                className="send-button"
                disabled={!canSend}
                onClick={() => submit()}
              >
                {sending ? 'Processing…' : '➤ Send'}
              </button>
            </div>
          </div>
        </div>

        <p className="composer-disclaimer">
          DevFoundry Copilot synthesizes multi-agent recommendations. Verify critical architecture specifications.
        </p>
      </div>
    </section>
  )
}
