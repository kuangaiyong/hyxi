export interface PostData {
  index: number
  username: string
  timestamp: string
  content: string
  translation: string
  page_number: number
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
