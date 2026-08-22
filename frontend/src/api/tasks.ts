import apiClient from './client'
import type { TaskResponse, TaskListResponse, TaskCreateRequest } from '@/types/task'

export async function submitTask(description: string): Promise<TaskResponse> {
  const { data } = await apiClient.post('/tasks', { description })
  return data
}

export async function fetchTasks(): Promise<TaskListResponse> {
  const { data } = await apiClient.get('/tasks')
  return data
}

export async function fetchTask(taskId: string): Promise<TaskResponse> {
  const { data } = await apiClient.get(`/tasks/${taskId}`)
  return data
}

export async function cancelTask(taskId: string): Promise<void> {
  await apiClient.delete(`/tasks/${taskId}`)
}

export async function deleteTask(taskId: string): Promise<void> {
  await apiClient.delete(`/tasks/${taskId}?force=true`)
}

/** full=true 为全量重跑：忽略全部增量标记，会重新消耗大模型调用 */
export async function retryTask(taskId: string, full = false): Promise<TaskResponse> {
  const { data } = await apiClient.post(`/tasks/${taskId}/retry`, null, {
    params: full ? { full: true } : {},
  })
  return data
}
