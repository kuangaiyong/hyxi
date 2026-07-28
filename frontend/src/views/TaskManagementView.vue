<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import { useTaskStore } from '@/stores/task'
import type { TaskStatus } from '@/types/task'

const router = useRouter()
const configStore = useConfigStore()
const taskStore = useTaskStore()

// 新建任务
const description = ref('')
const createError = ref('')
const createExpanded = ref(true)

// 过滤器
const filterStatus = ref('')
const filterKeyword = ref('')
const confirmDelete = ref<string | null>(null)

const quickActions = [
  { label: '抓取+翻译+舆情', icon: '📊', text: '抓取帖子2336074所有页面，翻译成中文，导出Excel，分析舆情' },
  { label: '仅抓取+翻译', icon: '🔄', text: '抓取帖子2336074的所有内容，翻译成中文，导出Excel报告' },
  { label: '翻译已有数据', icon: '🌐', text: '翻译已有的2336074数据，生成Excel' },
]

onMounted(() => {
  taskStore.fetchTasks()
})

// 过滤后的任务列表
const filteredTasks = computed(() => {
  let tasks = taskStore.tasks
  if (filterStatus.value) {
    tasks = tasks.filter(t => t.status === filterStatus.value)
  }
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    tasks = tasks.filter(t =>
      t.description.toLowerCase().includes(kw) ||
      t.id.includes(kw)
    )
  }
  return tasks
})

function statusLabel(s: TaskStatus): string {
  const m: Record<string, string> = {
    pending: '等待中', parsing: '解析中', running: '执行中',
    completed: '已完成', failed: '失败', cancelled: '已取消',
  }
  return m[s] || s
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${y}-${m}-${dd} ${hh}:${mm}`
}

function shortDesc(d: string): string {
  return d.length > 35 ? d.slice(0, 35) + '...' : d
}

function extractThreadId(desc: string): string {
  const m = desc.match(/(\d{5,})/)
  return m ? m[1] : '-'
}

async function handleSubmit() {
  createError.value = ''
  if (!description.value.trim()) {
    createError.value = '请输入任务描述'
    return
  }
  if (!configStore.isConfigured) {
    router.push('/config')
    return
  }
  const task = await taskStore.submitTask(description.value)
  if (task) {
    description.value = ''
    createExpanded.value = false
    router.push(`/tasks/${task.id}/progress`)
  } else {
    createError.value = '创建任务失败'
  }
}

function selectQuick(text: string) {
  description.value = text
}

function viewTask(taskId: string, status: string) {
  if (status === 'completed' || status === 'failed' || status === 'cancelled') {
    router.push(`/tasks/${taskId}/results`)
  } else {
    router.push(`/tasks/${taskId}/progress`)
  }
}

async function deleteTask(taskId: string) {
  await taskStore.removeTask(taskId)
  confirmDelete.value = null
}
</script>

<template>
  <div style="max-width: 1100px;">
    <!-- 新建任务区 -->
    <div class="card">
      <div
        class="card-header"
        style="cursor: pointer; user-select: none;"
        @click="createExpanded = !createExpanded"
      >
        🚀 新建任务
        <span style="margin-left: auto; font-size: 12px; color: var(--text-light);">
          {{ createExpanded ? '收起 ▴' : '展开 ▾' }}
        </span>
      </div>

      <div v-if="createExpanded">
        <div class="form-group">
          <label class="form-label">任务描述（自然语言）</label>
          <textarea
            v-model="description"
            class="form-input"
            rows="3"
            placeholder="例如：抓取帖子2336074的所有内容，翻译成中文，导出Excel报告"
          ></textarea>
        </div>

        <div class="flex gap-2 mb-4" style="flex-wrap: wrap;">
          <button
            v-for="qa in quickActions"
            :key="qa.label"
            class="btn btn-outline btn-sm"
            @click="selectQuick(qa.text)"
          >{{ qa.icon }} {{ qa.label }}</button>
        </div>

        <div v-if="createError" class="mb-4" style="padding: 8px 12px; border-radius: 6px; font-size: 13px; background: #FEE2E2; color: #DC2626;">
          ❌ {{ createError }}
        </div>

        <div v-if="!configStore.isConfigured" class="mb-4" style="padding: 8px 12px; border-radius: 6px; font-size: 13px; background: #FEF3C7;">
          ⚠️ 尚未配置 LLM API，请先前往 <router-link to="/config">LLM 配置</router-link>
        </div>

        <button
          class="btn btn-primary"
          :disabled="taskStore.isSubmitting || !description.trim()"
          @click="handleSubmit"
        >
          <span v-if="taskStore.isSubmitting" class="spinner"></span>
          {{ taskStore.isSubmitting ? '提交中...' : '🚀 开始执行' }}
        </button>
      </div>
    </div>

    <!-- 过滤器 -->
    <div class="card" style="padding: 12px 20px;">
      <div class="flex items-center gap-4" style="flex-wrap: wrap;">
        <span class="text-sm" style="font-weight: 600;">📜 历史任务</span>

        <select v-model="filterStatus" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部状态</option>
          <option value="completed">已完成</option>
          <option value="running">执行中</option>
          <option value="failed">失败</option>
          <option value="cancelled">已取消</option>
        </select>

        <input
          v-model="filterKeyword"
          type="text"
          class="form-input"
          placeholder="搜索任务描述..."
          style="width: 200px; padding: 4px 8px; font-size: 12px;"
        />

        <button
          v-if="filterStatus || filterKeyword"
          class="btn btn-outline btn-sm"
          @click="filterStatus = ''; filterKeyword = ''"
        >清除</button>

        <span class="text-sm text-secondary" style="margin-left: auto;">
          共 {{ filteredTasks.length }} 条
        </span>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="card" style="padding: 0; overflow-x: auto;">
      <table class="data-table" v-if="filteredTasks.length">
        <thead>
          <tr>
            <th style="width: 44px; text-align: center;">#</th>
            <th>任务描述</th>
            <th style="width: 80px; text-align: center;">状态</th>
            <th style="width: 72px; text-align: center;">帖子</th>
            <th style="width: 140px; text-align: center;">时间</th>
            <th style="width: 80px; text-align: center;">步骤</th>
            <th style="width: 120px; text-align: center;">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(task, idx) in filteredTasks" :key="task.id">
            <td style="text-align: center; font-size: 12px; color: var(--text-light);">{{ idx + 1 }}</td>
            <td>
              <span
                style="cursor: pointer; font-size: 13px;"
                @click="viewTask(task.id, task.status)"
              >{{ shortDesc(task.description) }}</span>
            </td>
            <td style="text-align: center;">
              <span class="badge" :class="'badge-' + task.status">{{ statusLabel(task.status) }}</span>
            </td>
            <td style="text-align: center; font-size: 12px; font-family: monospace;">
              {{ extractThreadId(task.description) }}
            </td>
            <td class="text-sm text-secondary" style="text-align: center;">{{ formatTime(task.created_at) }}</td>
            <td style="text-align: center; font-size: 12px;">
              <span v-if="task.plan.length">
                <template v-for="(s, i) in task.plan" :key="i">
                  <span :style="{
                    color: s.status === 'completed' ? '#10B981' : s.status === 'failed' ? '#EF4444' : s.status === 'running' ? '#3B82F6' : '#94A3B8'
                  }">{{ s.status === 'completed' ? '✓' : s.status === 'running' ? '○' : s.status === 'failed' ? '✗' : '·' }}</span>
                </template>
              </span>
              <span v-else class="text-secondary">-</span>
            </td>
            <td style="text-align: center;">
              <div class="flex gap-1" style="justify-content: center;">
                <button class="btn btn-outline btn-sm" @click="viewTask(task.id, task.status)">查看</button>
                <button
                  v-if="task.status !== 'running' && task.status !== 'pending' && task.status !== 'parsing'"
                  class="btn btn-outline btn-sm"
                  style="color: var(--error); border-color: transparent;"
                  @click="confirmDelete = task.id"
                >删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-else class="text-center text-secondary" style="padding: 48px;">
        <div style="font-size: 36px; margin-bottom: 8px;">📭</div>
        <p v-if="taskStore.tasks.length">无匹配结果</p>
        <p v-else>暂无任务记录，创建你的第一个任务吧</p>
      </div>
    </div>

    <!-- 删除确认弹窗 -->
    <div v-if="confirmDelete" class="modal-overlay" @click.self="confirmDelete = null">
      <div class="modal-content" style="max-width: 400px;">
        <div class="modal-header"><h3>确认删除</h3></div>
        <p class="mb-4">删除后无法恢复，确定要删除这条任务记录吗？</p>
        <div class="flex gap-2" style="justify-content: flex-end;">
          <button class="btn btn-outline" @click="confirmDelete = null">取消</button>
          <button class="btn btn-danger" @click="deleteTask(confirmDelete)">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>
