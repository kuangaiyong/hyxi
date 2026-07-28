export type TaskStatus = 'pending' | 'parsing' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface PlanStep {
  action: string
  params: Record<string, any>
  status: string
  error?: string
}

export interface TaskLog {
  time: string
  level: string
  message: string
}

export interface TaskResponse {
  id: string
  status: TaskStatus
  description: string
  plan: PlanStep[]
  logs: TaskLog[]
  progress: number
  current_step: string | null
  result: Record<string, any> | null
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface TaskListResponse {
  tasks: TaskResponse[]
  total: number
}

export interface TaskCreateRequest {
  description: string
}
