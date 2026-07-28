import apiClient from './client'
import type { ScheduleTask, SchedulePreset } from '@/types/schedule'

export async function fetchSchedules(): Promise<{ schedules: ScheduleTask[] }> {
  const { data } = await apiClient.get('/schedules')
  return data
}

export async function fetchPresets(): Promise<{ presets: Record<string, SchedulePreset> }> {
  const { data } = await apiClient.get('/schedules/presets')
  return data
}

export async function createSchedule(params: {
  description: string
  interval: string
  time: string
}): Promise<ScheduleTask> {
  const { data } = await apiClient.post('/schedules', params)
  return data
}

export async function deleteSchedule(id: string): Promise<void> {
  await apiClient.delete(`/schedules/${id}`)
}

export async function toggleSchedule(id: string): Promise<ScheduleTask> {
  const { data } = await apiClient.post(`/schedules/${id}/toggle`)
  return data
}

export async function runScheduleNow(id: string): Promise<{ message: string; task_id: string }> {
  const { data } = await apiClient.post(`/schedules/${id}/run`)
  return data
}
