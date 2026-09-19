export interface QuoteData {
  /** 被引用楼层的 id（原站就写在引用块的链接上）；引用里没有链接时是空串 */
  message_id: string
  /** 引用行原文（荷兰语）。被引用楼层没采到时直接显示它 —— 前端不解析荷兰语日期 */
  cite: string
  username: string
  timestamp: string
  content: string
  /** 被引用楼层的译文。只有解析到楼层时才有 —— 快照兜底那一段没送过翻译 */
  translation: string
  /** 原站把长引用**服务端**截断成 [...]（点「toon volledige bericht」文本不变，实测）。
      只有快照兜底时才会为真；解析到楼层时拿到的是全文 */
  truncated: boolean
  /** 引用块里的图，相对 data/media 的路径。**不是引用者的配图**（那是 post.images） */
  images: string[]
  /** 被引用的楼层在本任务里找得到吗 */
  resolved: boolean
  /** 找得到时：它在扁平数组里的绝对位置，与列表里的 #序号 是同一个编号 */
  index: number | null
}

export interface PostData {
  index: number
  username: string
  timestamp: string
  content: string
  translation: string
  page_number: number
  source: string
  source_name: string
  /**
   * 原帖固定链接，**只有主贴有**，回复贴一律空串。后端现算（group_id + message_id），
   * 来源被删掉或该来源没有 URL 形态时也是空串 —— 按空串决定不渲染即可，
   * 前端不判断来源类型。
   */
  source_url: string
  reply_level: number
  /**
   * 这个来源装的是什么：
   *  · `thread` 一个来源 = **一个讨论串**：一条主题 + 按时间平铺的回复（Tweakers 论坛）
   *  · `feed`   一个来源 = 许多主贴，每条主贴带自己的评论与回复（Facebook 小组）
   * 只用来选措辞（主题/回复 还是 主贴/评论与回复）。**不要按 source 名字或 collector
   * 判断来源类型** —— 站点知识在采集器声明里，这里只认这个语义值。
   */
  thread_kind: 'thread' | 'feed'
  /** 搜索命中标记：命中评论时父贴会被一起带出来，靠这个区分谁才是命中项 */
  matched: boolean
  /** 正文图，相对 data/media 的路径；渲染时拼成 /api/v1/media/<path>?api_key=… */
  images: string[]
  /** 多模态模型读出来的图片内容。纯图帖的全部信息都在这里 */
  image_desc: string
  /**
   * 这条楼层引用了哪些内容；没有引用就是空数组。
   * **一条楼层可以引多人**（原站支持多引用），所以是数组 —— 只渲染第一条就是静默丢内容。
   * **永远不在 content 里** —— 那是指纹的一部分（改了历史数据全部失配）
   */
  quotes: QuoteData[]
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
  /**
   * 主贴专用：原帖上显示的评论数（含回复的回复），采集时没读到就是 null。
   * 和子树条数比，对不上标「已采 X · 原帖 Y」
   */
  site_comment_count: number | null
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
  /** 还缺译文的条数：有正文、译文为空或是「[翻译…失败]」标记。提示条上的 N 就是它 */
  untranslated_count: number
  /** 这个任务的来源正在被补译（也可能是共用来源的别的任务在翻）—— 进页面就得显示「正在补译」 */
  translating: boolean
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
