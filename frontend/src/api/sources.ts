import apiClient, { withApiKey } from './client'
import type {
  CollectorInfo,
  CredentialInput,
  SourceCreate,
  SourcePublic,
  SourceUpdate,
} from '@/types/source'

export async function fetchCollectors(): Promise<CollectorInfo[]> {
  const { data } = await apiClient.get('/collectors')
  return data
}

export async function fetchSources(): Promise<SourcePublic[]> {
  const { data } = await apiClient.get('/sources')
  return data
}

export async function createSource(payload: SourceCreate): Promise<SourcePublic> {
  const { data } = await apiClient.post('/sources', payload)
  return data
}

export async function updateSource(id: string, payload: SourceUpdate): Promise<SourcePublic> {
  const { data } = await apiClient.patch(`/sources/${id}`, payload)
  return data
}

export async function deleteSource(id: string): Promise<void> {
  await apiClient.delete(`/sources/${id}`)
}

export async function setCredential(id: string, payload: CredentialInput): Promise<SourcePublic> {
  const { data } = await apiClient.put(`/sources/${id}/credential`, payload)
  return data
}

export async function deleteCredential(id: string): Promise<SourcePublic> {
  const { data } = await apiClient.delete(`/sources/${id}/credential`)
  return data
}

export async function authorizeSource(id: string): Promise<{ message: string; channel: string }> {
  const { data } = await apiClient.post(`/sources/${id}/authorize`)
  return data
}

/** 人工授权的进度流。EventSource 不能自定义请求头，密钥只能挂 query 上 */
export function authorizeEventsUrl(id: string): string {
  return withApiKey(`/api/v1/sources/${id}/authorize/events`)
}
