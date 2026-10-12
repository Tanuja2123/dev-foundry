import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  ArrowUp,
  Paperclip,
  User,
  X,
  ArrowDown,
  AlertCircle,
  Loader2,
  FileText,
  Lightbulb,
} from 'lucide-react'
import { ACCEPTED_REQUIREMENT_FILES } from '../constants'
import type { ChatMessage, Project, Suggestion } from '../types'
import { MarkdownRenderer } from './MarkdownRenderer'

// The input grows with its content up to this height, then scrolls.
const COMPOSER_MAX_HEIGHT = 280
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
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, 44), COMPOSER_MAX_HEIGHT)
    textarea.style.height = `${nextHeight}px`
    textarea.style.overflowY = textarea.scrollHeight > COMPOSER_MAX_HEIGHT ? 'auto' : 'hidden'
  }, [draft])

  // Keep the newest message (or the pending indicator) in view with smooth scroll
  useEffect(() => {
    const scrollToBottom = () => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
    scrollToBottom()
    const timer = setTimeout(scrollToBottom, 120)
    return () => clearTimeout(timer)
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
    setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }, 60)
  }

  function handleApplySuggestion(prompt: string) {
    setDraft(prompt)
    setTimeout(() => {
      textareaRef.current?.focus()
    }, 10)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <section className="chat-panel">
      <div className="chat-scroll-area" ref={messagesRef} onScroll={updateJumpToLatest}>
        <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="copilot-hero">
            <h2>DevFoundry Copilot</h2>
            <p>
              AI-Powered Requirements Engineering & Technical Architecture Orchestrator.
              Turn ideas and specifications into production-grade PRDs and TRDs.
            </p>


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
                  {isUser && (
                    <div className="bubble-avatar-icon bubble-avatar-user">
                      <User size={13} />
                    </div>
                  )}
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
            <ArrowDown size={14} />
            <span>Latest</span>
          </button>
        )}
      </div>

      {error && (
        <div className="chat-error">
          <AlertCircle size={16} />
          <span>{error}</span>
        </div>
      )}

      <div className="composer-container">
        {suggestions.length > 0 && !sending && (
          <div className="suggestion-bar" aria-label="Suggested next steps">
            <span className="suggestion-bar-label">
              <Lightbulb size={13} className="suggestion-bulb-icon" /> Suggested next steps
            </span>
            <div className="suggestion-chips">
              {suggestions.map((suggestion) => (
                <button
                  key={suggestion.label}
                  type="button"
                  className="suggestion-chip"
                  title={suggestion.prompt}
                  onClick={() => handleApplySuggestion(suggestion.prompt)}
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
                  <FileText size={14} className="file-chip-icon" />
                  <span>{file.name}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => onRemoveFile(file.name)}
                  >
                    <X size={12} />
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

            <button
              type="button"
              className="composer-icon-button attach-icon-btn"
              title="Attach specification file (.pdf, .docx, .txt)"
              aria-label="Attach specification file"
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip size={18} />
            </button>

            <textarea
              ref={textareaRef}
              rows={1}
              value={draft}
              placeholder={project.name ? `Ask DevFoundry Copilot about ${project.name}…` : 'Message DevFoundry Copilot…'}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
            />

            <button
              type="button"
              className="composer-icon-button send-icon-btn"
              disabled={!canSend}
              onClick={() => submit()}
              title="Send message"
              aria-label="Send message"
            >
              {sending ? (
                <Loader2 size={16} className="send-spinner-icon" />
              ) : (
                <ArrowUp size={18} />
              )}
            </button>
          </div>
        </div>
      </div>
    </section>
  )
}
