import apiClient, { withApiKey } from './client'
import type { SentimentData } from '@/types/sentiment'

export async function triggerSentiment(
  taskId: string
): Promise<{ message: string; status: string; pending_count?: number }> {
  const { data } = await apiClient.post(`/tasks/${taskId}/sentiment`)
  return data
}

export async function getSentiment(taskId: string): Promise<SentimentData> {
  const { data } = await apiClient.get(`/tasks/${taskId}/sentiment`)
  return data
}

/** 全站唯一的导出口：一份文件里含原文、译文和舆情分析结果 */
export function getExportUrl(taskId: string, format: 'xlsx' | 'csv'): string {
  return `/api/v1/tasks/${taskId}/export?format=${format}`
}

export function getSentimentEventsUrl(taskId: string): string {
  return withApiKey(`/api/v1/tasks/${taskId}/sentiment/events`)
}
