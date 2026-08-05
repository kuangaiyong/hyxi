import apiClient from './client'
import type { PostsResponse, PostData, TaskStats } from '@/types/result'

export async function fetchPosts(
  taskId: string,
  page = 1,
  pageSize = 50,
  search = ''
): Promise<PostsResponse> {
  const params: Record<string, string | number> = { page, page_size: pageSize }
  if (search) params.search = search
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
