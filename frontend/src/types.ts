export type Project = {
  id: string
  name: string
  stage?: string
}

export type Conversation = {
  conversation_id: string
  title: string
  stage?: string
}

export type Suggestion = {
  label: string
  prompt: string
}

export type ChatApiResponse = {
  conversation_id: string
  response: string
  suggestions?: Suggestion[]
  title?: string | null
}

export type ChatRole = 'user' | 'assistant'

export type ChatMessage = {
  id: string
  role: ChatRole
  content: string
}
export type DocumentStatus = 'created' | 'not_created'

export type DocumentDetails = {
  status: DocumentStatus
  content: string | null
  updated_at?: string | null
}

export type ProjectOverview = {
  conversation_id: string
  project_name: string | null
  description: string | null
  created_at: string
  prd: DocumentDetails
  trd: DocumentDetails
  project_link: string | null
  repo_link: string | null
}
