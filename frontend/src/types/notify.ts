/** 邮件通知配置（提交）。password 留空 = 沿用已保存的（只在 SMTP 服务器和用户名都没改时） */
export interface NotifyConfigInput {
  recipients: string[]
  source_ids: string[]
  /** 新回复时间窗口（天）；null = 不设置，按近 1 天 */
  days: number | null
  schedule_enabled: boolean
  schedule_time: string
  smtp_host: string
  smtp_port: number
  smtp_ssl_port: number
  use_ssl: boolean
  sender: string
  username: string
  password: string
}

/** 邮件通知配置（读回）。密码只回「有没有」 */
export interface NotifyConfigPublic extends Omit<NotifyConfigInput, 'password'> {
  has_password: boolean
  is_configured: boolean
}

export interface NotifyRun {
  trigger: 'manual' | 'schedule'
  outcome: 'sent' | 'skipped' | 'failed'
  message: string
  started_at: string | null
  finished_at: string | null
  reply_count: number
  subject: string
  attachment: boolean
}

export interface NotifyStatus {
  running: boolean
  progress: string
  last: NotifyRun | null
  next_run: string | null
}

export interface NotifyTestResult {
  success: boolean
  message: string
}
