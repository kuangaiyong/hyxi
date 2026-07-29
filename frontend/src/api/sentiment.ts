import apiClient from './client'
import type { SentimentData } from '@/types/sentiment'

export async function triggerSentiment(taskId: string): Promise<{ message: string; status: string }> {
  const { data } = await apiClient.post(`/tasks/${taskId}/sentiment`)
  return data
}

export async function getSentiment(taskId: string): Promise<SentimentData> {
  const { data } = await apiClient.get(`/tasks/${taskId}/sentiment`)
  return data
}

export function getSentimentDownloadUrl(taskId: string): string {
  return `/api/v1/tasks/${taskId}/sentiment/download`
}

export function getSentimentEventsUrl(taskId: string): string {
  return `/api/v1/tasks/${taskId}/sentiment/events`
}
