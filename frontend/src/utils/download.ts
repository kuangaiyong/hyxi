import { getApiKey } from '@/api/client'

function filenameFromDisposition(disposition: string | null): string {
  if (!disposition) return ''
  const utf8 = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8) return decodeURIComponent(utf8[1])
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain ? plain[1] : ''
}

/**
 * 裸 <a download> 会把 404/500 的 JSON 错误体当成正常内容存盘，
 * 用户拿到的是一个名为 .csv/.xlsx 的损坏文件却毫无提示。
 */
export async function downloadFile(url: string, fallbackName: string): Promise<void> {
  const key = getApiKey()
  const resp = await fetch(url, { headers: key ? { 'X-API-Key': key } : {} })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail || ''
    } catch {
      /* 错误体不是 JSON */
    }
    throw new Error(detail || `服务端返回 HTTP ${resp.status}`)
  }
  const blob = await resp.blob()
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filenameFromDisposition(resp.headers.get('content-disposition')) || fallbackName
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(objectUrl)
}
