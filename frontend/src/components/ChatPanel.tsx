import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { ACCEPTED_REQUIREMENT_FILES } from '../constants'
import type { ChatMessage, Project } from '../types'
import { MarkdownRenderer } from './MarkdownRenderer'
import { AdoLogo, CopilotSparkleIcon } from './AdoIcons'

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
    icon: '📋',
    title: 'Product Requirements (PRD)',
    desc: 'Extract and structure Objective, Goals, User Experience, and detailed User Flows.',
    prompt: 'Please draft a comprehensive Product Requirements Document (PRD) for this project.',
  },
  {
    icon: '🏗️',
    title: 'Technical Architecture (TRD)',
    desc: 'Define Frontend, Backend, Database, and Azure cloud infrastructure stack.',
    prompt: 'Would you recommend a comprehensive Technical Requirements Document (TRD) for this project?',
  },
  {
    icon: '🚀',
    title: 'Azure DevOps Project Setup',
    desc: 'Generate project name & description proposal and provision in Azure DevOps via MCP.',
    prompt: 'Let us proceed to create the Azure DevOps project proposal.',
  },
  {
    icon: '📦',
    title: 'Git Repository Structure',
    desc: 'Synthesize tailored directory tree and initialize remote Git repository in Azure Repos.',
    prompt: 'Please generate the recommended repository directory structure for this project.',
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
    onSend(content || 'Please analyze the uploaded specification.')
    setDraft('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <section className="ado-chat-panel">
      <div className="ado-messages-viewport">
        {messages.length === 0 ? (
          <div className="ado-hero">
            <div className="ado-hero-badge-wrap">
              <div className="ado-hero-logo">
                <AdoLogo size={36} />
              </div>
            </div>

            <h2 className="ado-hero-title">DevFoundry Copilot for Azure DevOps</h2>
            <p className="ado-hero-subtitle">
              Intelligent multi-agent orchestration for Product Requirements (PRD), Technical Architecture (TRD),
              and automated Azure DevOps Project & Git Repository provisioning.
            </p>

            <div className="ado-starter-grid">
              {PROMPT_STARTERS.map((starter) => (
                <button
                  key={starter.title}
                  type="button"
                  className="ado-starter-card"
                  onClick={() => setDraft(starter.prompt)}
                >
                  <div className="ado-starter-card-top">
                    <span className="ado-starter-icon">{starter.icon}</span>
                    <span className="ado-starter-title">{starter.title}</span>
                  </div>
                  <span className="ado-starter-desc">{starter.desc}</span>
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
                className={`ado-discussion-card ado-card-${isUser ? 'user' : 'assistant'}`}
              >
                <div className="ado-card-header">
                  <div className="ado-card-author">
                    <div
                      className={`ado-author-avatar ${
                        isUser ? 'avatar-user' : 'avatar-assistant'
                      }`}
                    >
                      {isUser ? (
                        <span>👤</span>
                      ) : (
                        <AdoLogo size={16} />
                      )}
                    </div>
                    <span className="ado-author-name">
                      {isUser ? 'You' : 'DevFoundry Copilot'}
                    </span>
                    {!isUser && (
                      <span className="ado-copilot-tag">
                        <CopilotSparkleIcon size={12} />
                        Orchestrator
                      </span>
                    )}
                  </div>
                  <span className="ado-card-time">Discussion</span>
                </div>

                <div className="ado-card-body">
                  {isUser ? (
                    <div className="ado-user-text">
                      <p style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{message.content}</p>
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
        <div className="ado-error-banner">
          <span className="ado-error-icon">⚠️</span>
          <span className="ado-error-text">{error}</span>
        </div>
      )}

      {/* Azure DevOps Discussion Composer */}
      <div className="ado-composer-container">
        <div className="ado-composer-box">
          {files.length > 0 && (
            <ul className="ado-file-chips">
              {files.map((file) => (
                <li key={file.name} className="ado-file-chip">
                  <span className="ado-chip-icon">📄</span>
                  <span className="ado-chip-name">{file.name}</span>
                  <button
                    type="button"
                    className="ado-chip-remove"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => onRemoveFile(file.name)}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="ado-composer-input-row">
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
              rows={2}
              value={draft}
              placeholder={`Discuss requirements, clarify architecture, or trigger repo setup for ${project.name}…`}
              disabled={sending}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
            />
          </div>

          <div className="ado-composer-toolbar">
            <div className="ado-toolbar-left">
              <button
                type="button"
                className="ado-btn-secondary ado-attach-btn"
                title="Attach specification file (.pdf, .docx, .txt)"
                onClick={() => fileInputRef.current?.click()}
              >
                <span>📎</span> Attach Spec
              </button>
            </div>

            <div className="ado-toolbar-right">
              <button
                type="button"
                className="ado-btn-primary ado-send-btn"
                disabled={!canSend}
                onClick={() => submit()}
              >
                {sending ? (
                  <>
                    <span className="ado-spinner" />
                    <span>Processing…</span>
                  </>
                ) : (
                  <>
                    <span>➤ Send</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>

        <p className="ado-composer-footnote">
          Powered by Azure DevOps MCP Tools & Microsoft Agent Framework. Review generated PRD/TRD specifications carefully.
        </p>
      </div>
    </section>
  )
}
