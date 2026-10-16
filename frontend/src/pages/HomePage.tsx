import { useMsal } from '@azure/msal-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { PanelLeftOpen, Moon, Sun } from 'lucide-react'

import {
  cancelChat,
  continueChat,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
} from '../api/chat'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar, type SidebarState } from '../components/Sidebar'
import { TypewriterText } from '../components/TypewriterText'
import { ProjectsPage } from './ProjectsPage'
import type { ChatMessage, Project, Suggestion } from '../types'

function createId() {
  return crypto.randomUUID()
}

// Key of the new chat that is still being created (it has no conversation id yet)
const DRAFT_KEY = '__draft__'

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
  // The open conversation is remembered per account, so a reload or reopened browser continues it.
  const lastConversationKey = `lastConversation:${account?.localAccountId ?? account?.homeAccountId ?? 'anonymous'}`
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
  // Chats run in parallel: each conversation has its own request, Stop button and error.
  // DRAFT_KEY = the new chat that is still being created (before it has an id).
  const [sendingIds, setSendingIds] = useState<Set<string>>(() => new Set())
  const [errorsByConversation, setErrorsByConversation] = useState<Record<string, string | null>>({})
  // "Too late to stop" notice per chat, shown in the waiting bubble until the reply arrives
  const [stopNotices, setStopNotices] = useState<Record<string, string | null>>({})
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
  const controllersRef = useRef<Map<string, AbortController>>(new Map())

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
        // Continue the conversation that was open last (reload, reopened browser, laptop wake-up).
        // After an explicit sign-out the key is cleared, so a fresh sign-in starts with a new chat.
        let lastId: string | null = null
        try {
          lastId = localStorage.getItem(lastConversationKey)
        } catch {
          // storage unavailable (private mode): start with a new chat
        }
        if (lastId && mapped.some((item) => item.id === lastId)) {
          setSelectedId(lastId)
        }
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

  // Remember the open conversation. Not cleared on a null selection here: on page load nothing is selected
  // until the list has loaded, which would wipe the saved id. "New Project" and sign-out clear it explicitly.
  useEffect(() => {
    if (!selectedId) return
    try {
      localStorage.setItem(lastConversationKey, selectedId)
    } catch {
      // storage unavailable: nothing to remember
    }
  }, [selectedId, lastConversationKey])

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
  const activeKey = conversation?.id ?? DRAFT_KEY
  const sending = sendingIds.has(activeKey)
  const chatError = errorsByConversation[activeKey] ?? null

  function setBusy(key: string, busy: boolean) {
    setSendingIds((current) => {
      const next = new Set(current)
      if (busy) next.add(key)
      else next.delete(key)
      return next
    })
  }

  function setChatError(message: string | null, key: string = activeKey) {
    setErrorsByConversation((current) => ({ ...current, [key]: message }))
  }

  const displayName = account?.name ?? account?.username ?? 'Signed in'
  const initials = getInitials(displayName)

  function handleNavigateProjects() {
    navigate('/projects')
  }

  // Stops only the open chat's request; other chats keep running. The server is asked first: before any Azure DevOps
  // action it cancels the turn (nothing is changed or saved); once an action has started, the turn finishes and its
  // reply still arrives, so the request keeps waiting and the bubble says why.
  async function handleStopChat() {
    const key = activeKey
    if (key !== DRAFT_KEY) {
      try {
        const result = await cancelChat(key)
        if (result.status === 'finishing') {
          setStopNotices((current) => ({
            ...current,
            [key]: `Too late to stop: ${result.action ?? 'an Azure DevOps action'} has already started. Finishing it; the reply will show what was done.`,
          }))
          return
        }
      } catch {
        // the server could not be asked: stop waiting anyway
      }
    }
    const controller = controllersRef.current.get(key)
    if (controller) {
      controller.abort()
      controllersRef.current.delete(key)
    }
    setBusy(key, false)
    setMessagesByConversation((current) => {
      const list = current[key] ?? []
      if (list.length === 0) return current
      const last = list[list.length - 1]
      if (last.role === 'user') {
        return {
          ...current,
          [key]: [...list.slice(0, -1), { ...last, stopped: true }],
        }
      }
      return current
    })
    if (key === DRAFT_KEY) {
      setDraftMessages((prev) => {
        if (prev.length === 0) return prev
        const last = prev[prev.length - 1]
        if (last.role === 'user') {
          return [...prev.slice(0, -1), { ...last, stopped: true }]
        }
        return prev
      })
    }
  }

  // Edit a stopped query and re-send it
  async function handleEditAndResend(messageId: string, newContent: string) {
    if (sending) return
    const key = activeKey
    if (conversation) {
      setMessagesByConversation((current) => {
        const list = current[key] ?? []
        const idx = list.findIndex((m) => m.id === messageId)
        if (idx === -1) return current
        return {
          ...current,
          [key]: list.slice(0, idx),
        }
      })
    } else {
      setDraftMessages((prev) => {
        const idx = prev.findIndex((m) => m.id === messageId)
        if (idx === -1) return prev
        return prev.slice(0, idx)
      })
    }
    await sendMessage(newContent)
  }

  // Switching chats never cancels a running request: its reply is delivered to its own chat.
  function selectConversation(conversationId: string) {
    setSelectedId(conversationId)
    setDraftMessages([])
    setDraftFiles([])
    if (location.pathname === '/projects') {
      navigate('/')
    }
  }

  const handleNewConversation = useCallback(() => {
    try {
      localStorage.removeItem(lastConversationKey)
    } catch {
      // ignore
    }
    setSelectedId(null)
    setDraftMessages([])
    setDraftFiles([])
    setErrorsByConversation((current) => ({ ...current, [DRAFT_KEY]: null }))
    if (location.pathname === '/projects') {
      navigate('/')
    }
    setTimeout(() => {
      document.querySelector<HTMLTextAreaElement>('.composer-row textarea')?.focus()
    }, 60)
  }, [location.pathname, navigate, lastConversationKey])

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

  function handleUpdateProjectName(conversationId: string, newName: string) {
    setConversations((prev) =>
      prev.map((c) => (c.id === conversationId ? { ...c, name: newName } : c)),
    )
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
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content,
    }
    const attachedFile = files[0] ?? null
    setChatError(null)

    // A new chat gets its id before the first message, so its request and reply belong to it even if the user
    // opens another chat (or starts another new one) while it runs.
    let conversationId = conversation?.id ?? null
    if (!conversationId) {
      setBusy(DRAFT_KEY, true)
      setDraftMessages([userMessage])
      let created
      try {
        created = await createConversation()
      } catch (reason) {
        setBusy(DRAFT_KEY, false)
        setChatError(reason instanceof Error ? reason.message : 'Could not start the chat.', DRAFT_KEY)
        return
      }
      const newId = created.conversation_id
      conversationId = newId
      const newProject: Project = { id: newId, name: created.title || 'New Project', stage: 'requirements' }
      setConversations((prev) => [newProject, ...prev])
      setMessagesByConversation((prev) => ({ ...prev, [newId]: [userMessage] }))
      setDraftMessages([])
      setDraftFiles([])
      setBusy(DRAFT_KEY, false)
      setSelectedId((current) => current ?? newId)    // open it, unless the user already moved to another chat
    } else {
      const id = conversationId
      setMessagesByConversation((current) => ({ ...current, [id]: [...(current[id] ?? []), userMessage] }))
    }

    const key = conversationId
    const controller = new AbortController()
    controllersRef.current.set(key, controller)
    setBusy(key, true)
    try {
      const result = await continueChat(key, content, attachedFile, controller.signal)
      const assistantMessage: ChatMessage = {
        id: createId(),
        role: 'assistant',
        content: result.response,
      }
      setSuggestionsByConversation((prev) => ({ ...prev, [key]: result.suggestions ?? [] }))
      if (result.title) {
        // the sidebar and header type the new title in (TypewriterText animates text changes)
        setConversations((current) => current.map((p) => (p.id === key ? { ...p, name: result.title! } : p)))
      }
      if (attachedFile) {
        setFilesByConversation((current) => ({ ...current, [key]: [] }))
      }
      setMessagesByConversation((current) => ({ ...current, [key]: [...(current[key] ?? []), assistantMessage] }))
    } catch (reason) {
      if (
        (reason instanceof DOMException && reason.name === 'AbortError') ||
        (reason instanceof Error && reason.name === 'AbortError') ||
        controller.signal.aborted
      ) {
        return
      }
      setChatError(reason instanceof Error ? reason.message : 'Chat request failed.', key)
    } finally {
      if (controllersRef.current.get(key) === controller) {
        controllersRef.current.delete(key)
      }
      setBusy(key, false)
      setStopNotices((current) => ({ ...current, [key]: null }))
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
        busyProjectIds={sendingIds}
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
          onSignOut: () => {
            try {
              localStorage.removeItem(lastConversationKey)
            } catch {
              // ignore
            }
            void instance.logoutRedirect()
          },
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
              {!isProjectsView && (
                <div className="topbar-title-with-actions">
                  <h1>
                    {/* A new chat starts as "New Project" and its product name is typed in when it arrives */}
                    <TypewriterText key={activeProject.id || 'draft'} text={activeProject.name} speed={28} />
                  </h1>
                </div>
              )}
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
            onDeleteProject={(id) => void handleDeleteProject(id)}
            onUpdateProjectName={handleUpdateProjectName}
          />
        ) : (
          <ChatPanel
            project={activeProject}
            messages={messages}
            files={files}
            suggestions={conversation ? (suggestionsByConversation[conversation.id] ?? []) : []}
            sending={sending}
            error={chatError}
            stopNotice={stopNotices[activeKey] ?? null}
            onSend={(content) => void sendMessage(content)}
            onStop={() => void handleStopChat()}
            onEditAndResend={(messageId, newContent) => void handleEditAndResend(messageId, newContent)}
            onFilesAdded={addFiles}
            onRemoveFile={removeFile}
          />
        )}
      </div>
    </div>
  )
}
