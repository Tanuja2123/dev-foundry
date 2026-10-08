import { useState, useMemo } from 'react'
import type { Project } from '../types'
import {
  BoardsIcon,
  ReposIcon,
  PipelinesIcon,
  CopilotSparkleIcon,
  SettingsGearIcon,
  AdoLogo,
} from './AdoIcons'

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
    <aside className="ado-sidebar">
      {/* Azure DevOps Project Context Header */}
      <div className="ado-sidebar-header">
        <div className="ado-project-avatar" title="Azure DevOps Project">
          <AdoLogo size={18} />
        </div>
        <div className="ado-project-info">
          <span className="ado-project-name">DevFoundry</span>
          <span className="ado-project-subtext">Azure DevOps Hub</span>
        </div>
      </div>

      {/* Azure DevOps Standard Service Hubs */}
      <div className="ado-hub-nav">
        <div className="ado-hub-item active" title="DevFoundry Multi-Agent Copilot">
          <span className="ado-hub-icon ado-hub-copilot">
            <CopilotSparkleIcon size={16} />
          </span>
          <span className="ado-hub-title">DevFoundry Copilot</span>
          <span className="ado-hub-badge">Active</span>
        </div>

        <div className="ado-hub-item disabled" title="Azure Boards (Coming soon)">
          <span className="ado-hub-icon ado-hub-boards">
            <BoardsIcon size={16} />
          </span>
          <span className="ado-hub-title">Boards</span>
        </div>

        <div className="ado-hub-item disabled" title="Azure Repos (Git Repositories)">
          <span className="ado-hub-icon ado-hub-repos">
            <ReposIcon size={16} />
          </span>
          <span className="ado-hub-title">Repos</span>
        </div>

        <div className="ado-hub-item disabled" title="Azure Pipelines (CI/CD)">
          <span className="ado-hub-icon ado-hub-pipelines">
            <PipelinesIcon size={16} />
          </span>
          <span className="ado-hub-title">Pipelines</span>
        </div>
      </div>

      <div className="ado-sidebar-divider" />

      {/* Primary Action Button */}
      {onNewProject && (
        <button
          type="button"
          className="ado-btn-primary ado-new-project-btn"
          onClick={onNewProject}
        >
          <span className="ado-btn-plus">＋</span>
          <span>New Project</span>
        </button>
      )}

      {/* Filter / Search Bar */}
      <div className="ado-search-box">
        <span className="ado-search-icon">🔍</span>
        <input
          type="text"
          className="ado-search-input"
          placeholder="Filter projects…"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
        {searchQuery && (
          <button
            type="button"
            className="ado-search-clear"
            onClick={() => setSearchQuery('')}
            aria-label="Clear filter"
          >
            ✕
          </button>
        )}
      </div>

      {/* Section Header */}
      <div className="ado-section-header">
        <span className="ado-section-label">PROJECT WORKSPACES</span>
        <span className="ado-section-count">{filteredProjects.length}</span>
      </div>

      {status && <p className="ado-sidebar-status">{status}</p>}

      {/* Projects List */}
      <nav className="ado-project-list" aria-label="Projects">
        {filteredProjects.length === 0 && !status && (
          <div className="ado-empty-state">
            <span>{searchQuery ? 'No matching projects.' : 'No projects yet.'}</span>
          </div>
        )}

        {filteredProjects.map((project) => {
          const isActive = project.id === selectedProjectId
          return (
            <div
              key={project.id}
              className={`ado-project-item ${isActive ? 'active' : ''}`}
            >
              <button
                type="button"
                className="ado-project-btn"
                onClick={() => onSelectProject(project.id)}
                title={project.name}
              >
                <span className="ado-item-icon">
                  <ReposIcon size={14} />
                </span>
                <span className="ado-item-name">{project.name}</span>
              </button>

              {onDeleteProject && (
                <button
                  type="button"
                  className="ado-item-delete"
                  title="Delete project conversation"
                  aria-label={`Delete ${project.name}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    if (window.confirm(`Delete "${project.name}" from Azure DevOps?`)) {
                      onDeleteProject(project.id)
                    }
                  }}
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/>
                    <path fillRule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/>
                  </svg>
                </button>
              )}
            </div>
          )
        })}
      </nav>

      {/* Bottom Azure DevOps Project Settings */}
      <div className="ado-sidebar-footer">
        <div className="ado-footer-item" title="Azure DevOps Project Settings">
          <SettingsGearIcon size={16} />
          <span>Project settings</span>
        </div>
      </div>
    </aside>
  )
}
