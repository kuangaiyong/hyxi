export interface SentimentResult {
  sentiment: 'positive' | 'negative' | 'neutral'
  intensity: number
  reason_cn: string
  dimensions: string[]
}

export interface SentimentSummary {
  sentiment_distribution: Record<string, number>
  sentiment_percentages: Record<string, number>
  avg_intensity: number
  top_dimensions: [string, number][]
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
  sentiment: SentimentResult | null
}
