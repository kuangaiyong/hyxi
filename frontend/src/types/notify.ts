/** 信任的一张证书：在哪台 SMTP 服务器上读到的 + 它的 SHA-256 指纹，只对这台服务器生效 */
export interface NotifyTrustedCert {
  host: string
  fingerprint: string
}

/** 邮件通知配置（提交）。password 留空 = 沿用已保存的（只在服务器、实际连的端口、「使用 SSL」、用户名都没变、也没新信任证书时） */
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
  /** 信任的证书（公司内网的自签证书；负载均衡后面几台各用一张时就有几张）。新信任一张时要重填密码 */
  trusted_certs: NotifyTrustedCert[]
}

/** 邮件通知配置（读回）。密码只回「有没有」 */
export interface NotifyConfigPublic extends Omit<NotifyConfigInput, 'password' | 'trusted_certs'> {
  /** 可选：8000 上跑着旧版本后端（便携包、改完没重启）时没有这个字段 */
  trusted_certs?: NotifyTrustedCert[]
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
  /** 新主贴条数（没有回复的新主贴也进邮件） */
  root_count: number
  subject: string
  attachment: boolean
  /** 这一轮发送前自动发起的「采集 + 翻译」任务 */
  task_ids: string[]
  /** 撞上没信任过的证书时才有：页面在记录下面给「信任这张证书」 */
  untrusted_cert?: NotifyCertInfo | null
}

export interface NotifyStatus {
  running: boolean
  progress: string
  /** 正在等的那个自动采集任务 */
  task_id: string | null
  last: NotifyRun | null
  next_run: string | null
}

export interface NotifySourceBrief {
  id: string
  name: string
  collector_name: string
}

/** 所选的（启用的）数据源里今天还没采集翻译过、要先采的 */
export interface NotifyPrecheck {
  stale: NotifySourceBrief[]
}

/** 严格校验没过的那张证书 */
export interface NotifyCertInfo {
  /** 在哪台服务器上读到的（小写）：信任它时绑在这台上 */
  host: string
  /** SHA-256，64 位小写十六进制 */
  fingerprint: string
  subject: string
  issuer: string
  not_before: string
  not_after: string
}

export interface NotifyTestResult {
  success: boolean
  message: string
  /** 证书没过校验、也不在信任的证书里时才有 */
  untrusted_cert?: NotifyCertInfo | null
}
