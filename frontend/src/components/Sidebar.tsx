import { useState, useMemo, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import {
  PanelLeftClose,
  Plus,
  Table,
  Search,
  FileText,
  Trash2,
  LogOut,
  User,
  LogIn,
} from 'lucide-react'
import type { Project } from '../types'
import { TypewriterText } from './TypewriterText'

export type SidebarState = 'expanded' | 'collapsed' | 'hidden'

type SidebarUserProfile = {
  name: string
  initials: string
  onSignOut?: () => void
  onSignIn?: () => void
}

type SidebarProps = {
  projects: Project[]
  selectedProjectId: string | null
  onSelectProject: (projectId: string) => void
  onNewProject?: () => void
  onDeleteProject?: (projectId: string) => void
  status?: string | null
  activeView?: 'chat' | 'projects'
  onNavigateProjects?: () => void
  sidebarState?: SidebarState
  onSetSidebarState?: (state: SidebarState) => void
  sidebarWidth?: number
  onSetSidebarWidth?: (width: number) => void
  user?: SidebarUserProfile | null
}

export function Sidebar({
  projects,
  selectedProjectId,
  onSelectProject,
  onNewProject,
  onDeleteProject,
  status,
  activeView = 'chat',
  onNavigateProjects,
  sidebarState = 'expanded',
  onSetSidebarState,
  sidebarWidth = 280,
  onSetSidebarWidth,
  user,
}: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState('')
  const [isProfileOpen, setIsProfileOpen] = useState(false)
  const [isResizing, setIsResizing] = useState(false)
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null)
  const profileContainerRef = useRef<HTMLDivElement>(null)

  const isExpanded = sidebarState !== 'hidden'
  const isHidden = sidebarState === 'hidden'

  // Close confirmation modal on Escape
  useEffect(() => {
    if (!projectToDelete) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setProjectToDelete(null)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [projectToDelete])

  // Close profile popup on click outside or Escape
  useEffect(() => {
    if (!isProfileOpen) return

    const handleClickOutside = (event: MouseEvent) => {
      if (
        profileContainerRef.current &&
        !profileContainerRef.current.contains(event.target as Node)
      ) {
        setIsProfileOpen(false)
      }
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsProfileOpen(false)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [isProfileOpen])

  // Drag resizer handling to dynamically set sidebar custom width
  const handleMouseDownResize = (e: React.MouseEvent) => {
    e.preventDefault()
    setIsResizing(true)
    const startX = e.clientX
    const startWidth = sidebarWidth

    const handleMouseMove = (moveEvent: MouseEvent) => {
      const deltaX = moveEvent.clientX - startX
      const nextWidth = Math.min(Math.max(Math.round(startWidth + deltaX), 180), 520)
      onSetSidebarWidth?.(nextWidth)
      if (sidebarState !== 'expanded') {
        onSetSidebarState?.('expanded')
      }
    }

    const handleMouseUp = () => {
      setIsResizing(false)
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
  }

  // Wheel resize scroller on resizer handle or width badge
  const handleWheelResize = (e: React.WheelEvent) => {
    if (!isExpanded) return
    e.preventDefault()
    const step = e.deltaY < 0 ? 12 : -12
    const nextWidth = Math.min(Math.max(sidebarWidth + step, 180), 520)
    onSetSidebarWidth?.(nextWidth)
  }

  const handleDoubleClickReset = () => {
    onSetSidebarWidth?.(280)
  }

  const filteredProjects = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) => p.name.toLowerCase().includes(q))
  }, [projects, searchQuery])

  return (
    <aside
      className={`sidebar sidebar-${sidebarState} ${isHidden ? 'hidden' : ''} ${isResizing ? 'is-resizing' : ''}`}
      style={isExpanded ? { width: `${sidebarWidth}px` } : undefined}
      aria-label="Sidebar navigation"
      aria-hidden={isHidden}
    >
      {/* Top Header / Brand Row with Complete Collapser */}
      <div className="sidebar-brand-container">
        <div className="sidebar-brand">
          <span>DevFoundry</span>
        </div>

        {/* Complete Collapser Control */}
        <div className="sidebar-state-controls">
          <button
            type="button"
            className="sidebar-control-btn"
            onClick={() => onSetSidebarState?.('hidden')}
            title="Collapse sidebar (Ctrl+B)"
            aria-label="Collapse sidebar completely"
          >
            <PanelLeftClose size={18} />
          </button>
        </div>
      </div>

      {/* New Project Button */}
      {onNewProject && (
        <button
          type="button"
          className="new-project-button"
          onClick={onNewProject}
          title={isExpanded ? 'New Project (Ctrl+Shift+O or Alt+N)' : 'New Project'}
          aria-label="New Project"
          aria-keyshortcuts="Control+Shift+O Alt+N"
        >
          <Plus size={16} />
          {isExpanded && <span>New Project</span>}
        </button>
      )}

      {/* Projects Overview Navigation Button */}
      {onNavigateProjects && (
        <div className="sidebar-nav-section">
          <button
            type="button"
            className={`sidebar-nav-item ${activeView === 'projects' ? 'active' : ''}`}
            onClick={onNavigateProjects}
            title="Projects overview (Table view)"
            aria-label="Projects overview"
          >
            <span className="sidebar-nav-icon">
              <Table size={16} />
            </span>
            {isExpanded && <span className="sidebar-nav-label">Projects</span>}
          </button>
        </div>
      )}

      {/* Project Search & Filter (Expanded Only) */}
      {isExpanded && (
        <div className="sidebar-search-box">
          <Search size={14} className="sidebar-search-icon" />
          <input
            type="text"
            className="sidebar-search-input"
            placeholder="Filter projects…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      )}

      {/* Projects List Header (Expanded Only) */}
      {isExpanded && (
        <div className="sidebar-header-row">
          <p className="sidebar-label">Projects</p>
          <span className="sidebar-count">{filteredProjects.length}</span>
        </div>
      )}

      {status && isExpanded && <p className="sidebar-status">{status}</p>}

      {/* Project List */}
      <nav className="project-list" aria-label="Projects">
        {isExpanded && filteredProjects.length === 0 && !status && (
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
                aria-label={project.name}
              >
                <span className="project-item-icon">
                  <FileText size={15} />
                </span>
                {isExpanded && (
                  <span className="project-item-name">
                    <TypewriterText text={project.name} animateOnChange={isActive} animateOnMount={isActive} speed={28} />
                  </span>
                )}
              </button>
              {isExpanded && onDeleteProject && (
                <button
                  type="button"
                  className="project-item-delete"
                  title="Delete conversation"
                  aria-label={`Delete ${project.name}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    setProjectToDelete(project)
                  }}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          )
        })}
      </nav>

      {/* Bottom User / Account Section (ChatGPT Style) */}
      <div className="sidebar-footer">
        {user ? (
          <div className="sidebar-user-container" ref={profileContainerRef}>
            <button
              type="button"
              className={`sidebar-user-card ${isProfileOpen ? 'open' : ''}`}
              onClick={() => setIsProfileOpen((prev) => !prev)}
              aria-expanded={isProfileOpen}
              aria-haspopup="menu"
              title={`Account options for ${user.name}`}
            >
              <div className="sidebar-user-info">
                <div className="sidebar-avatar-monogram">
                  {user.initials || <User size={14} />}
                </div>
                {isExpanded && (
                  <div className="sidebar-user-details">
                    <span className="sidebar-user-name">{user.name}</span>
                  </div>
                )}
              </div>
            </button>

            {/* Profile Popover / Dropdown Menu when clicked */}
            {isProfileOpen && (
              <div
                className="sidebar-user-popup"
                role="menu"
                aria-label="User account menu"
              >
                <div className="sidebar-popup-user-section">
                  <div className="sidebar-popup-avatar">
                    {user.initials || <User size={18} />}
                  </div>
                  <div className="sidebar-popup-identity">
                    {/* COMPLETE FULL USER NAME - No ellipsis truncation */}
                    <span className="sidebar-popup-name" title={user.name}>
                      {user.name}
                    </span>
                    <span className="sidebar-popup-tag">Microsoft Account</span>
                  </div>
                </div>

                <div className="sidebar-popup-divider" />

                {/* Signout button with icon and written text under user name */}
                {user.onSignOut && (
                  <button
                    type="button"
                    className="sidebar-popup-signout-btn"
                    role="menuitem"
                    onClick={() => {
                      setIsProfileOpen(false)
                      user.onSignOut?.()
                    }}
                  >
                    <LogOut size={16} />
                    <span>Sign out</span>
                  </button>
                )}
              </div>
            )}
          </div>
        ) : (
          <button
            type="button"
            className="sidebar-signin-btn"
            title="Sign in with Microsoft"
          >
            <LogIn size={16} />
            {isExpanded && <span>Sign in</span>}
          </button>
        )}
      </div>

      {/* Draggable Resizer / Scroller on the right border */}
      {isExpanded && (
        <div
          className={`sidebar-resizer ${isResizing ? 'is-dragging' : ''}`}
          onMouseDown={handleMouseDownResize}
          onWheel={handleWheelResize}
          onDoubleClick={handleDoubleClickReset}
          title="Drag or scroll to adjust sidebar width (Double-click to reset)"
          role="separator"
          aria-orientation="vertical"
        >
          <div className="sidebar-resizer-line" />
          <div className="sidebar-resizer-handle" />
        </div>
      )}

      {/* Custom Confirmation Modal for Deleting Projects */}
      {projectToDelete &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            className="modal-overlay"
            onClick={() => setProjectToDelete(null)}
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-delete-title"
          >
            <div
              className="confirm-modal-box"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="confirm-modal-header">
                <div className="confirm-modal-icon-badge">
                  <Trash2 size={18} className="confirm-modal-icon" />
                </div>
                <div className="confirm-modal-content">
                  <h3 id="confirm-delete-title" className="confirm-modal-title">
                    Delete Project?
                  </h3>
                  <p className="confirm-modal-desc">
                    Are you sure you want to delete{' '}
                    <strong className="confirm-modal-name">
                      "{projectToDelete.name}"
                    </strong>
                    ? This will permanently delete the conversation and cannot be undone.
                  </p>
                </div>
              </div>

              <div className="confirm-modal-footer">
                <button
                  type="button"
                  className="btn-modal-cancel"
                  onClick={() => setProjectToDelete(null)}
                >
                  No, cancel
                </button>
                <button
                  type="button"
                  className="btn-modal-delete"
                  onClick={() => {
                    const id = projectToDelete.id
                    setProjectToDelete(null)
                    onDeleteProject?.(id)
                  }}
                >
                  Yes, delete
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}
    </aside>
  )
}
