import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Sparkles, User, AlertTriangle, Lightbulb, FileText, X, Paperclip, Send, BrainCircuit, Target, BookOpen } from 'lucide-react'
import { ACCEPTED_REQUIREMENT_FILES } from '../constants'
import type { ChatMessage, Project, Suggestion } from '../types'
import { MarkdownRenderer } from './MarkdownRenderer'

// The input grows with its content up to this height, then scrolls.
const COMPOSER_MAX_HEIGHT = 200
// Show the "Jump to latest" button once the user is this far (px) above the bottom of the chat.
const JUMP_TO_LATEST_THRESHOLD = 240

function pendingStatus(seconds: number) {
  if (seconds < 4) return 'Understanding your request'
  if (seconds < 12) return 'Coordinating with specialist agents'
  if (seconds < 30) return 'Preparing the response'
  return 'Still working, Azure DevOps steps can take up to a minute'
}

function PendingResponse() {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const started = Date.now()
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started) / 1000))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <article className="bubble bubble-assistant bubble-pending" role="status" aria-live="polite">
      <div className="bubble-header">
        <div className="bubble-avatar-icon bubble-avatar-assistant" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Sparkles size={16} /></div>
        <span className="bubble-role-name">DevFoundry Copilot</span>
      </div>
      <div className="pending-body">
        <span className="typing-dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
        <span className="pending-status">{pendingStatus(elapsed)}…</span>
        <span className="pending-elapsed">{elapsed}s</span>
      </div>
    </article>
  )
}

type ChatPanelProps = {
  project: Project
  messages: ChatMessage[]
  files: File[]
  suggestions: Suggestion[]
  sending: boolean
  error: string | null
  onSend: (content: string) => void
  onFilesAdded: (files: FileList) => void
  onRemoveFile: (name: string) => void
}

const PROMPT_STARTERS = [
  {
    icon: <FileText size={18} className="copilot-starter-icon" style={{ marginRight: '8px' }} />,
    title: 'Analyze Uploaded Spec',
    desc: 'Extract key user stories, objective, and requirements from an attached document.',
    prompt: 'Please analyze the attached specification and generate structured product requirements.',
  },
  {
    icon: <BrainCircuit size={18} className="copilot-starter-icon" style={{ marginRight: '8px' }} />,
    title: 'Brainstorm New Project',
    desc: 'Discuss your app idea, core features, and target audience.',
    prompt: 'I have an idea for a new app. Can you help me brainstorm the core features and target audience?',
  },
  {
    icon: <Target size={18} className="copilot-starter-icon" style={{ marginRight: '8px' }} />,
    title: 'Define User Journeys',
    desc: 'Map out step-by-step user flows and identify edge cases.',
    prompt: 'Help me define the core objectives, problem statement, and step-by-step user flows.',
  },
  {
    icon: <BookOpen size={18} className="copilot-starter-icon" style={{ marginRight: '8px' }} />,
    title: 'Draft Requirements (PRD)',
    desc: 'Create a structured Product Requirements Document step-by-step.',
    prompt: 'Guide me through defining the requirements for a new Product Requirements Document (PRD).',
  },
]

export function ChatPanel({
  project,
  messages,
  files,
  suggestions,
  sending,
  error,
  onSend,
  onFilesAdded,
  onRemoveFile,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)

  function updateJumpToLatest() {
    const container = messagesRef.current
    if (!container) return
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight
    setShowJumpToLatest(distanceFromBottom > JUMP_TO_LATEST_THRESHOLD)
  }

  function jumpToLatest() {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }

  const canSend = useMemo(
    () => (draft.trim().length > 0 || files.length > 0) && !sending,
    [draft, files.length, sending],
  )

  // Grow the input with its content (up to COMPOSER_MAX_HEIGHT), then let it scroll
  useLayoutEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    const height = Math.min(textarea.scrollHeight, COMPOSER_MAX_HEIGHT)
    textarea.style.height = `${height}px`
    textarea.style.overflowY = textarea.scrollHeight > COMPOSER_MAX_HEIGHT ? 'auto' : 'hidden'
  }, [draft])

  // Keep the newest message (or the pending indicator) in view
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, sending])

  // Return focus to the input once the response arrives
  useEffect(() => {
    if (!sending) textareaRef.current?.focus()
  }, [sending])

  function submit(customText?: string) {
    if (sending) return
    const content = (customText ?? draft).trim()
    if (!content && files.length === 0) return
    onSend(content || 'Please analyze the uploaded document.')
    // A suggestion click sends its own text and keeps whatever the user was typing
    if (customText === undefined) setDraft('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <section className="chat-panel">
      <div className="chat-scroll-area">
        <div className="chat-messages" ref={messagesRef} onScroll={updateJumpToLatest}>
        {messages.length === 0 ? (
          <div className="copilot-hero">
            <div className="copilot-sparkle-badge" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Sparkles size={32} /></div>
            <h2>DevFoundry Copilot</h2>
            <p>
              Your AI Requirements Engineer. Let's discuss your ideas and turn them into structured, production-ready specifications.
            </p>

            <div className="copilot-starter-grid">
              {PROMPT_STARTERS.map((starter) => (
                <button
                  key={starter.title}
                  type="button"
                  className="copilot-starter-card"
                  onClick={() => setDraft(starter.prompt)}
                >
                  <span className="copilot-starter-title" style={{ display: 'flex', alignItems: 'center' }}>
                    {starter.icon}
                    {starter.title}
                  </span>
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
                    style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                  >
                    {isUser ? <User size={16} /> : <Sparkles size={16} />}
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
        {sending && <PendingResponse />}
        <div ref={messagesEndRef} />
        </div>
        {showJumpToLatest && (
          <button
            type="button"
            className="jump-to-latest"
            onClick={jumpToLatest}
            aria-label="Jump to the latest message"
          >
            ↓ Jump to latest
          </button>
        )}
      </div>

      {error && (
        <div className="chat-error">
          <span style={{ display: 'flex', alignItems: 'center' }}><AlertTriangle size={16} /></span>
          <span>{error}</span>
        </div>
      )}

      <div className="composer-container">
        {suggestions.length > 0 && !sending && (
          <div className="suggestion-bar" aria-label="Suggested next steps">
            <span className="suggestion-bar-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}><Lightbulb size={16} /> Suggested next steps</span>
            <div className="suggestion-chips">
              {suggestions.map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="suggestion-chip"
                  title={suggestion.prompt}
                  onClick={() => submit(suggestion.prompt)}
                >
                  {suggestion.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="composer">
          {files.length > 0 && (
            <ul className="file-chips">
              {files.map((file) => (
                <li key={file.name} className="file-chip">
                  <span className="file-chip-icon" style={{ display: 'flex', alignItems: 'center' }}><FileText size={14} /></span>
                  <span>{file.name}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => onRemoveFile(file.name)}
                    style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                  >
                    <X size={14} />
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
              ref={textareaRef}
              rows={1}
              value={draft}
              placeholder={`Ask DevFoundry Copilot about ${project.name}…`}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
            />

            <div className="composer-actions">
              <button
                type="button"
                className="upload-button"
                title="Attach specification file (.pdf, .docx, .txt)"
                onClick={() => fileInputRef.current?.click()}
                style={{ display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                <Paperclip size={16} /> Attach Spec
              </button>

              <button
                type="button"
                className="send-button"
                disabled={!canSend}
                onClick={() => submit()}
                style={{ display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                {sending ? (
                  <>
                    <span className="send-spinner" aria-hidden="true" /> Working…
                  </>
                ) : (
                  <><Send size={16} /> Send</>
                )}
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
