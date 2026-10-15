import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  ArrowUp,
  Paperclip,
  User,
  X,
  ArrowDown,
  AlertCircle,
  Square,
  FileText,
  Lightbulb,
  Copy,
  Check,
  Pencil,
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
  onStop?: () => void
  onEditAndResend?: (messageId: string, newContent: string) => void
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
  onStop,
  onEditAndResend,
  onFilesAdded,
  onRemoveFile,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)

  useEffect(() => {
    setEditingMessageId(null)
  }, [project.id])

  function handleEditInComposer(message: ChatMessage) {
    if (sending) {
      onStop?.()
    }
    setEditingMessageId(message.id)
    setDraft(message.content)
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.focus()
        textareaRef.current.setSelectionRange(
          textareaRef.current.value.length,
          textareaRef.current.value.length,
        )
        textareaRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' })
      }
    }, 50)
  }

  async function handleCopy(text: string, id: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      const el = document.createElement('textarea')
      el.value = text
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    }
  }

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
    const currentEditId = editingMessageId
    setEditingMessageId(null)
    if (currentEditId && onEditAndResend && customText === undefined) {
      onEditAndResend(currentEditId, content || 'Please analyze the uploaded document.')
    } else {
      onSend(content || 'Please analyze the uploaded document.')
    }
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
          (() => {
            const lastUserMessageIndex = messages.map((m) => m.role).lastIndexOf('user')
            return messages.map((message, index) => {
              const isUser = message.role === 'user'
              const isLastUserQuery = isUser && index === lastUserMessageIndex
              const isStopped = Boolean(message.stopped)
              const isEditingThis = editingMessageId === message.id
              return (
                <article
                  key={message.id}
                  className={`bubble bubble-${isUser ? 'user' : 'assistant'} ${isStopped ? 'bubble-stopped' : ''} ${isEditingThis ? 'bubble-editing' : ''}`}
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
                    {isUser && isStopped && (
                      <span className="stopped-badge" title="Query was stopped before response">
                        <Square size={9} fill="currentColor" /> Stopped
                      </span>
                    )}
                  </div>
                  <div className="bubble-content">
                    {isUser ? (
                      <>
                        <div className="message-body">
                          <p style={{ whiteSpace: 'pre-wrap' }}>{message.content}</p>
                        </div>
                        <div className="bubble-actions bubble-actions-user">
                          {isLastUserQuery && (
                            <button
                              type="button"
                              className="edit-query-button"
                              onClick={() => handleEditInComposer(message)}
                              title="Edit query in input box"
                              aria-label="Edit query in input box"
                            >
                              <Pencil size={13} />
                            </button>
                          )}
                          <button
                            type="button"
                            className="copy-response-button"
                            onClick={() => void handleCopy(message.content, message.id)}
                            title={copiedId === message.id ? 'Copied!' : 'Copy query'}
                            aria-label={copiedId === message.id ? 'Copied!' : 'Copy query'}
                          >
                            {copiedId === message.id ? (
                              <Check size={13} className="copy-icon-success" />
                            ) : (
                              <Copy size={13} />
                            )}
                          </button>
                        </div>
                      </>
                    ) : (
                      <>
                        <MarkdownRenderer content={message.content} />
                        <div className="bubble-actions">
                          <button
                            type="button"
                            className="copy-response-button"
                            onClick={() => void handleCopy(message.content, message.id)}
                            title={copiedId === message.id ? 'Copied!' : 'Copy response'}
                            aria-label={copiedId === message.id ? 'Copied!' : 'Copy response'}
                          >
                            {copiedId === message.id ? (
                              <Check size={13} className="copy-icon-success" />
                            ) : (
                              <Copy size={13} />
                            )}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                </article>
              )
            })
          })()
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
          {editingMessageId && (
            <div className="composer-editing-banner">
              <span className="composer-editing-banner-label">
                <Pencil size={12} />
                Editing query in input box
              </span>
              <button
                type="button"
                className="composer-editing-cancel"
                onClick={() => {
                  setEditingMessageId(null)
                  setDraft('')
                }}
                title="Cancel editing"
                aria-label="Cancel editing"
              >
                <X size={12} />
                <span>Cancel</span>
              </button>
            </div>
          )}
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
              className={`composer-icon-button ${sending ? 'stop-icon-btn' : 'send-icon-btn'}`}
              disabled={!sending && !canSend}
              onClick={() => {
                if (sending) {
                  onStop?.()
                } else {
                  submit()
                }
              }}
              title={sending ? 'Stop generating' : 'Send message'}
              aria-label={sending ? 'Stop generating' : 'Send message'}
            >
              {sending ? (
                <Square size={13} fill="currentColor" strokeWidth={0} />
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
