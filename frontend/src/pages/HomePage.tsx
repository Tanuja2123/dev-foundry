import { useMsal } from '@azure/msal-react'
import { useEffect, useState } from 'react'
import { continueChat, listConversations } from '../api/chat'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar } from '../components/Sidebar'
import type { ChatMessage, Project } from '../types'

function createId() {
  return crypto.randomUUID()
}

export function HomePage() {
  const { instance, accounts } = useMsal()
  const account = instance.getActiveAccount() ?? accounts[0]
  const [conversations, setConversations] = useState<Project[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [listStatus, setListStatus] = useState<string | null>('Loading conversations…')
  const [messagesByConversation, setMessagesByConversation] = useState<
    Record<string, ChatMessage[]>
  >({})
  const [filesByConversation, setFilesByConversation] = useState<
    Record<string, File[]>
  >({})
  const [sending, setSending] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const items = await listConversations()
        if (cancelled) return
        const mapped = items.map((item) => ({
          id: item.conversation_id,
          name: item.title,
        }))
        setConversations(mapped)
        setSelectedId((current) => current ?? mapped[0]?.id ?? null)
        setListStatus(mapped.length === 0 ? 'No conversations yet.' : null)
      } catch (reason) {
        if (cancelled) return
        setListStatus(
          reason instanceof Error
            ? reason.message
            : 'Could not load conversations.',
        )
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [])

  const conversation =
    conversations.find((item) => item.id === selectedId) ?? null
  const messages = conversation
    ? (messagesByConversation[conversation.id] ?? [])
    : []
  const files = conversation ? (filesByConversation[conversation.id] ?? []) : []
  const displayName = account?.name ?? account?.username ?? 'Signed in'

  function selectConversation(conversationId: string) {
    setSelectedId(conversationId)
    setChatError(null)
  }

  async function sendMessage(content: string) {
    if (!conversation || sending) return
    const conversationId = conversation.id
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content,
    }

    setChatError(null)
    setSending(true)
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [...(current[conversationId] ?? []), userMessage],
    }))

    try {
      const result = await continueChat(conversationId, content)
      const assistantMessage: ChatMessage = {
        id: createId(),
        role: 'assistant',
        content: result.response,
      }
      setMessagesByConversation((current) => ({
        ...current,
        [conversationId]: [...(current[conversationId] ?? []), assistantMessage],
      }))
    } catch (reason) {
      setChatError(
        reason instanceof Error ? reason.message : 'Chat request failed.',
      )
    } finally {
      setSending(false)
    }
  }

  function addFiles(fileList: FileList) {
    if (!conversation) return
    const incoming = Array.from(fileList)
    setFilesByConversation((current) => {
      const existing = current[conversation.id] ?? []
      const names = new Set(existing.map((file) => file.name))
      const unique = incoming.filter((file) => !names.has(file.name))
      return { ...current, [conversation.id]: [...existing, ...unique] }
    })
  }

  function removeFile(name: string) {
    if (!conversation) return
    setFilesByConversation((current) => ({
      ...current,
      [conversation.id]: (current[conversation.id] ?? []).filter(
        (file) => file.name !== name,
      ),
    }))
  }

  return (
    <div className="app-shell">
      <Sidebar
        projects={conversations}
        selectedProjectId={conversation?.id ?? null}
        onSelectProject={selectConversation}
        status={listStatus}
      />
      <div className="main">
        <header className="topbar">
          <div>
            <h1>{conversation?.name ?? 'Conversations'}</h1>
            <p>Requirement chat</p>
          </div>
          <div className="topbar-user">
            <span>{displayName}</span>
            <button type="button" onClick={() => instance.logoutRedirect()}>
              Sign out
            </button>
          </div>
        </header>
        {conversation ? (
          <ChatPanel
            project={conversation}
            messages={messages}
            files={files}
            sending={sending}
            error={chatError}
            onSend={(content) => void sendMessage(content)}
            onFilesAdded={addFiles}
            onRemoveFile={removeFile}
          />
        ) : (
          <section className="chat-panel">
            <div className="chat-empty">
              <h2>Conversations</h2>
              <p>{listStatus ?? 'Select a conversation to continue.'}</p>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
