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

export async function getConversation(conversationId: string): Promise<{
  conv_id: string
  title: string
  content?: Array<{ role: 'user' | 'assistant'; text: string }>
}> {
  const response = await fetch(`${apiBaseUrl()}/api/conversation/${conversationId}`)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not load conversation'))
  }
  return response.json()
}


export async function createConversation(): Promise<Conversation> {
  const response = await fetch(`${apiBaseUrl()}/api/conversation`, {
    method: 'POST',
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not create conversation'))
  }
  return response.json() as Promise<Conversation>
}

export async function continueChat(
  conversationId: string,
  userQuery: string,
  file?: File | null,
): Promise<ChatApiResponse> {
  const formData = new FormData()
  formData.append('conversation_id', conversationId)
  formData.append('user_query', userQuery)
  if (file) {
    formData.append('file', file)
  }

  const response = await fetch(`${apiBaseUrl()}/api/chat`, {
    method: 'POST',
    body: formData,
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Chat request failed'))
  }
  return response.json() as Promise<ChatApiResponse>
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await fetch(`${apiBaseUrl()}/api/conversation/${conversationId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not delete conversation'))
  }
}
