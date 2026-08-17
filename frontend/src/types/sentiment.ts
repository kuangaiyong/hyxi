export interface SentimentResult {
  // LLM 解析失败的条目后端会回填 sentiment: null，前端必须能渲染成「未分析」
  sentiment: 'positive' | 'negative' | 'neutral' | null
  intensity: number
  reason_cn: string
  dimensions: string[]
}

export interface SourceBreakdown {
  name: string
  distribution: Record<string, number>
  analyzed: number
  avg_intensity: number
  top_dimensions: [string, number][]
}

export interface SentimentSummary {
  sentiment_distribution: Record<string, number>
  sentiment_percentages: Record<string, number>
  avg_intensity: number
  top_dimensions: [string, number][]
  /** 单来源任务不带这两项 */
  by_source?: Record<string, SourceBreakdown>
  cross_source?: Record<string, Record<string, number>>
}

export interface SentimentData {
  task_id: string
  analyzed_at: number
  total: number
  success: number
  failed: number
  summary: SentimentSummary
  results: (SentimentResult | null)[]
  status?: string
  message?: string
}

export interface PostWithSentiment {
  index: number
  username: string
  content: string
  translation?: string
  /** 相对 data/media 的路径。纯图帖没有正文，不给出图就是一个全空的弹窗 */
  images?: string[]
  /** 多模态读出来的图片内容 —— 纯图帖的结论正是照着它下的 */
  image_desc?: string
  sentiment: SentimentResult | null
}
