import { useMsal } from '@azure/msal-react'
import { useEffect, useState } from 'react'
import {
  continueChat,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
} from '../api/chat'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar } from '../components/Sidebar'
import type { ChatMessage, Project } from '../types'

function createId() {
  return crypto.randomUUID()
}

function getInitials(name: string) {
  if (!name) return 'U'
  const parts = name.trim().split(/\s+/)
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase()
  }
  return name.slice(0, 2).toUpperCase()
}

export function HomePage() {
  const { instance, accounts } = useMsal()
  const account = instance.getActiveAccount() ?? accounts[0]
  const [conversations, setConversations] = useState<Project[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [listStatus, setListStatus] = useState<string | null>('Loading projects…')
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
        setListStatus(mapped.length === 0 ? 'No projects yet.' : null)
      } catch (reason) {
        if (cancelled) return
        setListStatus(
          reason instanceof Error
            ? reason.message
            : 'Could not load projects.',
        )
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [])

  // Automatically fetch existing conversation history when switching projects
  useEffect(() => {
    if (!selectedId) return
    if (messagesByConversation[selectedId]) return // Already loaded

    let cancelled = false

    async function fetchHistory() {
      try {
        const data = await getConversation(selectedId!)
        if (cancelled) return
        if (Array.isArray(data.content) && data.content.length > 0) {
          const loaded: ChatMessage[] = data.content.map((m) => ({
            id: createId(),
            role: m.role,
            content: m.text,
          }))
          setMessagesByConversation((prev) => ({
            ...prev,
            [selectedId!]: loaded,
          }))
        }
      } catch {
        // Conversation might be newly created without messages yet
      }
    }

    void fetchHistory()
    return () => {
      cancelled = true
    }
  }, [selectedId, messagesByConversation])

  const conversation =
    conversations.find((item) => item.id === selectedId) ?? null
  const messages = conversation
    ? (messagesByConversation[conversation.id] ?? [])
    : []
  const files = conversation ? (filesByConversation[conversation.id] ?? []) : []
  const displayName = account?.name ?? account?.username ?? 'Signed in'
  const initials = getInitials(displayName)

  function selectConversation(conversationId: string) {
    setSelectedId(conversationId)
    setChatError(null)
  }

  async function handleNewConversation() {
    try {
      const created = await createConversation()
      const newProject: Project = {
        id: created.conversation_id,
        name: created.title,
      }
      setConversations((current) => [newProject, ...current])
      setSelectedId(newProject.id)
      setChatError(null)
    } catch (reason) {
      setChatError(
        reason instanceof Error ? reason.message : 'Could not create new project.',
      )
    }
  }

  async function handleDeleteProject(projectId: string) {
    try {
      await deleteConversation(projectId)
      const remaining = conversations.filter((c) => c.id !== projectId)
      setConversations(remaining)
      if (selectedId === projectId) {
        setSelectedId(remaining[0]?.id ?? null)
      }
      setMessagesByConversation((prev) => {
        const next = { ...prev }
        delete next[projectId]
        return next
      })
    } catch (reason) {
      setChatError(
        reason instanceof Error ? reason.message : 'Could not delete conversation.',
      )
    }
  }

  async function sendMessage(content: string) {
    if (!conversation || sending) return
    const conversationId = conversation.id
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content,
    }
    const attachedFile = files[0] ?? null

    setChatError(null)
    setSending(true)
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [...(current[conversationId] ?? []), userMessage],
    }))

    try {
      const result = await continueChat(conversationId, content, attachedFile)
      if (attachedFile) {
        setFilesByConversation((current) => ({
          ...current,
          [conversationId]: [],
        }))
      }
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
        onNewProject={() => void handleNewConversation()}
        onDeleteProject={(id) => void handleDeleteProject(id)}
        status={listStatus}
      />
      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            <div className="topbar-breadcrumb">
              <span>Workspaces</span>
              <span>›</span>
              <span>Requirements Engineering</span>
              {conversation && (
                <>
                  <span>›</span>
                  <span>{conversation.name}</span>
                </>
              )}
            </div>
            <div className="topbar-title-row">
              <h1>{conversation?.name ?? 'DevFoundry Workspace'}</h1>
              <div className="topbar-status-chip">
                <span className="topbar-status-dot" />
                <span>Multi-Agent Copilot</span>
              </div>
            </div>
          </div>

          <div className="topbar-user">
            <div className="ms-avatar-monogram" title={displayName}>
              {initials}
            </div>
            <span className="topbar-username">{displayName}</span>
            <button
              type="button"
              className="ms-btn-ghost"
              onClick={() => instance.logoutRedirect()}
            >
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
            <div className="copilot-hero">
              <div className="copilot-sparkle-badge">✨</div>
              <h2>Select or create a project</h2>
              <p>Choose an existing project from the sidebar or click "+ New Project" to begin.</p>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
