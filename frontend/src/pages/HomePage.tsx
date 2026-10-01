import { useMsal } from '@azure/msal-react'
import { useState } from 'react'
import { ChatPanel } from '../components/ChatPanel'
import { Sidebar } from '../components/Sidebar'
import { PROJECTS } from '../constants'
import type { ChatMessage } from '../types'

function createId() {
  return crypto.randomUUID()
}

export function HomePage() {
  const { instance, accounts } = useMsal()
  const account = instance.getActiveAccount() ?? accounts[0]
  const [selectedProjectId, setSelectedProjectId] = useState(PROJECTS[0].id)
  const [messagesByProject, setMessagesByProject] = useState<
    Record<string, ChatMessage[]>
  >({})
  const [filesByProject, setFilesByProject] = useState<Record<string, File[]>>(
    {},
  )

  const project =
    PROJECTS.find((item) => item.id === selectedProjectId) ?? PROJECTS[0]
  const messages = messagesByProject[project.id] ?? []
  const files = filesByProject[project.id] ?? []
  const displayName = account?.name ?? account?.username ?? 'Signed in'

  function sendMessage(content: string) {
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      content,
    }
    const assistantMessage: ChatMessage = {
      id: createId(),
      role: 'assistant',
      content: `Received for ${project.name}. Chat backend is not connected yet.`,
    }

    setMessagesByProject((current) => ({
      ...current,
      [project.id]: [...(current[project.id] ?? []), userMessage, assistantMessage],
    }))
  }

  function addFiles(fileList: FileList) {
    const incoming = Array.from(fileList)
    setFilesByProject((current) => {
      const existing = current[project.id] ?? []
      const names = new Set(existing.map((file) => file.name))
      const unique = incoming.filter((file) => !names.has(file.name))
      return { ...current, [project.id]: [...existing, ...unique] }
    })
  }

  function removeFile(name: string) {
    setFilesByProject((current) => ({
      ...current,
      [project.id]: (current[project.id] ?? []).filter((file) => file.name !== name),
    }))
  }

  return (
    <div className="app-shell">
      <Sidebar
        projects={PROJECTS}
        selectedProjectId={project.id}
        onSelectProject={setSelectedProjectId}
      />
      <div className="main">
        <header className="topbar">
          <div>
            <h1>{project.name}</h1>
            <p>Requirement chat</p>
          </div>
          <div className="topbar-user">
            <span>{displayName}</span>
            <button type="button" onClick={() => instance.logoutRedirect()}>
              Sign out
            </button>
          </div>
        </header>
        <ChatPanel
          project={project}
          messages={messages}
          files={files}
          onSend={sendMessage}
          onFilesAdded={addFiles}
          onRemoveFile={removeFile}
        />
      </div>
    </div>
  )
}
