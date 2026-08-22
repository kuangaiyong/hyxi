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
  /** 这一轮是不是全量重跑（忽略全部增量标记，重新采集/翻译/下图/分析） */
  force_full: boolean
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
