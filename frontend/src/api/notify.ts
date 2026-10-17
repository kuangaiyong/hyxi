import apiClient from './client'
import type { NotifyConfigInput, NotifyConfigPublic, NotifyStatus, NotifyTestResult } from '@/types/notify'

export async function fetchNotifyConfig(): Promise<NotifyConfigPublic> {
  const { data } = await apiClient.get('/notify/config')
  return data
}

export async function saveNotifyConfig(payload: NotifyConfigInput): Promise<NotifyConfigPublic> {
  const { data } = await apiClient.put('/notify/config', payload)
  return data
}

export async function deleteNotifyConfig(): Promise<NotifyConfigPublic> {
  const { data } = await apiClient.delete('/notify/config')
  return data
}

/** 用表单上的（不必先保存的）SMTP 设置发一封测试邮件 */
export async function sendTestMail(payload: NotifyConfigInput): Promise<NotifyTestResult> {
  const { data } = await apiClient.post('/notify/test', payload)
  return data
}

/** 用已保存的配置立即发一轮，后台跑，进度看 fetchNotifyStatus */
export async function sendNow(): Promise<NotifyStatus> {
  const { data } = await apiClient.post('/notify/send')
  return data
}

export async function fetchNotifyStatus(): Promise<NotifyStatus> {
  const { data } = await apiClient.get('/notify/status')
  return data
}
