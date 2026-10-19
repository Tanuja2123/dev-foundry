import { getIdToken } from '../auth/token'
import type { ProjectOverview } from '../types'

function apiBaseUrl() {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim()
  return (configured || 'http://localhost:8000').replace(/\/$/, '')
}

/**
 * fetch() with the signed-in user's token. On 401 the token is renewed once and the request retried.
 */
async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  async function send(forceRefresh: boolean) {
    const headers = new Headers(init.headers)
    headers.set('Authorization', `Bearer ${await getIdToken(forceRefresh)}`)
    return fetch(url, { ...init, headers })
  }
  const response = await send(false)
  return response.status === 401 ? send(true) : response
}

async function readError(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail) return body.detail
  } catch {
    // The body is not JSON.
  }
  return `${fallback} (${response.status})`
}

/**
 * Fetches the projects portfolio from the backend API (GET /api/projects).
 * Returns real projects owned by the currently signed-in user.
 */
export async function fetchProjectsOverview(): Promise<ProjectOverview[]> {
  const url = `${apiBaseUrl()}/api/projects`
  const response = await authFetch(url)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not fetch projects overview'))
  }
  const data = (await response.json()) as ProjectOverview[]
  return Array.isArray(data) ? data : []
}

/**
 * One project's overview (its PRD / TRD as Markdown), e.g. for the "Open PRD" button under a chat reply.
 */
export async function fetchProjectOverview(conversationId: string): Promise<ProjectOverview | null> {
  const projects = await fetchProjectsOverview()
  return projects.find((p) => p.conversation_id === conversationId) ?? null
}

/**
 * Updates the project name (and Azure DevOps project name if created).
 * Returns the updated project overview.
 */
export async function updateProjectName(
  conversationId: string,
  projectName: string,
): Promise<ProjectOverview> {
  const url = `${apiBaseUrl()}/api/projects/${conversationId}`
  const response = await authFetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_name: projectName }),
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not update project name'))
  }
  return (await response.json()) as ProjectOverview
}
