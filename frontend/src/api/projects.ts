import type { ProjectOverview } from '../types'

function apiBaseUrl() {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim()
  return (configured || 'http://localhost:8000').replace(/\/$/, '')
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
 * Fetches the projects portfolio from the real backend API (GET /api/projects).
 * Returns real projects fetched directly from the database.
 */
export async function fetchProjectsOverview(): Promise<ProjectOverview[]> {
  const url = `${apiBaseUrl()}/api/projects`
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not fetch projects overview'))
  }
  const data = (await response.json()) as ProjectOverview[]
  return Array.isArray(data) ? data : []
}
