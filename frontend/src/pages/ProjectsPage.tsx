import { useEffect, useMemo, useState } from 'react'
import {
  Check,
  Circle,
  Folder,
  FileText,
  Cpu,
  GitBranch,
  Search,
  X,
  ExternalLink,
  MessageSquare,
  ChevronRight,
  FolderOpen,
  Trash2,
  Pencil,
} from 'lucide-react'
import { deleteConversation } from '../api/chat'
import { fetchProjectsOverview, updateProjectName } from '../api/projects'
import { ConfirmModal } from '../components/ConfirmModal'
import { DocumentDrawer } from '../components/DocumentDrawer'
import { EditProjectModal } from '../components/EditProjectModal'
import type { ProjectOverview } from '../types'

type ProjectsPageProps = {
  onSelectProject: (projectId: string) => void
  onNewProject?: () => void
  onDeleteProject?: (projectId: string) => void | Promise<void>
  onUpdateProjectName?: (projectId: string, newName: string) => void
}

type ActiveDrawerState = {
  isOpen: boolean
  docType: 'PRD' | 'TRD' | null
  projectName: string
  content: string | null
  updatedAt?: string | null
}

export function ProjectsPage({
  onSelectProject,
  onDeleteProject,
  onUpdateProjectName,
}: ProjectsPageProps) {
  const [projects, setProjects] = useState<ProjectOverview[]>([])
  const [loading, setLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)
  const [editTarget, setEditTarget] = useState<ProjectOverview | null>(null)
  const [drawerState, setDrawerState] = useState<ActiveDrawerState>({
    isOpen: false,
    docType: null,
    projectName: '',
    content: null,
    updatedAt: null,
  })

  async function confirmDelete() {
    if (!deleteTarget) return
    setIsDeleting(true)
    try {
      if (onDeleteProject) {
        await onDeleteProject(deleteTarget.id)
      } else {
        await deleteConversation(deleteTarget.id)
      }
      setProjects((prev) => prev.filter((p) => p.conversation_id !== deleteTarget.id))
      setDeleteTarget(null)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Could not delete project.')
    } finally {
      setIsDeleting(false)
    }
  }

  async function handleSaveProjectName(newName: string) {
    if (!editTarget) return
    const updated = await updateProjectName(editTarget.conversation_id, newName)
    setProjects((prev) =>
      prev.map((p) =>
        p.conversation_id === editTarget.conversation_id
          ? {
              ...p,
              project_name: updated.project_name,
              project_link: updated.project_link ?? p.project_link,
              repo_link: updated.repo_link ?? p.repo_link,
            }
          : p,
      ),
    )
    onUpdateProjectName?.(editTarget.conversation_id, updated.project_name || newName)
  }

  // Fetch projects on mount
  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const data = await fetchProjectsOverview()
        if (!cancelled) {
          setProjects(data)
        }
      } catch {
        // Handled in api layer
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  // Filter projects by search query
  const filteredProjects = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) => {
      const name = (p.project_name || 'Untitled Project').toLowerCase()
      const desc = (p.description || '').toLowerCase()
      return name.includes(q) || desc.includes(q)
    })
  }, [projects, searchQuery])

  // Summary Metrics
  const metrics = useMemo(() => {
    const total = projects.length
    const prdCount = projects.filter((p) => p.prd.status === 'created').length
    const trdCount = projects.filter((p) => p.trd.status === 'created').length
    const repoCount = projects.filter((p) => Boolean(p.repo_link)).length
    return { total, prdCount, trdCount, repoCount }
  }, [projects])

  function handleOpenDoc(
    docType: 'PRD' | 'TRD',
    projectName: string | null,
    content: string | null,
    updatedAt?: string | null,
  ) {
    setDrawerState({
      isOpen: true,
      docType,
      projectName: projectName || 'Untitled Project',
      content,
      updatedAt,
    })
  }

  function handleCloseDrawer() {
    setDrawerState((prev) => ({ ...prev, isOpen: false }))
  }

  return (
    <div className="projects-page-container">
      {/* Page Header */}
      <div className="projects-header-section">
        <div className="projects-title-row">
          <div className="projects-title-left">
            <h1 className="projects-main-title">Projects</h1>
            <p className="projects-subtitle">
              Centralized portfolio overview of requirement documents, technical architectures, and cloud repositories.
            </p>
          </div>
        </div>

        {/* Metrics Bar */}
        <div className="projects-metrics-grid">
          <div className="metric-card">
            <span className="metric-icon">
              <Folder size={18} />
            </span>
            <div className="metric-content">
              <span className="metric-value">{metrics.total}</span>
              <span className="metric-label">Total Projects</span>
            </div>
          </div>
          <div className="metric-card">
            <span className="metric-icon">
              <FileText size={18} />
            </span>
            <div className="metric-content">
              <span className="metric-value">{metrics.prdCount}</span>
              <span className="metric-label">PRDs Created</span>
            </div>
          </div>
          <div className="metric-card">
            <span className="metric-icon">
              <Cpu size={18} />
            </span>
            <div className="metric-content">
              <span className="metric-value">{metrics.trdCount}</span>
              <span className="metric-label">TRDs Created</span>
            </div>
          </div>
          <div className="metric-card">
            <span className="metric-icon">
              <GitBranch size={18} />
            </span>
            <div className="metric-content">
              <span className="metric-value">{metrics.repoCount}</span>
              <span className="metric-label">Repositories Linked</span>
            </div>
          </div>
        </div>

        {/* Filter and Search Bar */}
        <div className="projects-controls-bar">
          <div className="projects-search-input-wrapper">
            <Search size={15} className="projects-search-icon" />
            <input
              type="text"
              className="projects-search-input"
              placeholder="Search by project name or description…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery && (
              <button
                type="button"
                className="projects-search-clear"
                onClick={() => setSearchQuery('')}
                title="Clear filter"
              >
                <X size={13} />
              </button>
            )}
          </div>
          <div className="projects-controls-meta">
            <span className="projects-count-pill">
              Showing {filteredProjects.length} of {projects.length} projects
            </span>
          </div>
        </div>
      </div>

      {/* Projects Table */}
      <div className="projects-table-card">
        {loading ? (
          <div className="projects-loading-state">
            <div className="projects-spinner" />
            <p>Loading projects portfolio…</p>
          </div>
        ) : filteredProjects.length === 0 ? (
          <div className="projects-empty-state">
            <div className="projects-empty-icon">
              <FolderOpen size={36} />
            </div>
            <h3>No matching projects found</h3>
            <p>
              {searchQuery
                ? 'Try adjusting your search criteria.'
                : 'No projects have been created yet. Start a new project to get started.'}
            </p>
          </div>
        ) : (
          <div className="projects-table-responsive">
            <table className="projects-table">
              <thead>
                <tr>
                  <th className="th-project-name">Project Name</th>
                  <th className="th-doc-status">PRD</th>
                  <th className="th-doc-status">TRD</th>
                  <th className="th-external-link">Project</th>
                  <th className="th-external-link">Repo</th>
                  <th className="th-actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredProjects.map((project) => {
                  const displayName = project.project_name || 'Untitled Project'
                  const displayDesc =
                    project.description || 'No description provided yet.'
                  const isPrdCreated = project.prd.status === 'created'
                  const isTrdCreated = project.trd.status === 'created'

                  return (
                    <tr key={project.conversation_id} className="projects-table-row">
                      {/* 1. Project Name & Description */}
                      <td className="td-project-name">
                        <div className="project-name-cell">
                          <div className="project-name-title-row">
                            <button
                              type="button"
                              className="project-name-link"
                              onClick={() => onSelectProject(project.conversation_id)}
                              title={`Open workspace for ${displayName}`}
                            >
                              <span className="project-cell-icon">
                                <Folder size={15} />
                              </span>
                              <span className="project-cell-title">{displayName}</span>
                            </button>
                            <button
                              type="button"
                              className="project-cell-edit-btn"
                              onClick={() => setEditTarget(project)}
                              title={`Edit project name for "${displayName}"`}
                              aria-label={`Edit project name for "${displayName}"`}
                            >
                              <Pencil size={13} />
                            </button>
                          </div>
                          <p className="project-cell-description" title={displayDesc}>
                            {displayDesc}
                          </p>
                        </div>
                      </td>

                      {/* 2. PRD Status */}
                      <td className="td-doc-status">
                        {isPrdCreated ? (
                          <button
                            type="button"
                            className="doc-status-badge created prd"
                            onClick={() =>
                              handleOpenDoc(
                                'PRD',
                                project.project_name,
                                project.prd.content,
                                project.prd.updated_at,
                              )
                            }
                            title="Click to view PRD document"
                          >
                            <span className="badge-icon">
                              <Check size={13} strokeWidth={2.5} />
                            </span>
                            <span>Created</span>
                            <ChevronRight size={13} className="badge-arrow" />
                          </button>
                        ) : (
                          <span className="doc-status-badge not-created" title="PRD not created yet">
                            <span className="badge-icon">
                              <Circle size={10} strokeWidth={2} />
                            </span>
                            <span>Not Created</span>
                          </span>
                        )}
                      </td>

                      {/* 3. TRD Status */}
                      <td className="td-doc-status">
                        {isTrdCreated ? (
                          <button
                            type="button"
                            className="doc-status-badge created trd"
                            onClick={() =>
                              handleOpenDoc(
                                'TRD',
                                project.project_name,
                                project.trd.content,
                                project.trd.updated_at,
                              )
                            }
                            title="Click to view TRD document"
                          >
                            <span className="badge-icon">
                              <Check size={13} strokeWidth={2.5} />
                            </span>
                            <span>Created</span>
                            <ChevronRight size={13} className="badge-arrow" />
                          </button>
                        ) : (
                          <span className="doc-status-badge not-created" title="TRD not created yet">
                            <span className="badge-icon">
                              <Circle size={10} strokeWidth={2} />
                            </span>
                            <span>Not Created</span>
                          </span>
                        )}
                      </td>

                      {/* 4. Azure DevOps Project Link */}
                      <td className="td-external-link">
                        {project.project_link ? (
                          <a
                            href={project.project_link}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="external-resource-link"
                            title={`Open ${displayName} in Azure DevOps`}
                          >
                            <Folder size={14} className="link-icon" />
                            <span>Project</span>
                            <ExternalLink size={11} className="link-ext-symbol" />
                          </a>
                        ) : (
                          <span className="external-resource-empty" title="No project link available">
                            —
                          </span>
                        )}
                      </td>

                      {/* 5. Azure DevOps Git Repo Link */}
                      <td className="td-external-link">
                        {project.repo_link ? (
                          <a
                            href={project.repo_link}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="external-resource-link repo"
                            title={`Open Git repository for ${displayName}`}
                          >
                            <GitBranch size={14} className="link-icon" />
                            <span>Repo</span>
                            <ExternalLink size={11} className="link-ext-symbol" />
                          </a>
                        ) : (
                          <span className="external-resource-empty" title="No repository link available">
                            —
                          </span>
                        )}
                      </td>

                      {/* 6. Action / Open Chat, Edit & Delete Project */}
                      <td className="td-actions">
                        <div className="project-actions-group">
                          <button
                            type="button"
                            className="project-action-btn project-action-btn--icon project-action-btn--edit"
                            onClick={() => setEditTarget(project)}
                            title={`Edit project name for "${displayName}"`}
                            aria-label={`Edit project name for "${displayName}"`}
                          >
                            <Pencil size={15} />
                          </button>
                          <button
                            type="button"
                            className="project-action-btn project-action-btn--icon"
                            onClick={() => onSelectProject(project.conversation_id)}
                            title={`Open chat workspace for ${displayName}`}
                            aria-label={`Open chat workspace for ${displayName}`}
                          >
                            <MessageSquare size={16} />
                          </button>
                          <button
                            type="button"
                            className="project-action-btn project-action-btn--icon project-action-btn--delete"
                            onClick={() => setDeleteTarget({ id: project.conversation_id, name: displayName })}
                            title={`Delete "${displayName}"`}
                            aria-label={`Delete "${displayName}"`}
                          >
                            <Trash2 size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Slide-over Document Drawer */}
      <DocumentDrawer
        isOpen={drawerState.isOpen}
        docType={drawerState.docType}
        projectName={drawerState.projectName}
        content={drawerState.content}
        updatedAt={drawerState.updatedAt}
        onClose={handleCloseDrawer}
      />

      {/* Edit Project Name Modal (with ADO sync support) */}
      <EditProjectModal
        isOpen={Boolean(editTarget)}
        initialName={editTarget?.project_name || ''}
        isAdoLinked={Boolean(editTarget?.project_link)}
        onSave={handleSaveProjectName}
        onClose={() => setEditTarget(null)}
      />

      {/* Re-verify Delete Confirmation Modal */}
      <ConfirmModal
        isOpen={Boolean(deleteTarget)}
        title="Delete Project"
        message={`Do you want to delete "${deleteTarget?.name || 'this project'}"? This will permanently delete the project conversation and its requirement documents. This action cannot be undone.`}
        confirmLabel="Delete"
        cancelLabel="Cancel"
        isLoading={isDeleting}
        onConfirm={confirmDelete}
        onCancel={() => {
          if (!isDeleting) setDeleteTarget(null)
        }}
      />
    </div>
  )
}
