import apiClient from './client'
import type {
  NotifyConfigInput, NotifyConfigPublic, NotifyPrecheck, NotifyStatus, NotifyTestResult,
} from '@/types/notify'

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

/** 所选的（启用的）数据源里哪些今天还没采集翻译过，「立即发送」前据此弹确认框 */
export async function fetchPrecheck(): Promise<NotifyPrecheck> {
  const { data } = await apiClient.get('/notify/precheck')
  return data
}

/** 用已保存的配置立即发一轮，后台跑，进度看 fetchNotifyStatus。collect：先把今天还没采集翻译过的数据源采一遍 */
export async function sendNow(collect: boolean): Promise<NotifyStatus> {
  const { data } = await apiClient.post('/notify/send', { collect })
  return data
}

export async function fetchNotifyStatus(): Promise<NotifyStatus> {
  const { data } = await apiClient.get('/notify/status')
  return data
}
