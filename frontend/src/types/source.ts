export interface ParamField {
  name: string
  label: string
  type: string
  required?: boolean
  placeholder?: string
}

export interface CollectorInfo {
  id: string
  display_name: string
  needs_credentials: boolean
  incremental_strategy: string
  param_fields: ParamField[]
}

export interface SourcePublic {
  id: string
  collector_id: string
  collector_name: string
  name: string
  params: Record<string, any>
  enabled: boolean
  needs_credentials: boolean
  has_credential: boolean
  credential_username: string
  last_auth_at: string | null
  created_at: string | null
}

export interface SourceCreate {
  collector_id: string
  name: string
  params: Record<string, any>
  enabled?: boolean
}

export interface SourceUpdate {
  name?: string
  params?: Record<string, any>
  enabled?: boolean
}

export interface CredentialInput {
  username: string
  password: string
}
