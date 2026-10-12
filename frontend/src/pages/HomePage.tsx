import { useMsal } from '@azure/msal-react'
import { useCallback, useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { PanelLeftOpen, Moon, Sun } from 'lucide-react'
import {
  continueChat,
  deleteConversation,
  getConversation,
  listConversations,
} from '../api/chat'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar, type SidebarState } from '../components/Sidebar'
import { ProjectsPage } from './ProjectsPage'
import type { ChatMessage, Project, Suggestion } from '../types'

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
  const location = useLocation()
  const navigate = useNavigate()
  const isProjectsView = location.pathname === '/projects'

  const [conversations, setConversations] = useState<Project[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [listStatus, setListStatus] = useState<string | null>('Loading projects…')
  const [messagesByConversation, setMessagesByConversation] = useState<
    Record<string, ChatMessage[]>
  >({})
  const [filesByConversation, setFilesByConversation] = useState<
    Record<string, File[]>
  >({})
  // Next-step suggestions computed by the backend from the stored workflow state
  const [suggestionsByConversation, setSuggestionsByConversation] = useState<
    Record<string, Suggestion[]>
  >({})
  const [draftMessages, setDraftMessages] = useState<ChatMessage[]>([])
  const [draftFiles, setDraftFiles] = useState<File[]>([])
  const [sending, setSending] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  const [sidebarState, setSidebarState] = useState<SidebarState>(() => {
    const saved = localStorage.getItem('sidebarState') as SidebarState | null
    return saved === 'hidden' ? 'hidden' : 'expanded'
  })
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    const saved = localStorage.getItem('sidebarWidth')
    const parsed = saved ? parseInt(saved, 10) : NaN
    return !isNaN(parsed) && parsed >= 180 && parsed <= 520 ? parsed : 280
  })
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('theme')
    return saved === 'dark' ? 'dark' : 'light'
  })

  useEffect(() => {
    localStorage.setItem('sidebarState', sidebarState)
  }, [sidebarState])

  useEffect(() => {
    localStorage.setItem('sidebarWidth', String(sidebarWidth))
  }, [sidebarWidth])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  function toggleTheme() {
    setTheme((curr) => (curr === 'light' ? 'dark' : 'light'))
  }

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const items = await listConversations()
        if (cancelled) return
        const mapped = items.map((item) => ({
          id: item.conversation_id,
          name: item.title,
          stage: item.stage,
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
        setSuggestionsByConversation((prev) => ({
          ...prev,
          [selectedId!]: data.suggestions ?? [],
        }))
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

  function handleNavigateProjects() {
    navigate('/projects')
  }

  function selectConversation(conversationId: string) {
    setSelectedId(conversationId)
    setDraftMessages([])
    setDraftFiles([])
    setChatError(null)
    if (location.pathname === '/projects') {
      navigate('/')
    }
  }

  const handleNewConversation = useCallback(() => {
    setSelectedId(null)
    setDraftMessages([])
    setDraftFiles([])
    setChatError(null)
    if (location.pathname === '/projects') {
      navigate('/')
    }
    setTimeout(() => {
      document.querySelector<HTMLTextAreaElement>('.composer-row textarea')?.focus()
    }, 60)
  }, [location.pathname, navigate])

  // Global keyboard accessibility shortcuts:
  // - Toggle Sidebar: Ctrl+B / Ctrl+\ / Ctrl+Shift+S
  // - Start New Project: Ctrl+Shift+O / Alt+N / Ctrl+Alt+N
  useEffect(() => {
    function onGlobalKeyDown(e: globalThis.KeyboardEvent) {
      const isMod = e.ctrlKey || e.metaKey

      // Toggle sidebar between expanded and hidden states
      if (
        (isMod && (e.key === 'b' || e.key === 'B')) ||
        (isMod && e.key === '\\') ||
        (isMod && e.shiftKey && (e.key === 's' || e.key === 'S'))
      ) {
        e.preventDefault()
        setSidebarState((curr) => (curr === 'hidden' ? 'expanded' : 'hidden'))
        return
      }

      // Start new project
      if (
        (isMod && e.shiftKey && (e.key === 'o' || e.key === 'O')) ||
        (e.altKey && (e.key === 'n' || e.key === 'N')) ||
        (isMod && e.altKey && (e.key === 'n' || e.key === 'N'))
      ) {
        e.preventDefault()
        handleNewConversation()
      }
    }

    window.addEventListener('keydown', onGlobalKeyDown)
    return () => window.removeEventListener('keydown', onGlobalKeyDown)
  }, [handleNewConversation])

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
      setSuggestionsByConversation((prev) => ({
        ...prev,
        [result.conversation_id]: result.suggestions ?? [],
      }))

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
          stage: 'requirements',
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
    <div className={`app-shell sidebar-state-${sidebarState}`}>
      <Sidebar
        projects={conversations}
        selectedProjectId={isProjectsView ? null : (conversation?.id ?? null)}
        onSelectProject={selectConversation}
        onNewProject={handleNewConversation}
        onDeleteProject={(id) => void handleDeleteProject(id)}
        status={listStatus}
        activeView={isProjectsView ? 'projects' : 'chat'}
        onNavigateProjects={handleNavigateProjects}
        sidebarState={sidebarState}
        onSetSidebarState={setSidebarState}
        sidebarWidth={sidebarWidth}
        onSetSidebarWidth={setSidebarWidth}
        user={{
          name: displayName,
          initials,
          onSignOut: () => instance.logoutRedirect(),
        }}
      />
      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            <div className="topbar-title-row">
              {sidebarState === 'hidden' && (
                <button
                  type="button"
                  className="sidebar-reopen-btn"
                  onClick={() => setSidebarState('expanded')}
                  title="Open sidebar (Ctrl+B)"
                  aria-label="Open sidebar"
                  aria-keyshortcuts="Control+B"
                >
                  <PanelLeftOpen size={18} />
                </button>
              )}
              <h1>{isProjectsView ? 'Projects' : activeProject.name}</h1>
            </div>
          </div>

          <div className="topbar-right">
            <button
              type="button"
              className="theme-toggle-btn theme-toggle-btn--icon"
              title={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
            >
              {theme === 'light' ? <Moon size={16} /> : <Sun size={16} />}
            </button>
          </div>
        </header>

        {isProjectsView ? (
          <ProjectsPage
            onSelectProject={selectConversation}
            onNewProject={handleNewConversation}
          />
        ) : (
          <ChatPanel
            project={activeProject}
            messages={messages}
            files={files}
            suggestions={conversation ? (suggestionsByConversation[conversation.id] ?? []) : []}
            sending={sending}
            error={chatError}
            onSend={(content) => void sendMessage(content)}
            onFilesAdded={addFiles}
            onRemoveFile={removeFile}
          />
        )}
      </div>
    </div>
  )
}
