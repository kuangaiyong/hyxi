import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { LLMConfig, LLMConfigPublic } from '@/types/config'
import * as configApi from '@/api/config'

export const useConfigStore = defineStore('config', () => {
  const baseUrl = ref('https://api.deepseek.com')
  const modelName = ref('deepseek-chat')
  const isConfigured = ref(false)
  const isTesting = ref(false)
  const testResult = ref<'success' | 'failure' | null>(null)
  const testMessage = ref('')

  const hasConfig = computed(() => isConfigured.value)

  async function fetchConfig() {
    try {
      const config = await configApi.fetchConfig()
      baseUrl.value = config.base_url
      modelName.value = config.model_name
      isConfigured.value = config.is_configured
    } catch (e) {
      console.error('获取配置失败:', e)
    }
  }

  async function saveConfig(apiKey: string) {
    try {
      const config: LLMConfig = {
        api_key: apiKey,
        base_url: baseUrl.value,
        model_name: modelName.value,
      }
      const result = await configApi.saveConfig(config)
      isConfigured.value = result.is_configured
      return true
    } catch (e) {
      console.error('保存配置失败:', e)
      return false
    }
  }

  async function testConnection(apiKey: string) {
    isTesting.value = true
    testResult.value = null
    try {
      const config: LLMConfig = {
        api_key: apiKey,
        base_url: baseUrl.value,
        model_name: modelName.value,
      }
      const result = await configApi.testConnection(config)
      testResult.value = result.success ? 'success' : 'failure'
      testMessage.value = result.message
    } catch (e: any) {
      testResult.value = 'failure'
      testMessage.value = e?.response?.data?.message || '连接测试异常'
    } finally {
      isTesting.value = false
    }
  }

  async function resetConfig() {
    try {
      const result = await configApi.resetConfig()
      baseUrl.value = result.base_url
      modelName.value = result.model_name
      isConfigured.value = false
      testResult.value = null
    } catch (e) {
      console.error('重置配置失败:', e)
    }
  }

  return {
    baseUrl,
    modelName,
    isConfigured,
    isTesting,
    testResult,
    testMessage,
    hasConfig,
    fetchConfig,
    saveConfig,
    testConnection,
    resetConfig,
  }
})
