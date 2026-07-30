<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/composables/useToast'
import * as schedulesApi from '@/api/schedules'
import type { ScheduleTask, SchedulePreset } from '@/types/schedule'

const toast = useToast()

const router = useRouter()
const configStore = useConfigStore()
const taskStore = useTaskStore()
const schedules = ref<ScheduleTask[]>([])
const presets = ref<Record<string, SchedulePreset>>({})
const loading = ref(true)
const loadError = ref('')

// 过滤
const filterEnabled = ref('')
const filterInterval = ref('')

// 新建
const showForm = ref(false)
const formDesc = ref('')
const formInterval = ref('daily')
const formTime = ref('09:00')
const formError = ref('')

// 展开的执行历史
const expandedId = ref<string | null>(null)

const INTERVAL_LABELS: Record<string, string> = {
  hourly: '每小时', '6h': '每6小时', '12h': '每12小时', daily: '每天',
}

const filteredSchedules = computed(() => {
  let list = schedules.value
  if (filterEnabled.value === 'enabled') list = list.filter(s => s.enabled)
  if (filterEnabled.value === 'disabled') list = list.filter(s => !s.enabled)
  if (filterInterval.value) list = list.filter(s => s.interval === filterInterval.value)
  return list
})

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  loadError.value = ''
  try {
    const [sData, pData] = await Promise.all([
      schedulesApi.fetchSchedules(),
      schedulesApi.fetchPresets(),
    ])
    schedules.value = sData.schedules
    presets.value = pData.presets
    // 加载任务列表以便解析执行历史的状态
    await taskStore.fetchTasks()
  } catch (e: any) {
    // 后端不可达时渲染空状态会诱导用户「创建第一个」，从而建出重复的定时任务
    loadError.value = '加载失败: ' + (e.response?.data?.detail || e.message || '网络错误')
  }
  finally { loading.value = false }
}

function getHistoryStatus(taskId: string): string {
  const task = taskStore.tasks.find(t => t.id === taskId)
  return task?.status || 'unknown'
}

function statusLabel(s: string): string {
  const m: Record<string, string> = {
    completed: '已完成', failed: '失败', running: '执行中',
    parsing: '解析中', pending: '等待中', cancelled: '已取消',
    started: '启动中', unknown: '未知',
  }
  return m[s] || s
}

async function handleCreate() {
  formError.value = ''
  if (!formDesc.value.trim()) { formError.value = '请输入任务描述'; return }
  try {
    await schedulesApi.createSchedule({
      description: formDesc.value,
      interval: formInterval.value,
      time: formTime.value,
    })
    showForm.value = false
    formDesc.value = ''
    await loadData()
  } catch (e: any) {
    formError.value = e.response?.data?.detail || '创建失败'
  }
}

async function handleToggle(item: ScheduleTask) {
  try {
    await schedulesApi.toggleSchedule(item.id)
    await loadData()
  } catch (e: any) {
    toast.error('切换启用状态失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function handleDelete(id: string) {
  if (!confirm('确定删除此定时任务？')) return
  try {
    await schedulesApi.deleteSchedule(id)
    expandedId.value = null
    await loadData()
    toast.success('定时任务已删除')
  } catch (e: any) {
    toast.error('删除失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function handleRunNow(id: string) {
  try {
    const result = await schedulesApi.runScheduleNow(id)
    toast.success('已触发执行')
    if (result.task_id) {
      router.push(`/tasks/${result.task_id}/progress`)
    }
  } catch (e: any) {
    toast.error('触发失败: ' + (e.response?.data?.detail || e.message))
  }
}

function formatTime(iso: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${y}-${m}-${dd} ${hh}:${mm}`
}
</script>

<template>
  <div style="max-width: 1100px;">
    <div class="flex items-center justify-between mb-4">
      <h2 style="font-size: 18px; font-weight: 600;">⏰ 定时任务</h2>
      <button class="btn btn-primary" @click="showForm = !showForm">
        {{ showForm ? '取消' : '+ 新建定时任务' }}
      </button>
    </div>

    <!-- 创建表单 -->
    <div v-if="showForm" class="card">
      <div class="card-header">新建定时任务</div>
      <div class="form-group">
        <label class="form-label">任务描述</label>
        <textarea v-model="formDesc" class="form-input" rows="2"
          placeholder="例如：抓取帖子2336074的所有内容，翻译成中文，舆情分析"></textarea>
      </div>
      <div class="flex gap-4 mb-4" style="flex-wrap: wrap;">
        <div class="form-group" style="margin-bottom: 0;">
          <label class="form-label">执行频率</label>
          <select v-model="formInterval" class="form-input" style="width: auto; min-width: 120px;">
            <option v-for="(v, k) in presets" :key="k" :value="k">{{ v.label }}</option>
          </select>
        </div>
        <div v-if="formInterval === 'daily'" class="form-group" style="margin-bottom: 0;">
          <label class="form-label">执行时间</label>
          <input v-model="formTime" type="time" class="form-input" style="width: auto;" />
        </div>
      </div>
      <div v-if="formError" class="mb-4" style="padding: 8px 12px; border-radius: 6px; font-size:13px; background:#FEE2E2;color:#DC2626;">❌ {{ formError }}</div>
      <button class="btn btn-primary" @click="handleCreate" :disabled="!configStore.isConfigured">💾 创建</button>
      <span v-if="!configStore.isConfigured" class="text-sm text-secondary ml-2">请先 <router-link to="/config">配置 LLM</router-link></span>
    </div>

    <!-- 过滤器 -->
    <div v-if="schedules.length" class="card" style="padding: 12px 20px;">
      <div class="flex items-center gap-4" style="flex-wrap: wrap;">
        <span class="text-sm" style="font-weight: 600;">📜 定时任务列表</span>
        <select v-model="filterEnabled" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部状态</option>
          <option value="enabled">已启用</option>
          <option value="disabled">已暂停</option>
        </select>
        <select v-model="filterInterval" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部频率</option>
          <option value="hourly">每小时</option>
          <option value="6h">每6小时</option>
          <option value="12h">每12小时</option>
          <option value="daily">每天</option>
        </select>
        <button v-if="filterEnabled || filterInterval" class="btn btn-outline btn-sm" @click="filterEnabled='';filterInterval=''">清除</button>
        <span class="text-sm text-secondary" style="margin-left: auto;">共 {{ filteredSchedules.length }} 条</span>
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="card text-center" style="padding: 48px;"><span class="spinner spinner-lg"></span></div>

    <!-- 加载失败 -->
    <div v-else-if="loadError" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">⚠️</div>
      <p class="text-secondary mb-4">{{ loadError }}</p>
      <button class="btn btn-primary" @click="loadData">重试</button>
    </div>

    <!-- 空状态 -->
    <div v-else-if="!schedules.length" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">⏰</div>
      <p class="text-secondary mb-4">暂无定时任务，创建第一个定时任务开始自动追踪帖子舆情</p>
      <button class="btn btn-primary" @click="showForm = true">创建定时任务</button>
    </div>

    <!-- 任务列表 -->
    <div v-else class="card" style="padding: 0; overflow-x: auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 40px; text-align: center;">#</th>
            <th style="width: 30px;"></th>
            <th>任务描述</th>
            <th style="width: 90px; text-align: center;">频率</th>
            <th style="width: 130px; text-align: center;">下次执行</th>
            <th style="width: 130px; text-align: center;">上次执行</th>
            <th style="width: 80px; text-align: center;">状态</th>
            <th style="width: 200px; text-align: center;">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(item, idx) in filteredSchedules" :key="item.id">
            <tr @click="expandedId = expandedId === item.id ? null : item.id" style="cursor: pointer;">
              <td style="text-align: center; font-size: 12px; color: var(--text-light);">{{ idx + 1 }}</td>
              <td style="text-align: center; font-size: 14px;">
                {{ expandedId === item.id ? '▾' : '▸' }}
              </td>
              <td style="font-weight: 500;">{{ item.description }}</td>
              <td style="text-align: center;">
                <span class="text-sm">{{ INTERVAL_LABELS[item.interval] || item.interval }}</span>
                <span v-if="item.interval === 'daily'" class="text-sm text-secondary"> {{ item.time }}</span>
              </td>
              <td style="text-align: center;" class="text-sm">{{ formatTime(item.next_run || '') }}</td>
              <td style="text-align: center;" class="text-sm text-secondary">{{ formatTime(item.last_run || '') }}</td>
              <td style="text-align: center;">
                <span :style="{
                  display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%',
                  background: item.enabled ? '#10B981' : '#94A3B8',
                }" :title="item.enabled ? '已启用' : '已暂停'"></span>
                {{ item.enabled ? '启用' : '暂停' }}
              </td>
              <td style="text-align: center;">
                <div class="flex gap-1" style="justify-content: center;" @click.stop>
                  <button class="btn btn-outline btn-sm" @click="handleRunNow(item.id)">▶ 执行</button>
                  <button class="btn btn-outline btn-sm" @click="handleToggle(item)">{{ item.enabled ? '⏸' : '▶' }}</button>
                  <button class="btn btn-outline btn-sm" style="color: var(--error);" @click="handleDelete(item.id)">删除</button>
                </div>
              </td>
            </tr>
            <!-- 展开的执行历史 -->
            <tr v-if="expandedId === item.id">
              <td colspan="8" style="padding: 0; background: #F8FAFC;">
                <div style="padding: 12px 40px; font-size: 13px;">
                  <div v-if="!item.history?.length" class="text-secondary">暂无执行记录</div>
                  <div v-else v-for="(h, idx) in [...item.history].reverse()" :key="h.task_id"
                    style="display: flex; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--border-light);">
                    <span class="text-sm text-secondary" style="width: 100px;">{{ formatTime(h.time) }}</span>
                    <span class="badge" :class="'badge-' + getHistoryStatus(h.task_id)">{{ statusLabel(getHistoryStatus(h.task_id)) }}</span>
                    <span class="text-sm text-secondary">
                      第 {{ item.history!.length - idx }} 次执行
                      <template v-if="getHistoryStatus(h.task_id) === 'completed'">
                        · 共 {{ taskStore.tasks.find(t => t.id === h.task_id)?.result?.total_posts || '?' }} 帖
                      </template>
                    </span>
                    <span style="flex: 1;"></span>
                    <button class="btn btn-outline btn-sm"
                      @click="router.push(getHistoryStatus(h.task_id) === 'completed' || getHistoryStatus(h.task_id) === 'failed'
                        ? `/tasks/${h.task_id}/results` : `/tasks/${h.task_id}/progress`)">
                      查看 →
                    </button>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>
