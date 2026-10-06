export type Project = {
  id: string
  name: string
}

export type Conversation = {
  conversation_id: string
  title: string
}

export type ChatApiResponse = {
  conversation_id: string
  response: string
}

export type ChatRole = 'user' | 'assistant'

export type ChatMessage = {
  id: string
  role: ChatRole
  content: string
}
