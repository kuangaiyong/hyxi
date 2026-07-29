import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { TaskResponse, PlanStep } from '@/types/task'
import type { TimelineEvent, PostData, TaskStats } from '@/types/result'
import * as taskApi from '@/api/tasks'
import * as resultApi from '@/api/results'

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<TaskResponse[]>([])
  const currentTaskId = ref<string | null>(null)
  const currentTask = ref<TaskResponse | null>(null)
  const timeline = ref<TimelineEvent[]>([])
  const logs = ref<TimelineEvent[]>([])
  const isSubmitting = ref(false)
  const isConnected = ref(false)

  // 结果数据
  const posts = ref<PostData[]>([])
  const postsTotal = ref(0)
  const currentPage = ref(1)
  const pageSize = ref(50)
  const stats = ref<TaskStats | null>(null)
  const progressPct = ref(0)
  const progressMsg = ref('')

  const currentTaskStatus = computed(() => currentTask.value?.status || null)
  const isRunning = computed(() =>
    currentTask.value?.status === 'running' || currentTask.value?.status === 'parsing' || currentTask.value?.status === 'pending'
  )
  const isCompleted = computed(() => currentTask.value?.status === 'completed')
  const isFailed = computed(() => currentTask.value?.status === 'failed')

  const recentTasks = computed(() =>
    tasks.value.filter(t => t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled')
  )

  async function submitTask(description: string) {
    isSubmitting.value = true
    try {
      const task = await taskApi.submitTask(description)
      currentTaskId.value = task.id
      currentTask.value = task
      timeline.value = []
      logs.value = []
      posts.value = []
      stats.value = null
      await fetchTasks()
      return task
    } finally {
      isSubmitting.value = false
    }
  }

  async function fetchTasks() {
    try {
      const result = await taskApi.fetchTasks()
      tasks.value = result.tasks
    } catch (e) {
      console.error('获取任务列表失败:', e)
    }
  }

  async function fetchTask(taskId: string) {
    try {
      const task = await taskApi.fetchTask(taskId)
      // 更新 currentTask（匹配 currentTaskId 或者首次加载时 currentTaskId 为空）
      if (!currentTaskId.value || task.id === currentTaskId.value) {
        currentTaskId.value = task.id
        currentTask.value = task
      }
      return task
    } catch (e) {
      console.error('获取任务详情失败:', e)
      return null
    }
  }

  async function cancelCurrentTask() {
    if (!currentTaskId.value) return
    try {
      await taskApi.cancelTask(currentTaskId.value)
      if (currentTask.value) {
        currentTask.value.status = 'cancelled'
      }
      await fetchTasks()
    } catch (e) {
      console.error('取消任务失败:', e)
    }
  }

  async function removeTask(taskId: string) {
    try {
      await taskApi.deleteTask(taskId)
      await fetchTasks()
    } catch (e) {
      console.error('删除任务失败:', e)
    }
  }

  async function retryTask(taskId: string) {
    try {
      const newTask = await taskApi.retryTask(taskId)
      currentTaskId.value = newTask.id
      currentTask.value = newTask
      timeline.value = []
      logs.value = []
      posts.value = []
      stats.value = null
      await fetchTasks()
      return newTask
    } catch (e) {
      console.error('重试任务失败:', e)
      return null
    }
  }

  function addTimelineEvent(event: TimelineEvent) {
    timeline.value.push(event)
    if (event.type === 'log' || event.type === 'error') {
      logs.value.push(event)
    }
  }

  function setConnected(val: boolean) {
    isConnected.value = val
  }

  async function fetchResults(search = '', page = 1) {
    if (!currentTaskId.value) return
    try {
      const [postsData, statsData] = await Promise.all([
        resultApi.fetchPosts(currentTaskId.value, page, pageSize.value, search),
        resultApi.fetchStats(currentTaskId.value),
      ])
      posts.value = postsData.posts
      postsTotal.value = postsData.total
      currentPage.value = page
      stats.value = statsData
    } catch (e) {
      console.error('获取结果失败:', e)
    }
  }

  function getDownloadUrl(): string {
    return currentTaskId.value ? resultApi.getDownloadUrl(currentTaskId.value) : ''
  }

  return {
    tasks,
    currentTaskId,
    currentTask,
    timeline,
    logs,
    isSubmitting,
    isConnected,
    posts,
    postsTotal,
    currentPage,
    pageSize,
    stats,
    progressPct,
    progressMsg,
    currentTaskStatus,
    isRunning,
    isCompleted,
    isFailed,
    recentTasks,
    submitTask,
    fetchTasks,
    fetchTask,
    cancelCurrentTask,
    removeTask,
    retryTask,
    addTimelineEvent,
    setConnected,
    fetchResults,
    getDownloadUrl,
  }
})
