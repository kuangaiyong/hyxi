import apiClient from './client'
import type { PostsResponse, PostData, TaskStats } from '@/types/result'

export async function fetchPosts(
  taskId: string,
  page = 1,
  pageSize = 50
): Promise<PostsResponse> {
  const { data } = await apiClient.get(`/tasks/${taskId}/posts`, {
    params: { page, page_size: pageSize },
  })
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

export function getDownloadUrl(taskId: string): string {
  return `/api/v1/tasks/${taskId}/download`
}
