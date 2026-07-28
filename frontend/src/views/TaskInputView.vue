<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import { useTaskStore } from '@/stores/task'

const router = useRouter()
const configStore = useConfigStore()
const taskStore = useTaskStore()

const description = ref('')
const error = ref('')

const quickActions = [
  {
    label: '抓取 + 翻译 + Excel',
    icon: '📊',
    text: '抓取帖子2336074所有页面，翻译成中文，导出Excel报告',
  },
  {
    label: '仅抓取',
    icon: '🕷️',
    text: '抓取帖子2336074的所有内容，保存为JSON',
  },
  {
    label: '仅翻译已有数据',
    icon: '🌐',
    text: '把已抓取的JSON数据翻译成中文，生成Excel',
  },
]

function selectQuick(text: string) {
  description.value = text
}

async function handleSubmit() {
  error.value = ''

  if (!description.value.trim()) {
    error.value = '请输入任务描述'
    return
  }

  if (!configStore.isConfigured) {
    error.value = '请先配置 LLM API'
    router.push('/config')
    return
  }

  const task = await taskStore.submitTask(description.value)
  if (task) {
    router.push(`/tasks/${task.id}/progress`)
  } else {
    error.value = '创建任务失败，请重试'
  }
}
</script>

<template>
  <div style="max-width: 700px;">
    <div class="card">
      <div class="card-header">🚀 新建抓取任务</div>
      <p class="text-secondary text-sm mb-4">
        用自然语言描述您想要执行的任务，AI 将自动解析并执行。
      </p>

      <div class="form-group">
        <label class="form-label">任务描述</label>
        <textarea
          v-model="description"
          class="form-input"
          rows="4"
          placeholder="例如：抓取帖子2336074的所有内容，翻译成中文，导出Excel报告"
        ></textarea>
      </div>

      <div
        v-if="error"
        class="mb-4"
        style="padding: 10px 12px; border-radius: 8px; font-size: 13px; background: #FEE2E2; color: #DC2626;"
      >
        ❌ {{ error }}
      </div>

      <button
        class="btn btn-primary btn-lg"
        :disabled="taskStore.isSubmitting || !description.trim()"
        @click="handleSubmit"
      >
        <span v-if="taskStore.isSubmitting" class="spinner"></span>
        {{ taskStore.isSubmitting ? '提交中...' : '🚀 开始执行' }}
      </button>
    </div>

    <!-- 快捷操作 -->
    <div class="card">
      <div class="card-header">⚡ 快捷任务</div>
      <div class="flex gap-2" style="flex-wrap: wrap;">
        <button
          v-for="qa in quickActions"
          :key="qa.label"
          class="btn btn-outline"
          @click="selectQuick(qa.text)"
        >
          {{ qa.icon }} {{ qa.label }}
        </button>
      </div>
    </div>

    <!-- 未配置提示 -->
    <div
      v-if="!configStore.isConfigured"
      class="card"
      style="background: #FEF3C7; border-color: #F59E0B;"
    >
      <div class="flex items-center gap-2">
        <span style="font-size: 20px;">⚠️</span>
        <div>
          <strong>尚未配置 LLM API</strong>
          <p class="text-sm text-secondary mt-1">
            请先前往 <router-link to="/config">LLM 配置页面</router-link> 设置 API Key。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>
