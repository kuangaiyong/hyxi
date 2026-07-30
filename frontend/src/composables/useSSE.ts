import { ref, onUnmounted, watch, type Ref } from 'vue'
import { useTaskStore } from '@/stores/task'
import type { TimelineEvent } from '@/types/result'

export function useSSE(taskId: Ref<string | null>) {
  const eventSource = ref<EventSource | null>(null)
  const isConnected = ref(false)
  const taskStore = useTaskStore()

  function connect() {
    if (!taskId.value) return
    disconnect()

    const url = `/api/v1/tasks/${taskId.value}/events`
    const es = new EventSource(url)
    eventSource.value = es

    es.onopen = () => {
      isConnected.value = true
      taskStore.setConnected(true)
    }

    es.addEventListener('step_start', (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      taskStore.addTimelineEvent({
        type: 'step_start',
        timestamp: new Date(),
        message: data.message || `开始: ${data.action}`,
        step: data.step,
        action: data.action,
      })
      // 新步骤开始时重置进度
      taskStore.progressPct = 0
      taskStore.progressMsg = data.message || ''
      // 实际步骤开始时刷新任务状态
      if (data.step !== undefined && data.step >= 0) {
        taskStore.fetchTask(taskId.value!)
      }
    })

    es.addEventListener('step_complete', (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      taskStore.addTimelineEvent({
        type: 'step_complete',
        timestamp: new Date(),
        message: data.message || `完成: ${data.action}`,
        step: data.step,
        action: data.action,
      })
      // 意图解析完成后刷新计划
      if (data.step === -1 && data.action === 'parse_intent') {
        taskStore.fetchTask(taskId.value!)
      }
      // 重置进度条
      taskStore.progressPct = 100
    })

    es.addEventListener('step_progress', (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      if (data.progress !== undefined) {
        taskStore.progressPct = Math.round(data.progress * 100)
      }
      if (data.message) {
        taskStore.progressMsg = data.message
        taskStore.addTimelineEvent({
          type: 'log',
          timestamp: new Date(),
          message: data.message,
          level: 'info',
        })
      }
    })

    es.addEventListener('log', (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      taskStore.addTimelineEvent({
        type: 'log',
        timestamp: new Date(),
        message: data.message,
        level: data.level || 'info',
      })
    })

    es.addEventListener('error', (e: MessageEvent) => {
      const data = e.data ? JSON.parse(e.data) : { message: '连接中断' }
      taskStore.addTimelineEvent({
        type: 'error',
        timestamp: new Date(),
        message: data.message || '发生未知错误',
        level: 'error',
      })
    })

    es.addEventListener('task_complete', async (e: MessageEvent) => {
      const data = JSON.parse(e.data)
      if (data.status === 'completed') {
        taskStore.addTimelineEvent({
          type: 'step_complete',
          timestamp: new Date(),
          message: '🎉 任务执行完成！',
          level: 'success',
        })
      } else if (data.status === 'failed') {
        taskStore.addTimelineEvent({
          type: 'error',
          timestamp: new Date(),
          message: `❌ 任务失败: ${data.error || '未知错误'}`,
          level: 'error',
        })
      }
      // currentTask 只有 fetchTask 会写，而 isCompleted / isFailed 全派生于它——
      // 不在断开前刷新，进度页就会永远停在 running，跳转和「查看结果」都不出现
      await taskStore.fetchTask(taskId.value!)
      // 刷新任务列表以更新侧边栏
      await taskStore.fetchTasks()
      disconnect()
    })

    es.onerror = () => {
      isConnected.value = false
      taskStore.setConnected(false)
    }
  }

  function disconnect() {
    if (eventSource.value) {
      eventSource.value.close()
      eventSource.value = null
      isConnected.value = false
      taskStore.setConnected(false)
    }
  }

  // 当 taskId 变化时自动重连
  watch(taskId, (newId) => {
    if (newId) {
      connect()
    } else {
      disconnect()
    }
  }, { immediate: true })

  onUnmounted(() => {
    disconnect()
  })

  return { isConnected, connect, disconnect }
}
