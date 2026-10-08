import { useMsal } from '@azure/msal-react'
import { useEffect, useState } from 'react'
import {
  continueChat,
  deleteConversation,
  getConversation,
  listConversations,
} from '../api/chat'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar } from '../components/Sidebar'
import { AdoLogo, WaffleIcon } from '../components/AdoIcons'
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
  const [draftMessages, setDraftMessages] = useState<ChatMessage[]>([])
  const [draftFiles, setDraftFiles] = useState<File[]>([])
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
    if (messagesByConversation[selectedId]) return // Already cached

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
    : draftMessages
  const files = conversation
    ? (filesByConversation[conversation.id] ?? [])
    : draftFiles

  const displayName = account?.name ?? account?.username ?? 'Signed in'
  const initials = getInitials(displayName)

  function selectConversation(conversationId: string) {
    setSelectedId(conversationId)
    setDraftMessages([])
    setDraftFiles([])
    setChatError(null)
  }

  function handleNewConversation() {
    setSelectedId(null)
    setDraftMessages([])
    setDraftFiles([])
    setChatError(null)
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
    if (sending) return
    const isNew = !conversation
    const conversationId = conversation?.id ?? null
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content,
    }
    const attachedFile = files[0] ?? null

    setChatError(null)
    setSending(true)

    if (isNew) {
      setDraftMessages([userMessage])
    } else {
      setMessagesByConversation((current) => ({
        ...current,
        [conversationId!]: [...(current[conversationId!] ?? []), userMessage],
      }))
    }

    try {
      // Pass null for conversationId on the very first message
      const result = await continueChat(conversationId, content, attachedFile)
      const assistantMessage: ChatMessage = {
        id: createId(),
        role: 'assistant',
        content: result.response,
      }

      if (isNew) {
        const newId = result.conversation_id
        let projectTitle = 'New Project'
        try {
          const fresh = await getConversation(newId)
          if (fresh.title) projectTitle = fresh.title
        } catch {
          // fallback
        }

        const newProject: Project = {
          id: newId,
          name: projectTitle,
        }
        setConversations((prev) => [newProject, ...prev])
        setSelectedId(newId)
        setMessagesByConversation((prev) => ({
          ...prev,
          [newId]: [userMessage, assistantMessage],
        }))
        setDraftMessages([])
        setDraftFiles([])
      } else {
        if (attachedFile) {
          setFilesByConversation((current) => ({
            ...current,
            [conversationId!]: [],
          }))
        }
        setMessagesByConversation((current) => ({
          ...current,
          [conversationId!]: [...(current[conversationId!] ?? []), assistantMessage],
        }))
      }
    } catch (reason) {
      setChatError(
        reason instanceof Error ? reason.message : 'Chat request failed.',
      )
    } finally {
      setSending(false)
    }
  }

  function addFiles(fileList: FileList) {
    const incoming = Array.from(fileList)
    if (!conversation) {
      setDraftFiles((prev) => {
        const names = new Set(prev.map((f) => f.name))
        return [...prev, ...incoming.filter((f) => !names.has(f.name))]
      })
      return
    }
    setFilesByConversation((current) => {
      const existing = current[conversation.id] ?? []
      const names = new Set(existing.map((file) => file.name))
      const unique = incoming.filter((file) => !names.has(file.name))
      return { ...current, [conversation.id]: [...existing, ...unique] }
    })
  }

  function removeFile(name: string) {
    if (!conversation) {
      setDraftFiles((prev) => prev.filter((f) => f.name !== name))
      return
    }
    setFilesByConversation((current) => ({
      ...current,
      [conversation.id]: (current[conversation.id] ?? []).filter(
        (file) => file.name !== name,
      ),
    }))
  }

  const activeProject: Project = conversation ?? {
    id: '',
    name: 'New Project',
  }

  return (
    <div className="ado-app">
      {/* Azure DevOps Top Suite Bar */}
      <header className="ado-suitebar">
        <div className="ado-suitebar-left">
          <button
            type="button"
            className="ado-waffle-btn"
            title="Microsoft 365 / Azure App Launcher"
            aria-label="App Launcher"
          >
            <WaffleIcon size={18} />
          </button>
          <div className="ado-suite-brand">
            <AdoLogo size={20} />
            <span className="ado-suite-name">Azure DevOps</span>
            <span className="ado-suite-sep">/</span>
            <span className="ado-suite-org">DevFoundry</span>
          </div>
        </div>

        <div className="ado-suitebar-center">
          <div className="ado-suitebar-search">
            <span className="ado-search-icon">🔍</span>
            <input
              type="text"
              placeholder="Search Azure DevOps (/)"
              className="ado-suitebar-search-input"
            />
            <span className="ado-shortcut-badge">/</span>
          </div>
        </div>

        <div className="ado-suitebar-right">
          <div className="ado-mcp-badge" title="Azure DevOps MCP Tools Connected">
            <span className="ado-mcp-dot" />
            <span>ADO MCP Online</span>
          </div>

          <div className="ado-user-profile" title={`Signed in as ${displayName}`}>
            <div className="ado-user-avatar">{initials}</div>
            <span className="ado-user-name">{displayName}</span>
            <button
              type="button"
              className="ado-signout-btn"
              onClick={() => instance.logoutRedirect()}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      {/* Main Azure DevOps Body: Left Navigation + Content Hub */}
      <div className="ado-body-container">
        <Sidebar
          projects={conversations}
          selectedProjectId={conversation?.id ?? null}
          onSelectProject={selectConversation}
          onNewProject={handleNewConversation}
          onDeleteProject={(id) => void handleDeleteProject(id)}
          status={listStatus}
        />

        <main className="ado-content-hub">
          {/* Azure DevOps Command Bar & Context Subheader */}
          <div className="ado-commandbar">
            <div className="ado-commandbar-left">
              <nav className="ado-breadcrumb" aria-label="Breadcrumb">
                <span>DevFoundry</span>
                <span className="ado-bc-sep">›</span>
                <span>Requirements & Architecture</span>
                {conversation && (
                  <>
                    <span className="ado-bc-sep">›</span>
                    <span className="ado-bc-current">{conversation.name}</span>
                  </>
                )}
              </nav>

              <div className="ado-title-row">
                <h1 className="ado-project-heading">{activeProject.name}</h1>
                <span className="ado-stage-tag ado-stage-prd">PRD & TRD</span>
                <span className="ado-stage-tag ado-stage-repo">Azure Repos</span>
              </div>
            </div>

            <div className="ado-commandbar-right">
              <span className="ado-agent-status-pill">
                <span className="ado-status-pulse" />
                Multi-Agent Orchestrator
              </span>
            </div>
          </div>

          {/* Azure DevOps Chat / Work Item Copilot View */}
          <ChatPanel
            project={activeProject}
            messages={messages}
            files={files}
            sending={sending}
            error={chatError}
            onSend={(content) => void sendMessage(content)}
            onFilesAdded={addFiles}
            onRemoveFile={removeFile}
          />
        </main>
      </div>
    </div>
  )
}
