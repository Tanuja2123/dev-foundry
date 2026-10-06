import type { ChatApiResponse, Conversation } from '../types'

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

export async function listConversations(): Promise<Conversation[]> {
  const response = await fetch(`${apiBaseUrl()}/api/conversation`)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not load conversations'))
  }
  return response.json() as Promise<Conversation[]>
}

export async function continueChat(
  conversationId: string,
  userQuery: string,
): Promise<ChatApiResponse> {
  const response = await fetch(`${apiBaseUrl()}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      conversation_id: conversationId,
      user_query: userQuery,
    }),
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Chat request failed'))
  }
  return response.json() as Promise<ChatApiResponse>
}
