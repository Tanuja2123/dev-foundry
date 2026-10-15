import { getIdToken } from '../auth/token'
import type { ChatApiResponse, Conversation, Suggestion } from '../types'

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

export async function listConversations(): Promise<Conversation[]> {
  const response = await authFetch(`${apiBaseUrl()}/api/conversation`)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not load conversations'))
  }
  return response.json() as Promise<Conversation[]>
}

export async function getConversation(conversationId: string): Promise<{
  conv_id: string
  title: string
  content?: Array<{ role: 'user' | 'assistant'; text: string }>
  suggestions?: Suggestion[]
}> {
  const response = await authFetch(`${apiBaseUrl()}/api/conversation/${conversationId}`)
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not load conversation'))
  }
  return response.json()
}


export async function createConversation(): Promise<Conversation> {
  const response = await authFetch(`${apiBaseUrl()}/api/conversation`, {
    method: 'POST',
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not create conversation'))
  }
  return response.json() as Promise<Conversation>
}

export async function continueChat(
  conversationId: string | null | undefined,
  userQuery: string,
  file?: File | null,
  signal?: AbortSignal,
): Promise<ChatApiResponse> {
  const formData = new FormData()
  formData.append('user_query', userQuery)
  formData.append('conversation_id', conversationId && conversationId.trim() !== '' ? conversationId : 'null')
  if (file) {
    formData.append('file', file)
  }

  const response = await authFetch(`${apiBaseUrl()}/api/chat`, {
    method: 'POST',
    body: formData,
    signal,
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Chat request failed'))
  }
  return response.json() as Promise<ChatApiResponse>
}

export type ChatProgress = {
  running: boolean
  step?: string
  detail?: string
  elapsed_seconds?: number
  step_seconds?: number
}

/** What a running chat turn is doing right now (polled while waiting for the reply). */
export async function getChatProgress(conversationId: string, signal?: AbortSignal): Promise<ChatProgress> {
  const response = await authFetch(`${apiBaseUrl()}/api/chat/progress/${conversationId}`, { signal })
  if (!response.ok) {
    return { running: false }
  }
  return response.json() as Promise<ChatProgress>
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await authFetch(`${apiBaseUrl()}/api/conversation/${conversationId}`, {
    method: 'DELETE',
  })
  if (!response.ok) {
    throw new Error(await readError(response, 'Could not delete conversation'))
  }
}
