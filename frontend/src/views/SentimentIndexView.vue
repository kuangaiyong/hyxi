<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import * as sentimentApi from '@/api/sentiment'

const router = useRouter()
const items = ref<any[]>([])
const loading = ref(true)
const loadError = ref('')

// 过滤
const filterTaskStatus = ref('')
const filterSentimentStatus = ref('')
const filterSource = ref('')
const filterKeyword = ref('')

const SENTIMENT_LABELS: Record<string, string> = {
  done: '已分析', running: '分析中', none: '待分析',
}

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  loadError.value = ''
  try {
    const { fetchTasks } = await import('@/api/tasks')
    const result = await fetchTasks()
    const targetTasks = result.tasks.filter(t =>
      t.status === 'completed' || t.status === 'running' || t.status === 'failed'
    )

    const list = []
    for (const task of targetTasks) {
      try {
        let sentimentStatus = 'none'
        let sentimentSummary: any = null
        try {
          const senti = await sentimentApi.getSentiment(task.id)
          if (senti.summary) {
            sentimentStatus = 'done'
            sentimentSummary = senti.summary
          } else if (senti.status === 'running') {
            sentimentStatus = 'running'
          }
        } catch { /* none */ }

        list.push({
          sources: sourceNames(task),
          taskId: task.id,
          description: task.description,
          sentimentStatus,
          sentimentSummary,
          taskStatus: task.status,
          created: task.created_at,
          lastUpdated: task.completed_at || task.created_at,
        })
      } catch { /* skip */ }
    }
    items.value = list
  } catch (e: any) {
    // 后端不可达时渲染「暂无分析记录」会把故障说成空数据，用户只会白等
    loadError.value = '加载失败: ' + (e.response?.data?.detail || e.message || '网络错误')
  }
  finally { loading.value = false }
}

const filteredItems = computed(() => {
  let list = items.value
  if (filterTaskStatus.value) list = list.filter(i => i.taskStatus === filterTaskStatus.value)
  if (filterSentimentStatus.value) list = list.filter(i => i.sentimentStatus === filterSentimentStatus.value)
  if (filterSource.value) list = list.filter(i => i.sources.includes(filterSource.value))
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    list = list.filter(i => i.description.toLowerCase().includes(kw))
  }
  return list
})

// 所有来源（去重）
const allSources = computed(() => [...new Set(items.value.flatMap(i => i.sources))].sort())

// 统计面板
const statsPanel = computed(() => {
  const analyzed = items.value.filter(i => i.sentimentStatus === 'done')
  const latest = analyzed[0]
  const total = items.value.length
  const withSentiment = analyzed.length
  let avgIntensity = 0
  let positiveTotal = 0
  let negativeTotal = 0
  let neutralTotal = 0
  if (analyzed.length) {
    for (const a of analyzed) {
      if (a.sentimentSummary) {
        avgIntensity += a.sentimentSummary.avg_intensity || 0
        positiveTotal += a.sentimentSummary.sentiment_distribution?.positive || 0
        negativeTotal += a.sentimentSummary.sentiment_distribution?.negative || 0
        neutralTotal += a.sentimentSummary.sentiment_distribution?.neutral || 0
      }
    }
    avgIntensity = +(avgIntensity / analyzed.length).toFixed(2)
  }
  return {
    total,
    withSentiment,
    latest,
    avgIntensity,
    positiveTotal,
    negativeTotal,
    neutralTotal,
  }
})

/** 结果里存了当时的来源清单；失败任务没跑到落结果那一步，退回 plan 里的 collect 步骤 */
function sourceNames(task: any): string[] {
  const fromResult = (task.result?.sources || []).map((s: any) => s.name).filter(Boolean)
  if (fromResult.length) return fromResult
  return (task.plan || [])
    .filter((s: any) => s.action === 'collect' && s.params?.source_name)
    .map((s: any) => s.params.source_name)
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

function statusLabel(s: string): string {
  const m: Record<string, string> = {
    completed: '已完成', failed: '失败', running: '执行中', cancelled: '已取消',
  }
  return m[s] || s
}
</script>

<template>
  <div style="max-width: 1100px;">
    <!-- 统计面板 -->
    <div class="stats-grid" v-if="items.length">
      <div class="stat-card">
        <div class="stat-value">{{ statsPanel.total }}</div>
        <div class="stat-label">分析记录</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" style="color: var(--primary);">{{ statsPanel.withSentiment }}</div>
        <div class="stat-label">已有舆情</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" style="font-size: 14px;">
          <span style="color: #10B981;">😊{{ statsPanel.positiveTotal }}</span>
          <span style="color: #EF4444; margin-left: 8px;">😞{{ statsPanel.negativeTotal }}</span>
          <span style="color: #6B7280; margin-left: 8px;">😐{{ statsPanel.neutralTotal }}</span>
        </div>
        <div class="stat-label">情感累计</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ statsPanel.avgIntensity }} / 5</div>
        <div class="stat-label">平均强度</div>
      </div>
    </div>

    <!-- 过滤器 -->
    <div v-if="items.length" class="card" style="padding: 12px 20px;">
      <div class="flex items-center gap-4" style="flex-wrap: wrap;">
        <span class="text-sm" style="font-weight: 600;">📊 舆情分析历史</span>

        <select v-model="filterTaskStatus" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部任务状态</option>
          <option value="completed">已完成</option>
          <option value="failed">失败</option>
        </select>

        <select v-model="filterSentimentStatus" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部舆情状态</option>
          <option value="done">已分析</option>
          <option value="running">分析中</option>
          <option value="none">待分析</option>
        </select>

        <select v-if="allSources.length > 1" v-model="filterSource" class="form-input" style="width: auto; padding: 4px 24px 4px 8px; font-size: 12px;">
          <option value="">全部来源</option>
          <option v-for="s in allSources" :key="s" :value="s">{{ s }}</option>
        </select>

        <input v-model="filterKeyword" type="text" class="form-input" placeholder="搜索描述..."
          style="width: 160px; padding: 4px 8px; font-size: 12px;" />

        <button v-if="filterTaskStatus || filterSentimentStatus || filterSource || filterKeyword"
          class="btn btn-outline btn-sm"
          @click="filterTaskStatus='';filterSentimentStatus='';filterSource='';filterKeyword=''">清除</button>

        <span class="text-sm text-secondary" style="margin-left: auto;">共 {{ filteredItems.length }} 条</span>
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
    <div v-else-if="!items.length" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">📭</div>
      <p class="text-secondary mb-4">暂无分析记录，请先执行任务并进行舆情分析</p>
      <button class="btn btn-primary" @click="router.push('/tasks')">前往任务管理</button>
    </div>

    <!-- 表格 -->
    <div v-else class="card" style="padding: 0; overflow-x: auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 44px; text-align: center;">#</th>
            <th>任务描述</th>
            <th style="width: 120px; text-align: center;">来源</th>
            <th style="width: 80px; text-align: center;">任务状态</th>
            <th style="width: 80px; text-align: center;">舆情状态</th>
            <th style="width: 140px; text-align: center;">时间</th>
            <th style="width: 160px; text-align: center;">情感概览</th>
            <th style="width: 60px; text-align: center;">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!filteredItems.length">
            <td colspan="8" class="text-center text-secondary" style="padding: 32px;">无匹配结果</td>
          </tr>
          <tr v-for="(item, idx) in filteredItems" :key="item.taskId">
            <td style="text-align: center; font-size: 12px; color: var(--text-light);">{{ idx + 1 }}</td>
            <td style="font-size: 13px; max-width: 280px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
              {{ item.description }}
            </td>
            <td style="text-align: center; font-size: 12px;">{{ item.sources.join('、') || '-' }}</td>
            <td style="text-align: center;">
              <span class="badge" :class="'badge-' + item.taskStatus">{{ statusLabel(item.taskStatus) }}</span>
            </td>
            <td style="text-align: center;">
              <span class="badge" :class="{
                'badge-completed': item.sentimentStatus === 'done',
                'badge-running': item.sentimentStatus === 'running',
                'badge-pending': item.sentimentStatus === 'none',
              }">{{ SENTIMENT_LABELS[item.sentimentStatus] || '未知' }}</span>
            </td>
            <td class="text-sm text-secondary" style="text-align: center;">{{ formatTime(item.lastUpdated) }}</td>
            <td style="text-align: center; font-size: 12px;">
              <template v-if="item.sentimentSummary">
                <span style="color:#10B981;">😊{{ item.sentimentSummary.sentiment_distribution?.positive || 0 }}</span>
                <span style="color:#EF4444;margin-left:4px;">😞{{ item.sentimentSummary.sentiment_distribution?.negative || 0 }}</span>
                <span style="color:#6B7280;margin-left:4px;">😐{{ item.sentimentSummary.sentiment_distribution?.neutral || 0 }}</span>
                <span class="text-secondary" style="margin-left:4px;">{{ item.sentimentSummary.avg_intensity }}/5</span>
              </template>
              <span v-else class="text-secondary">-</span>
            </td>
            <td style="text-align: center;">
              <button class="btn btn-outline btn-sm" @click="router.push(`/tasks/${item.taskId}/sentiment`)">查看</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
