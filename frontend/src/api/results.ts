import apiClient from './client'
import type { PostsResponse, PostData, TaskStats } from '@/types/result'

export async function fetchPosts(
  taskId: string,
  page = 1,
  pageSize = 50,
  search = '',
  freshDays?: number
): Promise<PostsResponse> {
  const params: Record<string, string | number> = { page, page_size: pageSize }
  if (search) params.search = search
  // 「老帖新回复」的时间窗口。后端只接受 3/7/14，传别的会 400 ——
  // 那是有意的：这个数字会进报告文案，静默回落到默认值会让用户以为自己换了窗口
  if (freshDays) params.fresh_days = freshDays
  const { data } = await apiClient.get(`/tasks/${taskId}/posts`, { params })
  return data
}

export async function fetchPostDetail(taskId: string, index: number): Promise<PostData> {
  const { data } = await apiClient.get(`/tasks/${taskId}/posts/${index}`)
  return data
}

export async function fetchStats(taskId: string): Promise<TaskStats> {
  const { data } = await apiClient.get(`/tasks/${taskId}/stats`)
  return data
}
