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

// 401 提示的 id，null 表示眼下没在提示。它是常驻的（不自动消失），所以**鉴权一恢复
// 就得主动撤掉**：否则用户照着提示填对了密钥，右上角那条红色「访问被拒绝」还一直挂着，
// 看不出自己做对没有（用户实测反馈过）。/api/v1/* 全部受保护，任何一次成功的响应都
// 说明密钥已经对了
let unauthorizedToast: number | null = null

apiClient.interceptors.response.use(
  (response) => {
    if (unauthorizedToast !== null) {
      useToast().remove(unauthorizedToast)
      unauthorizedToast = null
    }
    return response
  },
  (error) => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    if (error.response?.status === 401 && unauthorizedToast === null) {
      // 各页面都把请求失败渲染成空状态，不提示的话用户看到的是「暂无数据」而非「缺密钥」。
      // 同一页往往并发多个请求，只提示一次；不自动消失，点击关闭。
      // 提示里的名字必须和 LLM 配置页那个框的标签**逐字一致** —— 用户是照着这几个字去找的
      unauthorizedToast = useToast().add(
        '访问被拒绝：请在「LLM 配置」页填写与后端一致的服务访问密钥', 'error', 0)
    }
    console.error('[API Error]', message)
    return Promise.reject(error)
  }
)

export default apiClient
