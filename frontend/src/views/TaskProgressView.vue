<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '@/stores/task'
import { useSSE } from '@/composables/useSSE'
import type { TimelineEvent } from '@/types/result'
import type { TaskLog } from '@/types/task'

const route = useRoute()
const router = useRouter()
const taskStore = useTaskStore()

const taskId = computed(() => route.params.id as string)
const { isConnected } = useSSE(taskId)

const logViewer = ref<HTMLElement | null>(null)

// 自动滚动日志
watch(() => taskStore.logs.length, async () => {
  await nextTick()
  if (logViewer.value) {
    logViewer.value.scrollTop = logViewer.value.scrollHeight
  }
})

// 任务完成时跳转到结果页
watch(() => taskStore.isCompleted, (completed) => {
  if (completed && taskStore.currentTaskId) {
    setTimeout(() => {
      router.push(`/tasks/${taskStore.currentTaskId}/results`)
    }, 1500)
  }
})

onMounted(async () => {
  const task = await taskStore.fetchTask(taskId.value)
  if (task) {
    // 从持久化的 plan 重建时间线
    taskStore.timeline = []
    for (const step of task.plan) {
      if (step.status === 'completed') {
        taskStore.timeline.push({
          type: 'step_complete',
          timestamp: new Date(),
          message: `步骤完成: ${step.action}`,
          action: step.action,
        })
      } else if (step.status === 'running') {
        taskStore.timeline.push({
          type: 'step_start',
          timestamp: new Date(),
          message: `正在执行: ${step.action}`,
          action: step.action,
        })
      } else if (step.status === 'failed') {
        taskStore.timeline.push({
          type: 'error',
          timestamp: new Date(),
          message: `步骤失败: ${step.action} - ${step.error || ''}`,
          level: 'error',
        })
      }
    }

    // 从持久化的 logs 恢复日志
    taskStore.logs = (task.logs || []).map((l: TaskLog) => ({
      type: 'log' as const,
      timestamp: new Date(l.time),
      message: l.message,
      level: l.level,
    }))
  }
})

function getStepStatus(idx: number): string {
  const plan = taskStore.currentTask?.plan
  if (!plan || !plan[idx]) return 'pending'
  return plan[idx].status
}

const showCancelConfirm = ref(false)

async function handleCancel() {
  showCancelConfirm.value = false
  await taskStore.cancelCurrentTask()
}
</script>

<template>
  <div>
    <!-- 任务信息 -->
    <div class="card">
      <div class="flex items-center justify-between mb-4">
        <div class="card-header" style="margin-bottom: 0;">
          任务进度
          <span
            class="status-dot"
            :class="isConnected ? 'connected' : 'disconnected'"
            :title="isConnected ? '已连接' : '未连接'"
          ></span>
        </div>
        <div class="flex gap-2 items-center">
          <span
            v-if="taskStore.currentTask"
            class="badge"
            :class="'badge-' + taskStore.currentTask.status"
          >
            {{ taskStore.currentTask.status }}
          </span>
          <button
            v-if="taskStore.isRunning"
            class="btn btn-outline btn-sm"
            @click="showCancelConfirm = true"
          >
            取消
          </button>
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-primary btn-sm"
            @click="router.push(`/tasks/${taskId}/results`)"
          >
            查看结果 →
          </button>
        </div>
      </div>

      <p class="text-sm text-secondary mb-4">
        {{ taskStore.currentTask?.description || '加载中...' }}
      </p>

      <!-- 进度条 -->
      <div v-if="taskStore.isRunning && taskStore.progressPct > 0" style="margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 13px;">
          <span class="text-secondary">{{ taskStore.progressMsg || '执行中...' }}</span>
          <span style="font-weight: 600;">{{ taskStore.progressPct }}%</span>
        </div>
        <div style="background: var(--border-light); border-radius: 6px; height: 8px; overflow: hidden;">
          <div
            :style="{
              width: taskStore.progressPct + '%',
              height: '100%',
              background: 'linear-gradient(90deg, #3B82F6, #6366F1)',
              borderRadius: '6px',
              transition: 'width 0.5s',
            }"
          ></div>
        </div>
      </div>

      <!-- 步骤时间线 -->
      <div v-if="taskStore.currentTask?.plan?.length" class="timeline">
        <div
          v-for="(step, idx) in taskStore.currentTask.plan"
          :key="idx"
          class="timeline-item"
        >
          <div
            class="timeline-dot"
            :class="getStepStatus(idx)"
          ></div>
          <div class="timeline-content">
            <div class="flex items-center gap-2">
              <strong>{{ idx + 1 }}. {{ step.action }}</strong>
              <span class="badge" :class="'badge-' + getStepStatus(idx)">
                {{ getStepStatus(idx) }}
              </span>
            </div>
            <div v-if="step.error" class="text-sm text-error mt-1">
              {{ step.error }}
            </div>
          </div>
        </div>
      </div>

      <!-- 等待中状态 -->
      <div v-if="!taskStore.currentTask?.plan?.length && taskStore.isRunning" class="text-center text-secondary" style="padding: 20px;">
        <span class="spinner spinner-lg"></span>
        <p class="mt-4">等待任务开始...</p>
      </div>
    </div>

    <!-- 实时日志 -->
    <div class="card">
      <div class="card-header">📋 实时日志</div>
      <div ref="logViewer" class="log-viewer">
        <div v-if="!taskStore.logs.length" class="log-line info">
          等待日志输出...
        </div>
        <div
          v-for="(log, idx) in taskStore.logs"
          :key="idx"
          class="log-line"
          :class="log.level || 'info'"
        >
          <span style="opacity: 0.5; margin-right: 8px;">
            {{ log.timestamp.toLocaleTimeString('zh-CN', { hour12: false }) }}
          </span>
          {{ log.message }}
        </div>
      </div>
    </div>

    <!-- 取消确认弹窗 -->
    <div v-if="showCancelConfirm" class="modal-overlay" @click.self="showCancelConfirm = false">
      <div class="modal-content" style="max-width: 400px;">
        <div class="modal-header">
          <h3>确认取消</h3>
        </div>
        <p class="mb-4">确定要取消当前正在执行的任务吗？</p>
        <div class="flex gap-2" style="justify-content: flex-end;">
          <button class="btn btn-outline" @click="showCancelConfirm = false">继续执行</button>
          <button class="btn btn-danger" @click="handleCancel">确认取消</button>
        </div>
      </div>
    </div>
  </div>
</template>
