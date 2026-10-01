import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { ACCEPTED_REQUIREMENT_FILES } from '../constants'
import type { ChatMessage, Project } from '../types'

type ChatPanelProps = {
  project: Project
  messages: ChatMessage[]
  files: File[]
  onSend: (content: string) => void
  onFilesAdded: (files: FileList) => void
  onRemoveFile: (name: string) => void
}

export function ChatPanel({
  project,
  messages,
  files,
  onSend,
  onFilesAdded,
  onRemoveFile,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const canSend = useMemo(() => draft.trim().length > 0, [draft])

  function submit() {
    const content = draft.trim()
    if (!content) return
    onSend(content)
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
          <div className="chat-empty">
            <h2>{project.name}</h2>
            <p>
              Ask a question or upload a requirement document to get started.
            </p>
          </div>
        ) : (
          messages.map((message) => (
            <article
              key={message.id}
              className={`bubble bubble-${message.role}`}
            >
              <span className="bubble-role">
                {message.role === 'user' ? 'You' : 'Assistant'}
              </span>
              <p>{message.content}</p>
            </article>
          ))
        )}
      </div>

      <div className="composer">
        {files.length > 0 && (
          <ul className="file-chips">
            {files.map((file) => (
              <li key={file.name} className="file-chip">
                <span>{file.name}</span>
                <button
                  type="button"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => onRemoveFile(file.name)}
                >
                  ×
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
            className="icon-button"
            onClick={() => fileInputRef.current?.click()}
          >
            Upload
          </button>
          <textarea
            rows={1}
            value={draft}
            placeholder={`Message ${project.name}…`}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <button type="button" className="send-button" disabled={!canSend} onClick={submit}>
            Send
          </button>
        </div>
      </div>
    </section>
  )
}
