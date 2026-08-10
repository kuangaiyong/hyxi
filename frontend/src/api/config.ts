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

/** 多模态（图片理解）模型。可选配置，没配时舆情分析按纯文本进行 */

export async function fetchVisionConfig(): Promise<LLMConfigPublic> {
  const { data } = await apiClient.get('/config/vision')
  return data
}

export async function saveVisionConfig(config: LLMConfig): Promise<LLMConfigPublic> {
  const { data } = await apiClient.post('/config/vision', config)
  return data
}

export async function testVisionConnection(config: LLMConfig): Promise<ConfigTestResult> {
  const { data } = await apiClient.post('/config/vision/test', config)
  return data
}

export async function resetVisionConfig(): Promise<LLMConfigPublic> {
  const { data } = await apiClient.delete('/config/vision')
  return data
}
