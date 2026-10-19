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

// A button under an assistant reply (UX-9): open a document in the chat or on the Projects page, or an Azure DevOps link
export type ReplyAction = {
  kind: 'document' | 'projects' | 'link'
  label: string
  document?: 'PRD' | 'TRD' | null
  url?: string | null
}

export type ChatApiResponse = {
  conversation_id: string
  response: string
  suggestions?: Suggestion[]
  title?: string | null
  actions?: ReplyAction[]
}

export type ChatRole = 'user' | 'assistant'

export type ChatMessage = {
  id: string
  role: ChatRole
  content: string
  stopped?: boolean
  actions?: ReplyAction[]
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
