import apiClient from './client'
import type { LLMConfig, LLMConfigPublic, ConfigTestResult } from '@/types/config'

export async function fetchConfig(): Promise<LLMConfigPublic> {
  const { data } = await apiClient.get('/config')
  return data
}

export async function saveConfig(config: LLMConfig): Promise<LLMConfigPublic> {
  const { data } = await apiClient.post('/config', config)
  return data
}

export async function testConnection(config: LLMConfig): Promise<ConfigTestResult> {
  const { data } = await apiClient.post('/config/test', config)
  return data
}

export async function resetConfig(): Promise<LLMConfigPublic> {
  const { data } = await apiClient.delete('/config')
  return data
}
