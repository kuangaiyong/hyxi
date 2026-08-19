import apiClient, { withApiKey } from './client'
import type { SentimentData } from '@/types/sentiment'

/** force=true 忽略 sentiment_at，把所有有正文的帖子按当前口径重跑（会重新花钱） */
export async function triggerSentiment(
  taskId: string,
  force = false
): Promise<{ message: string; status: string; pending_count?: number }> {
  const { data } = await apiClient.post(
    `/tasks/${taskId}/sentiment${force ? '?force=true' : ''}`
  )
  return data
}

export async function getSentiment(taskId: string): Promise<SentimentData> {
  const { data } = await apiClient.get(`/tasks/${taskId}/sentiment`)
  return data
}

/** 全站唯一的导出口：一份文件里含原文、译文和舆情分析结果 */
export function getExportUrl(taskId: string, format: 'xlsx' | 'csv', freshDays?: number): string {
  // 窗口要跟着一起传：页面上看的是 14 天、导出的报告却按 7 天算，两边对不上最难解释
  const extra = freshDays ? `&fresh_days=${freshDays}` : ''
  return `/api/v1/tasks/${taskId}/export?format=${format}${extra}`
}

export function getSentimentEventsUrl(taskId: string): string {
  return withApiKey(`/api/v1/tasks/${taskId}/sentiment/events`)
}
