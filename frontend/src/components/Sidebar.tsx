import { useState, useMemo } from 'react'
import { Sparkles, Plus, Search, FileText, Trash2 } from 'lucide-react'
import type { Project } from '../types'

type SidebarProps = {
  projects: Project[]
  selectedProjectId: string | null
  onSelectProject: (projectId: string) => void
  onNewProject?: () => void
  onDeleteProject?: (projectId: string) => void
  status?: string | null
}

export function Sidebar({
  projects,
  selectedProjectId,
  onSelectProject,
  onNewProject,
  onDeleteProject,
  status,
}: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState('')

  const filteredProjects = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) => p.name.toLowerCase().includes(q))
  }, [projects, searchQuery])

  return (
    <aside className="sidebar">
      <div className="sidebar-brand-container">
        <div className="sidebar-brand">
          <div className="ms-copilot-icon"><Sparkles size={18} /></div>
          <span>DevFoundry</span>
        </div>
        <span className="ms-badge-pill">Copilot</span>
      </div>

      {onNewProject && (
        <button
          type="button"
          className="new-project-button"
          onClick={onNewProject}
        >
          <Plus size={16} style={{ marginRight: '4px' }} /> New Project
        </button>
      )}

      <div className="sidebar-search-box">
        <span className="sidebar-search-icon" style={{ display: 'flex', alignItems: 'center' }}><Search size={14} /></span>
        <input
          type="text"
          className="sidebar-search-input"
          placeholder="Filter projects…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
      </div>

      <div className="sidebar-header-row">
        <p className="sidebar-label">Projects</p>
        <span className="sidebar-count">{filteredProjects.length}</span>
      </div>

      {status && <p className="sidebar-status">{status}</p>}

      <nav className="project-list" aria-label="Projects">
        {filteredProjects.length === 0 && !status && (
          <p className="sidebar-status">
            {searchQuery ? 'No matching projects.' : 'No projects yet.'}
          </p>
        )}

        {filteredProjects.map((project) => {
          const isActive = project.id === selectedProjectId
          return (
            <div
              key={project.id}
              className={`project-item-wrapper ${isActive ? 'active' : ''}`}
            >
              <button
                type="button"
                className="project-item-button"
                onClick={() => onSelectProject(project.id)}
                title={project.name}
              >
                <span className="project-item-icon" style={{ display: 'flex', alignItems: 'center' }}><FileText size={16} /></span>
                <span className="project-item-name">{project.name}</span>
              </button>
              {onDeleteProject && (
                <button
                  type="button"
                  className="project-item-delete"
                  title="Delete conversation"
                  aria-label={`Delete ${project.name}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    if (window.confirm(`Delete "${project.name}"?`)) {
                      onDeleteProject(project.id)
                    }
                  }}
                >
                  <Trash2 size={16} />
                </button>
              )}
            </div>
          )
        })}
      </nav>
    </aside>
  )
}
