export interface SchedulePreset {
  label: string
  trigger: string
  hours?: number
  cron_expr?: string | null
}

export interface ScheduleHistory {
  task_id: string
  time: string
  status: string
}

export interface ScheduleTask {
  id: string
  description: string
  interval: string
  time: string
  enabled: boolean
  created_at: string
  last_run?: string
  next_run?: string
  history?: ScheduleHistory[]
}
