import { ref } from 'vue'

export interface Toast {
  id: number
  message: string
  type: 'success' | 'error' | 'warning' | 'info'
}

const toasts = ref<Toast[]>([])
let nextId = 0

export function useToast() {
  function add(message: string, type: Toast['type'] = 'info', duration = 4000) {
    const id = nextId++
    toasts.value.push({ id, message, type })
    if (duration > 0) {
      setTimeout(() => remove(id), duration)
    }
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
