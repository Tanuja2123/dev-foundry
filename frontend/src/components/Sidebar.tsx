import type { Project } from '../types'

type SidebarProps = {
  projects: Project[]
  selectedProjectId: string
  onSelectProject: (projectId: string) => void
}

export function Sidebar({
  projects,
  selectedProjectId,
  onSelectProject,
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">DevFoundry</div>
      <p className="sidebar-label">Projects</p>
      <nav className="project-list" aria-label="Projects">
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
