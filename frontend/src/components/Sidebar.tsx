import type { Project } from '../types'

type SidebarProps = {
  projects: Project[]
  selectedProjectId: string | null
  onSelectProject: (projectId: string) => void
  status?: string | null
}

export function Sidebar({
  projects,
  selectedProjectId,
  onSelectProject,
  status,
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">DevFoundry</div>
      <p className="sidebar-label">Conversations</p>
      {status && <p className="sidebar-status">{status}</p>}
      <nav className="project-list" aria-label="Conversations">
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            className={
              project.id === selectedProjectId
                ? 'project-item project-item-active'
                : 'project-item'
            }
            onClick={() => onSelectProject(project.id)}
          >
            {project.name}
          </button>
        ))}
      </nav>
    </aside>
  )
}
