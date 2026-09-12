import { ref } from 'vue'

export interface Toast {
  id: number
  message: string
  type: 'success' | 'error' | 'warning' | 'info'
}

const toasts = ref<Toast[]>([])
let nextId = 0

export function useToast() {
  /** 返回 id：常驻提示（duration 0）得靠它在条件解除时主动撤掉，见 api/client.ts 的 401 */
  function add(message: string, type: Toast['type'] = 'info', duration = 4000): number {
    const id = nextId++
    toasts.value.push({ id, message, type })
    if (duration > 0) {
      setTimeout(() => remove(id), duration)
    }
    return id
  }

  function remove(id: number) {
    toasts.value = toasts.value.filter(t => t.id !== id)
  }

  function success(msg: string) { add(msg, 'success') }
  function error(msg: string) { add(msg, 'error', 6000) }
  function warning(msg: string) { add(msg, 'warning', 5000) }
  function info(msg: string) { add(msg, 'info') }

  return { toasts, add, remove, success, error, warning, info }
}
