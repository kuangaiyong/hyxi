export interface PostData {
  index: number
  username: string
  timestamp: string
  content: string
  translation: string
  page_number: number
  source: string
  source_name: string
  reply_level: number
  /** 搜索命中标记：命中评论时父贴会被一起带出来，靠这个区分谁才是命中项 */
  matched: boolean
  /** 正文图，相对 data/media 的路径；渲染时拼成 /api/v1/media/<path>?api_key=… */
  images: string[]
  /** 多模态模型读出来的图片内容。纯图帖的全部信息都在这里 */
  image_desc: string
  /**
   * 「老主贴上的新回复」：这条回复发在近 N 天内，而它所属主贴早于 N 天。
   * 列表按主贴时间从新到旧排、评论跟着主贴走，所以这类回复会被排到很后面 ——
   * 真实数据里有一条今天的回复挂在两个月前的主贴上，排在第 40 多个主贴之后。
   */
  fresh_reply: boolean
  /** 距其主贴的天数，只在 fresh_reply 为真时有意义 */
  days_since_root: number
  /** 主贴专用：整棵子树里有几条这样的新回复，用来做徽标 */
  fresh_reply_count: number
  replies: PostData[]
}

export interface PostsResponse {
  posts: PostData[]
  total: number
  page: number
  page_size: number
}

export interface TaskStats {
  total_posts: number
  unique_users: number
  total_pages: number
  time_range_start: string | null
  time_range_end: string | null
  top_users: { username: string; count: number }[]
}

export interface SSEEvent {
  event: string
  data: Record<string, any>
}

export interface TimelineEvent {
  type: 'step_start' | 'step_complete' | 'log' | 'error'
  timestamp: Date
  message: string
  step?: number
  action?: string
  level?: string
}
