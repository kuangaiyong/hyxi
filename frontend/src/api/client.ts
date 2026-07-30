import axios from 'axios'
import { useToast } from '@/composables/useToast'

const API_KEY_STORAGE = 'hyxi_api_key'

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) || ''
}

export function setApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key)
  else localStorage.removeItem(API_KEY_STORAGE)
}

/** 浏览器的 EventSource 不能自定义请求头，SSE 只能把密钥挂在 query 上 */
export function withApiKey(url: string): string {
  const key = getApiKey()
  if (!key) return url
  return `${url}${url.includes('?') ? '&' : '?'}api_key=${encodeURIComponent(key)}`
}

const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
})

apiClient.interceptors.request.use((config) => {
  const key = getApiKey()
  if (key) config.headers.set('X-API-Key', key)
  return config
})

let unauthorizedNotified = false

apiClient.interceptors.response.use(
  (response) => {
    unauthorizedNotified = false
    return response
  },
  (error) => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    if (error.response?.status === 401 && !unauthorizedNotified) {
      // 各页面都把请求失败渲染成空状态，不提示的话用户看到的是「暂无数据」而非「缺密钥」。
      // 同一页往往并发多个请求，只提示一次；不自动消失，点击关闭。
      unauthorizedNotified = true
      useToast().add('访问被拒绝：请在「LLM 配置」页填写与后端一致的服务访问密钥', 'error', 0)
    }
    console.error('[API Error]', message)
    return Promise.reject(error)
  }
)

export default apiClient
