<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import * as sentimentApi from '@/api/sentiment'
import * as resultsApi from '@/api/results'
import { useToast } from '@/composables/useToast'
import { downloadFile } from '@/utils/download'
import type { SentimentData, PostWithSentiment } from '@/types/sentiment'
import type { PostData } from '@/types/result'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const taskId = computed(() => route.params.id as string)

const data = ref<SentimentData | null>(null)
const loading = ref(false)
const analyzing = ref(false)
const error = ref('')
const crossTaskWarning = ref('')
const downloading = ref(false)
const eventSource = ref<EventSource | null>(null)
const showDetail = ref(false)
const detailPost = ref<PostWithSentiment | null>(null)
// 帖子原始数据（用于对照查看）
const postsMap = ref<Map<number, PostData>>(new Map())
const pendingCount = ref(0)
// 实时进度
const progressMsg = ref('')
const progressPct = ref(0)
const progressLogs = ref<string[]>([])
// 过滤条件
const filterSentiment = ref('')
const filterDimension = ref('')
const filterKeyword = ref('')

// 所有维度列表（从数据中提取）
const allDimensions = computed(() => {
  const dims = new Set<string>()
  if (!data.value?.results) return []
  for (const r of data.value.results) {
    if (r?.dimensions) {
      for (const d of r.dimensions) dims.add(d)
    }
  }
  return Array.from(dims).sort()
})

// 过滤后的结果
// 注意不能用 { ...r, _idx } 打平：展开 null 得到的是 {_idx} 这种永远 truthy 的对象，
// 分析失败的条目会伪装成正常结果，模板里的空值守卫全部失效。
const filteredResults = computed(() => {
  if (!data.value?.results) return []
  let rows = data.value.results.map((result, _idx) => ({ result, _idx }))

  if (filterSentiment.value) {
    rows = rows.filter(x => x.result?.sentiment === filterSentiment.value)
  }
  if (filterDimension.value) {
    rows = rows.filter(x => x.result?.dimensions?.includes(filterDimension.value))
  }
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    rows = rows.filter(x =>
      x.result?.reason_cn?.toLowerCase().includes(kw) ||
      x.result?.dimensions?.some(d => d.toLowerCase().includes(kw))
    )
  }
  return rows
})

function clearFilters() {
  filterSentiment.value = ''
  filterDimension.value = ''
  filterKeyword.value = ''
}

const COLORS = {
  positive: '#10B981',
  negative: '#EF4444',
  neutral: '#6B7280',
}

const DIM_COLORS: Record<string, string> = {
  '价格/性价比': '#3B82F6',
  '产品质量/可靠性': '#8B5CF6',
  '安装/配置体验': '#F59E0B',
  'App/软件体验': '#06B6D4',
  '客服/售后支持': '#10B981',
  'WiFi/连接问题': '#F97316',
  '固件更新': '#EC4899',
  '温度/散热': '#EF4444',
  'P1电表/智能控制': '#6366F1',
  '认证/合规(如Synergrid)': '#14B8A6',
  '扩展/兼容性': '#84CC16',
  '与其他品牌对比(如AEG/Marstek)': '#A855F7',
  '性能/效率': '#0EA5E9',
  '安全性': '#DC2626',
}

// 按需注册，整包 echarts 会往首屏包里塞进一兆多用不上的图表类型
echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

onMounted(() => {
  checkStatus()
  themeOb = new MutationObserver(() => { if (trendChart) renderTrend() })
  themeOb.observe(document.documentElement, { attributeFilter: ['data-theme'] })
})

onUnmounted(() => {
  closeSSE()
  stopPolling()
  clearConnectTimer()
  themeOb?.disconnect()
  disposeTrend()
})

// 定时器句柄：组件卸载后再触发就会泄漏一条无人持有的 EventSource
let connectTimer: number | null = null
let pollTimer: number | null = null
let sseRetries = 0

function clearConnectTimer() {
  if (connectTimer !== null) {
    clearTimeout(connectTimer)
    connectTimer = null
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

/** SSE 反复重连失败后的兜底：每 5 秒直接拉一次结果，直到分析结束 */
function schedulePoll() {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    pollTimer = null
    let result: SentimentData | null = null
    try {
      result = await sentimentApi.getSentiment(taskId.value)
    } catch {
      /* 后端仍不可达，继续轮询 */
    }
    if (result?.task_id) {
      applyData(result)
      analyzing.value = false
      await loadPosts()
    } else if (!result || result.status === 'running') {
      schedulePoll()
    } else {
      analyzing.value = false
      error.value = '分析已结束但未取到结果，请重试'
    }
  }, 5000)
}

function closeSSE() {
  if (eventSource.value) {
    eventSource.value.close()
    eventSource.value = null
  }
}

function connectSSE() {
  closeSSE()
  stopPolling()
  sseRetries = 0
  const url = sentimentApi.getSentimentEventsUrl(taskId.value)
  const es = new EventSource(url)
  eventSource.value = es

  es.addEventListener('step_progress', (e: MessageEvent) => {
    const d = JSON.parse(e.data)
    progressPct.value = Math.round((d.progress || 0) * 100)
    if (d.message) progressMsg.value = d.message
  })

  es.addEventListener('log', (e: MessageEvent) => {
    const d = JSON.parse(e.data)
    const msg = d.message || ''
    // 过滤掉重复的进度计数日志（进度条已展示）
    if (/已分析\s*\d+\/\d+\s*条/.test(msg)) return
    // 从初始日志中提取实际待分析条数，动态更新 pendingCount
    const contentMatch = msg.match(/(\d+)\s*条有内容/)
    if (contentMatch && pendingCount.value === 0) {
      pendingCount.value = parseInt(contentMatch[1])
    }
    progressLogs.value.push(msg)
    if (progressLogs.value.length > 20) progressLogs.value.shift()
  })

  es.addEventListener('error', (e: MessageEvent) => {
    // 无 data 的是连接层错误，交给下面的 onerror 走重连/轮询；这里只处理后端主动推的错误
    if (!e.data) return
    const d = JSON.parse(e.data)
    progressLogs.value.push('⚠️ ' + (d.message || '发生错误'))
    closeSSE()
    analyzing.value = false
    error.value = '分析失败: ' + (d.message || '未知错误')
  })

  es.addEventListener('sentiment_complete', (e: MessageEvent) => {
    const d = JSON.parse(e.data)
    closeSSE()
    if (d.status === 'completed') {
      // 拉取最终结果
      loadResults()
    } else {
      analyzing.value = false
      error.value = '分析失败: ' + (d.error || '未知错误')
    }
  })

  es.onerror = () => {
    // 不能在这里 close()：那会连 EventSource 自带的自动重连一起关掉，界面永久转圈
    sseRetries++
    if (sseRetries >= 3) {
      closeSSE()
      progressLogs.value.push('⚠️ 实时连接持续中断，改用轮询获取结果')
      schedulePoll()
    }
  }
}

/** 后端曾对查不到的任务回退返回最新一条舆情，索引与本任务帖子对不上，必须显式提示 */
function applyData(result: SentimentData) {
  data.value = result
  crossTaskWarning.value = result.task_id === taskId.value
    ? ''
    : `当前展示的是任务 ${result.task_id} 的舆情结果，与本任务的帖子并不对应`
}

async function loadPosts() {
  // 趋势图和详情弹窗都按绝对下标取帖子，只拉第一页会让 index ≥ 200 的行全空
  try {
    const size = 200  // 后端 page_size 上限
    const map = new Map<number, PostData>()
    // 评论挂在主贴的 replies 里，只遍历顶层就把它们全漏了 —— 而舆情结果的下标
    // 来自扁平数组，评论也占位。实测 88 条里 42 条是评论，趋势图因此只画了一半
    const walk = (p: PostData) => {
      map.set(p.index - 1, p)  // index 是 1-based
      ;(p.replies || []).forEach(walk)
    }
    const first = await resultsApi.fetchPosts(taskId.value, 1, size)
    first.posts.forEach(walk)
    // 页数由首个响应的 total 定死，避免网络循环依赖后端的翻页终止条件
    const pages = Math.ceil(first.total / size)
    for (let page = 2; page <= pages; page++) {
      const result = await resultsApi.fetchPosts(taskId.value, page, size)
      result.posts.forEach(walk)
    }
    postsMap.value = map
  } catch (e) {
    // 非关键数据，静默失败
  }
}

async function loadResults() {
  try {
    const result = await sentimentApi.getSentiment(taskId.value)
    if (result.task_id) {
      applyData(result)
      analyzing.value = false
      await loadPosts()
    } else if (result.status === 'running') {
      analyzing.value = true
      connectSSE()
    } else if (result.status === 'not_found') {
      analyzing.value = false
      error.value = '尚未进行舆情分析'
    } else {
      analyzing.value = false
      error.value = '分析结果异常，请重试'
    }
  } catch (e: any) {
    analyzing.value = false
    error.value = '加载结果失败: ' + (e?.response?.data?.detail || e?.message || '网络错误')
  }
}

async function checkStatus() {
  loading.value = true
  error.value = ''
  try {
    const result = await sentimentApi.getSentiment(taskId.value)
    if (result.status === 'running') {
      analyzing.value = true
      pendingCount.value = 0
      connectSSE()
      return
    }
    if (result.status === 'not_found') {
      error.value = '尚未进行舆情分析'
      return
    }
    if (result.task_id) {
      applyData(result)
      await loadPosts()
    } else {
      error.value = '尚未进行舆情分析'
    }
  } catch (e: any) {
    error.value = '加载失败: ' + (e?.response?.data?.detail || e?.message || '网络错误')
  } finally {
    loading.value = false
  }
}

async function startAnalysis() {
  analyzing.value = true
  error.value = ''
  progressMsg.value = '正在启动分析...'
  progressPct.value = 0
  progressLogs.value = []
  pendingCount.value = 0
  try {
    const result = await sentimentApi.triggerSentiment(taskId.value)
    // 优先使用 API 返回的 pending_count 字段，fallback 到正则匹配消息文本
    if (typeof result.pending_count === 'number') {
      pendingCount.value = result.pending_count
    } else {
      const match = result.message?.match(/(\d+)\s*条待分析/)
      if (match) pendingCount.value = parseInt(match[1])
    }
    if (result.status === 'started' || result.status === 'running') {
      clearConnectTimer()
      connectTimer = window.setTimeout(() => {
        connectTimer = null
        connectSSE()
      }, 500)
    } else if (result.status === 'completed') {
      // 所有帖子已完成分析，直接加载已有结果
      await loadResults()
    } else {
      analyzing.value = false
      error.value = '分析未能启动: ' + (result.message || result.status || '未知状态')
    }
  } catch (e: any) {
    analyzing.value = false
    error.value = '启动分析失败: ' + (e.response?.data?.detail || e.message)
  }
}

// ===== 图表计算 =====

const pieData = computed(() => {
  if (!data.value?.summary) return []
  const dist = data.value.summary.sentiment_distribution
  const total = Object.values(dist).reduce((a: number, b: number) => a + b, 0) || 1
  return [
    { label: '正面', value: dist.positive, pct: (dist.positive / total * 100).toFixed(1), color: COLORS.positive },
    { label: '负面', value: dist.negative, pct: (dist.negative / total * 100).toFixed(1), color: COLORS.negative },
    { label: '中立', value: dist.neutral, pct: (dist.neutral / total * 100).toFixed(1), color: COLORS.neutral },
  ]
})

const pieSegments = computed(() => {
  const items = pieData.value
  const total = items.reduce((s, i) => s + i.value, 0) || 1
  let offset = 0
  return items.map(item => {
    const angle = (item.value / total) * 360
    const start = offset
    offset += angle
    return { ...item, start, angle }
  })
})

function polarToCartesian(cx: number, cy: number, r: number, angleDeg: number) {
  const angleRad = (angleDeg - 90) * Math.PI / 180
  return { x: cx + r * Math.cos(angleRad), y: cy + r * Math.sin(angleRad) }
}

function describeArc(cx: number, cy: number, r: number, startAngle: number, endAngle: number) {
  if (endAngle - startAngle >= 360) {
    const mid = polarToCartesian(cx, cy, r, startAngle + 180)
    return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${mid.x} ${mid.y} A ${r} ${r} 0 1 1 ${cx} ${cy - r}`
  }
  const largeArc = endAngle - startAngle > 180 ? 1 : 0
  const start = polarToCartesian(cx, cy, r, endAngle)
  const end = polarToCartesian(cx, cy, r, startAngle)
  return `M ${cx} ${cy} L ${start.x} ${start.y} A ${r} ${r} 0 ${largeArc} 0 ${end.x} ${end.y} Z`
}

const maxDimCount = computed(() => {
  const dims = data.value?.summary?.top_dimensions || []
  return Math.max(...dims.map(d => d[1]), 1)
})

// ===== 按来源对比 =====

const SENTIMENT_COLORS: Record<string, string> = {
  positive: '#10B981', negative: '#EF4444', neutral: '#94A3B8',
}

const bySourceRows = computed(() => {
  const by = data.value?.summary?.by_source
  if (!by) return []
  return Object.entries(by).map(([id, b]) => {
    const total = b.analyzed || 1
    return {
      id,
      name: b.name || id,
      analyzed: b.analyzed,
      avg_intensity: b.avg_intensity,
      distribution: b.distribution,
      top_dimensions: b.top_dimensions || [],
      segments: (['positive', 'negative', 'neutral'] as const)
        .filter(k => (b.distribution[k] || 0) > 0)
        .map(k => ({
          label: k,
          value: b.distribution[k] || 0,
          pct: (b.distribution[k] || 0) / total * 100,
          color: SENTIMENT_COLORS[k],
        })),
    }
  })
})

const crossSourceRows = computed(() => {
  const cross = data.value?.summary?.cross_source
  if (!cross) return []
  return Object.entries(cross)
    .map(([name, counts]) => ({
      name,
      counts,
      total: Object.values(counts).reduce((a, b) => a + b, 0),
    }))
    .sort((a, b) => b.total - a.total)
})

// ===== 趋势图数据 =====

const trendData = computed(() => {
  if (!data.value?.results || !postsMap.value.size) return []
  const results = data.value.results
  // 收集每篇帖子的日期和情感
  const daily: Record<string, { positive: number; negative: number; neutral: number }> = {}
  for (let i = 0; i < results.length; i++) {
    const post = postsMap.value.get(i)
    if (!post?.timestamp) continue
    const r = results[i]
    if (!r?.sentiment) continue
    // 提取日期部分 (yyyy-mm-dd)
    const dateStr = post.timestamp.slice(0, 10)
    if (!daily[dateStr]) daily[dateStr] = { positive: 0, negative: 0, neutral: 0 }
    daily[dateStr][r.sentiment as keyof typeof daily[string]]++
  }
  return Object.entries(daily)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, counts]) => ({ date, ...counts }))
})

// 趋势图只画得出有时间戳的帖子。旧版提取器留下的条目没有时间，
// 不把这个数标出来，图上的点加起来对不上概览的「已分析」，看着像少算了
const trendTotal = computed(() =>
  trendData.value.reduce((n, d) => n + d.positive + d.negative + d.neutral, 0)
)

const SERIES = [
  { key: 'positive', label: '正面' },
  { key: 'negative', label: '负面' },
  { key: 'neutral', label: '中立' },
] as const

// ===== 趋势图（ECharts）=====

const trendEl = ref<HTMLElement | null>(null)
let trendChart: echarts.ECharts | null = null
let trendResizeOb: ResizeObserver | null = null
let themeOb: MutationObserver | null = null

/** 深浅色是 AppLayout 直接改 documentElement 上的 data-theme，没有 store 可订阅，
 *  ECharts 又只吃具体色值，只能在每次出图时把当下的 CSS 变量读出来 */
function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function trendOption() {
  const label = cssVar('--text-light')
  const line = cssVar('--border-light')
  return {
    color: SERIES.map((s) => COLORS[s.key]),
    // ECharts 6 起 containLabel 作废且静默失效，改用 outerBounds 把「绘图区 + 轴标签」
    // 一起框住；内边距必须显式归零，否则默认的 10% 会把图挤成 830px 宽
    grid: { left: 0, right: 0, top: 0, bottom: 0, outerBounds: { left: 4, right: 16, top: 36, bottom: 4 } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: cssVar('--bg-card'),
      borderColor: cssVar('--border'),
      textStyle: { color: cssVar('--text'), fontSize: 12 },
    },
    legend: { top: 0, itemHeight: 8, textStyle: { color: label } },
    xAxis: {
      type: 'category',
      // 类目值保留完整日期让提示框显示年份，轴上只画月-日，26 天才不用旋转
      data: trendData.value.map((d) => d.date),
      boundaryGap: false,
      axisLine: { lineStyle: { color: line } },
      axisTick: { show: false },
      axisLabel: { color: label, formatter: (v: string) => v.slice(5) },
    },
    yAxis: {
      type: 'value',
      // 条数是整数，不加这条会在数据少时画出 0.5 这样的刻度
      minInterval: 1,
      axisLabel: { color: label },
      splitLine: { lineStyle: { color: line } },
    },
    series: SERIES.map((s) => ({
      name: s.label,
      type: 'line',
      symbol: 'circle',
      symbolSize: 6,
      data: trendData.value.map((d) => d[s.key]),
    })),
  }
}

function renderTrend() {
  if (!trendEl.value) return
  if (!trendChart) {
    trendChart = echarts.init(trendEl.value)
    // 侧边栏折叠不会触发 window resize，盯容器才两种情况都覆盖得到
    trendResizeOb = new ResizeObserver(() => trendChart?.resize())
    trendResizeOb.observe(trendEl.value)
  }
  trendChart.setOption(trendOption(), true)
}

function disposeTrend() {
  trendResizeOb?.disconnect()
  trendResizeOb = null
  trendChart?.dispose()
  trendChart = null
}

// 必须连容器一起 watch：数据先于结果区渲染就位（结果区还被 loading 挡着），
// 只盯 trendData 会在容器还是 null 时白跑一次，之后容器挂上来也没人再画
watch([trendData, trendEl], () => {
  if (trendEl.value && trendData.value.length > 1) renderTrend()
  else disposeTrend()
}, { flush: 'post' })

function sentimentLabel(s: string | null | undefined): string {
  if (!s) return '未分析'
  return { positive: '正面', negative: '负面', neutral: '中立' }[s] || s
}

function sentimentColor(s: string | null | undefined): string {
  return (COLORS as Record<string, string>)[s || ''] || '#94A3B8'
}

function intensityStars(n: number): string {
  // LLM 可能返回越界或非数值的 intensity，String.repeat 收到负数会直接抛 RangeError
  const stars = Number.isFinite(n) ? Math.min(5, Math.max(0, Math.round(n))) : 0
  return '★'.repeat(stars) + '☆'.repeat(5 - stars)
}

async function handleDownload() {
  downloading.value = true
  try {
    await downloadFile(
      sentimentApi.getSentimentDownloadUrl(taskId.value),
      `舆情分析报告_${taskId.value}.xlsx`
    )
  } catch (e: any) {
    toast.error('下载舆情报告失败: ' + (e?.message || '网络错误'))
  } finally {
    downloading.value = false
  }
}

function viewPost(idx: number) {
  const r = data.value?.results?.[idx]
  const post = postsMap.value.get(idx)
  detailPost.value = {
    index: idx + 1,
    username: post?.username || '',
    content: post?.content || '',
    translation: post?.translation || '',
    sentiment: r || null,
  }
  showDetail.value = true
}
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-4">
      <h2 style="font-size: 18px; font-weight: 600;">📊 舆情分析</h2>
      <div class="flex gap-2">
        <button
          v-if="data"
          class="btn btn-success btn-sm"
          :disabled="downloading"
          @click="handleDownload"
        >
          {{ downloading ? '下载中...' : '📥 下载舆情报告' }}
        </button>
        <button class="btn btn-outline btn-sm" @click="router.push(`/tasks/${taskId}/results`)">
          ← 返回结果
        </button>
      </div>
    </div>

    <!-- 初始加载 -->
    <div v-if="loading" class="card text-center" style="padding: 48px;">
      <span class="spinner spinner-lg"></span>
      <p class="mt-4 text-secondary">加载中...</p>
    </div>

    <!-- 分析进行中 -->
    <div v-else-if="analyzing" class="card text-center" style="padding: 48px;">
      <span class="spinner spinner-lg"></span>
      <p style="font-size: 16px; font-weight: 600; margin-top: 16px;">🔍 舆情分析进行中...</p>
      <p class="text-secondary mt-2">
        正在使用 LLM 逐条分析帖子情感倾向<span v-if="pendingCount">，{{ pendingCount }} 条待分析</span>，预计需要 1-3 分钟。
      </p>
      <!-- 进度条 -->
      <div v-if="progressPct > 0" style="margin: 24px auto 0; max-width: 400px;">
        <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 13px;">
          <span class="text-secondary">分析进度</span>
          <span style="font-weight: 600;">{{ progressPct }}%</span>
        </div>
        <div style="background: var(--border-light); border-radius: 6px; height: 8px; overflow: hidden;">
          <div
            :style="{
              width: progressPct + '%',
              height: '100%',
              background: 'linear-gradient(90deg, #3B82F6, #6366F1)',
              borderRadius: '6px',
              transition: 'width 0.5s',
            }"
          ></div>
        </div>
        <p v-if="progressMsg" class="text-sm text-secondary mt-2">{{ progressMsg }}</p>
      </div>
      <!-- 实时日志 -->
      <div v-if="progressLogs.length" class="log-viewer mt-4" style="max-height: 200px; text-align: left;">
        <div
          v-for="(log, idx) in progressLogs"
          :key="idx"
          class="log-line info"
        >{{ log }}</div>
      </div>
    </div>

    <!-- 未分析 -->
    <div v-else-if="error" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">📭</div>
      <p class="text-secondary mb-4">{{ error }}</p>
      <button class="btn btn-primary btn-lg" @click="startAnalysis" :disabled="analyzing">
        <span v-if="analyzing" class="spinner"></span>
        🔍 开始舆情分析
      </button>
    </div>

    <!-- 分析结果 -->
    <template v-else-if="data">
      <div
        v-if="crossTaskWarning"
        class="card"
        style="border-left: 4px solid var(--warning, #F59E0B); margin-bottom: 16px;"
      >
        ⚠️ {{ crossTaskWarning }}
      </div>

      <!-- 概览卡片 -->
      <div class="stats-grid">
        <!-- 分子分母一起给：三档情感之和等于「已分析」而不是总数，
             只显示总数会让人以为少算了几条 -->
        <div class="stat-card">
          <div class="stat-value">{{ data.success }} / {{ data.total }}</div>
          <div class="stat-label">已分析 / 总帖子数</div>
        </div>
        <div v-if="data.failed" class="stat-card">
          <div class="stat-value" style="color: var(--warning, #D97706);">{{ data.failed }}</div>
          <div class="stat-label">未分析（见下表）</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color: var(--success);">{{ data.summary.sentiment_distribution.positive }}</div>
          <div class="stat-label">正面评价</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color: var(--error);">{{ data.summary.sentiment_distribution.negative }}</div>
          <div class="stat-label">负面评价</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color: var(--text-secondary);">{{ data.summary.sentiment_distribution.neutral }}</div>
          <div class="stat-label">中立评价</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ data.summary.avg_intensity }} / 5</div>
          <div class="stat-label">平均情感强度</div>
        </div>
      </div>

      <!-- 图表行 -->
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px;">
        <!-- 饼图: 情感分布 -->
        <div class="card">
          <div class="card-header">🎯 情感分布</div>
          <div style="display: flex; align-items: center; gap: 32px;">
            <svg width="180" height="180" viewBox="0 0 180 180">
              <g v-for="seg in pieSegments" :key="seg.label">
                <path
                  :d="describeArc(90, 90, 80, seg.start, seg.start + seg.angle)"
                  :fill="seg.color"
                  stroke="white"
                  stroke-width="2"
                />
              </g>
              <circle cx="90" cy="90" r="45" fill="white" />
              <text x="90" y="86" text-anchor="middle" font-size="20" font-weight="700" fill="#1E293B">
                {{ data.total }}
              </text>
              <text x="90" y="104" text-anchor="middle" font-size="11" fill="#64748B">总计</text>
            </svg>
            <div style="flex: 1;">
              <div v-for="seg in pieData" :key="seg.label" style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                <div :style="{ width: '12px', height: '12px', borderRadius: '3px', background: seg.color, flexShrink: '0' }"></div>
                <span style="font-size: 13px; font-weight: 500; width: 36px;">{{ seg.label }}</span>
                <span style="font-size: 13px; color: var(--text-secondary); width: 36px;">{{ seg.value }}条</span>
                <span style="font-size: 13px; font-weight: 600;">{{ seg.pct }}%</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 柱状图: 关注维度 Top 10 -->
        <div class="card">
          <div class="card-header">🔑 关注维度 Top 10</div>
          <div v-if="data.summary.top_dimensions.length">
            <div
              v-for="[dim, count] in data.summary.top_dimensions"
              :key="dim"
              style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;"
            >
              <span style="font-size: 11px; width: 100px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0;" :title="dim">{{ dim }}</span>
              <div style="flex: 1; background: var(--border-light); border-radius: 4px; height: 20px; overflow: hidden;">
                <div
                  :style="{
                    width: (count / maxDimCount * 100) + '%',
                    height: '100%',
                    background: DIM_COLORS[dim] || '#6366F1',
                    borderRadius: '4px',
                    transition: 'width 0.5s',
                  }"
                ></div>
              </div>
              <span style="font-size: 12px; font-weight: 600; width: 24px; text-align: right;">{{ count }}</span>
            </div>
          </div>
          <div v-else class="text-center text-secondary" style="padding: 24px;">暂无维度数据</div>
        </div>
      </div>

      <!-- 按来源对比：单来源任务后端不产出 by_source，整块不渲染 -->
      <div v-if="bySourceRows.length > 1" class="card">
        <div class="card-header">🌐 按来源对比</div>
        <p class="text-secondary text-sm mb-4">
          不同平台的表达语气基线不同，评分是按平台内部的相对水平判断的，跨平台看趋势而非绝对值。
        </p>
        <div style="overflow-x: auto;">
          <table class="data-table">
            <thead>
              <tr>
                <th>来源</th>
                <th style="width: 70px; text-align: center;">已分析</th>
                <th style="width: 200px;">情感分布</th>
                <th style="width: 80px; text-align: center;">强度均值</th>
                <th>主要维度</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in bySourceRows" :key="row.id">
                <td style="font-weight: 500;">{{ row.name }}</td>
                <td style="text-align: center;">{{ row.analyzed }}</td>
                <td>
                  <div style="display: flex; height: 16px; border-radius: 4px; overflow: hidden;">
                    <div v-for="seg in row.segments" :key="seg.label"
                      :title="`${seg.label} ${seg.value} 条`"
                      :style="{ width: seg.pct + '%', background: seg.color }"></div>
                  </div>
                  <div class="text-sm text-secondary" style="margin-top: 2px;">
                    正 {{ row.distribution.positive }} · 负 {{ row.distribution.negative }} · 中 {{ row.distribution.neutral }}
                  </div>
                </td>
                <td style="text-align: center;">{{ row.avg_intensity }}</td>
                <td class="text-sm text-secondary">
                  {{ row.top_dimensions.slice(0, 3).map(d => `${d[0]}(${d[1]})`).join('、') || '-' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="crossSourceRows.length" style="margin-top: 16px;">
          <div class="text-sm" style="font-weight: 600; margin-bottom: 8px;">同一维度在各来源的提及次数</div>
          <div style="overflow-x: auto;">
            <table class="data-table">
              <thead>
                <tr>
                  <th>维度</th>
                  <th v-for="row in bySourceRows" :key="row.id" style="text-align: center;">{{ row.name }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="dim in crossSourceRows" :key="dim.name">
                  <td>{{ dim.name }}</td>
                  <td v-for="row in bySourceRows" :key="row.id" style="text-align: center;">
                    {{ dim.counts[row.id] || 0 }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 趋势图: 情感随时间变化 -->
      <div v-if="trendData.length > 1" class="card">
        <div class="card-header">
          📈 情感趋势
          <span class="text-sm text-secondary" style="font-weight: normal;">
            （仅含有时间的 {{ trendTotal }} 条）
          </span>
        </div>
        <div ref="trendEl" class="trend-chart"></div>
      </div>

      <!-- 帖子详情列表 -->
      <div class="card" style="padding: 0; overflow-x: auto;">
        <div style="padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; border-bottom: 1px solid var(--border-light);">
          <div class="card-header" style="margin-bottom: 0; padding: 0;">📋 帖子情感详情</div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <!-- 情感过滤 -->
            <select v-model="filterSentiment" class="form-input" style="width: auto; padding: 4px 28px 4px 8px; font-size: 12px;">
              <option value="">全部情感</option>
              <option value="positive">正面</option>
              <option value="negative">负面</option>
              <option value="neutral">中立</option>
            </select>
            <!-- 维度过滤 -->
            <select v-model="filterDimension" class="form-input" style="width: auto; max-width: 140px; padding: 4px 28px 4px 8px; font-size: 12px;">
              <option value="">全部维度</option>
              <option v-for="d in allDimensions" :key="d" :value="d">{{ d }}</option>
            </select>
            <!-- 关键词搜索 -->
            <input
              v-model="filterKeyword"
              type="text"
              class="form-input"
              placeholder="搜索..."
              style="width: 120px; padding: 4px 8px; font-size: 12px;"
            />
            <button
              v-if="filterSentiment || filterDimension || filterKeyword"
              class="btn btn-outline btn-sm"
              @click="clearFilters"
            >清除</button>
            <span class="text-sm text-secondary" style="white-space: nowrap;">共 {{ filteredResults.length }} 条</span>
          </div>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th style="width: 44px; text-align: center;">#</th>
              <th style="width: 72px; text-align: center;">情感</th>
              <th style="width: 100px; text-align: center;">强度</th>
              <th>分析理由</th>
              <th style="width: 220px;">涉及维度</th>
              <th style="width: 64px; text-align: center;">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!filteredResults.length">
              <td colspan="6" class="text-center text-secondary" style="padding: 32px;">无匹配结果</td>
            </tr>
            <tr
              v-for="row in filteredResults"
              :key="row._idx"
            >
              <td style="text-align: center; font-size: 12px; color: var(--text-light);">{{ row._idx + 1 }}</td>
              <td style="text-align: center;">
                <span
                  :style="{
                    display: 'inline-block',
                    padding: '2px 10px',
                    borderRadius: '10px',
                    fontSize: '12px',
                    fontWeight: 600,
                    background: sentimentColor(row.result?.sentiment) + '18',
                    color: sentimentColor(row.result?.sentiment),
                  }"
                >
                  {{ sentimentLabel(row.result?.sentiment) }}
                </span>
              </td>
              <td style="text-align: center;">
                <span v-if="row.result?.sentiment" style="font-size: 13px; color: #F59E0B; letter-spacing: 1px;">{{ intensityStars(row.result.intensity) }}</span>
                <span v-else class="text-secondary">-</span>
              </td>
              <td style="font-size: 13px; max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                {{ row.result?.reason_cn || '-' }}
              </td>
              <td>
                <div v-if="row.result?.dimensions?.length" style="display: flex; gap: 3px; flex-wrap: wrap;">
                  <span
                    v-for="d in row.result.dimensions"
                    :key="d"
                    style="padding: 1px 6px; border-radius: 3px; font-size: 10px; white-space: nowrap;"
                    :style="{ background: (DIM_COLORS[d] || '#E2E8F0') + '28', color: DIM_COLORS[d] || '#64748B' }"
                  >{{ d }}</span>
                </div>
                <span v-else class="text-secondary" style="font-size: 11px;">-</span>
              </td>
              <td style="text-align: center;">
                <button class="btn btn-outline btn-sm" @click="viewPost(row._idx)">查看</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 详情弹窗 -->
    <div v-if="showDetail" class="modal-overlay" @click.self="showDetail = false">
      <div class="modal-content" style="max-width: 1000px; max-height: 85vh;" role="dialog" aria-modal="true" aria-labelledby="sentiment-post-title">
        <div class="modal-header">
          <h3 id="sentiment-post-title">📝 帖子 #{{ detailPost?.index }} — {{ detailPost?.username }}</h3>
          <button class="btn btn-sm btn-outline" @click="showDetail = false">✕</button>
        </div>

        <!-- 情感标签 -->
        <div v-if="detailPost?.sentiment" class="flex items-center gap-4 mb-4">
          <span
            :style="{
              padding: '4px 14px',
              borderRadius: '16px',
              fontSize: '14px',
              fontWeight: 700,
              background: sentimentColor(detailPost.sentiment.sentiment) + '18',
              color: sentimentColor(detailPost.sentiment.sentiment),
            }"
          >{{ sentimentLabel(detailPost.sentiment.sentiment) }}</span>
          <span class="text-secondary">强度:</span>
          <span style="font-size: 14px; color: #F59E0B;">{{ intensityStars(detailPost.sentiment.intensity) }}</span>
          <span class="text-secondary">{{ detailPost.sentiment.intensity }}/5</span>
          <span v-if="detailPost.sentiment.dimensions.length" class="flex gap-1" style="flex-wrap: wrap;">
            <span
              v-for="d in detailPost.sentiment.dimensions"
              :key="d"
              style="padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500;"
              :style="{ background: (DIM_COLORS[d] || '#E2E8F0') + '22', color: DIM_COLORS[d] || '#64748B' }"
            >{{ d }}</span>
          </span>
        </div>

        <!-- 分析理由 -->
        <div v-if="detailPost?.sentiment?.reason_cn" style="margin-bottom: 16px; padding: 10px 14px; background: #F8FAFC; border-radius: 8px; border-left: 3px solid #6366F1;">
          <span class="text-sm text-secondary">分析理由：</span>
          <span style="font-size: 14px; line-height: 1.7;">{{ detailPost.sentiment.reason_cn }}</span>
        </div>

        <!-- 原文 + 翻译 双栏 -->
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
          <div>
            <div class="form-label">📄 原文</div>
            <div style="white-space: pre-wrap; font-size: 13px; line-height: 1.7; max-height: 40vh; overflow-y: auto; padding: 12px; background: #F8FAFC; border-radius: 8px;">
              {{ detailPost?.content || '(无内容)' }}
            </div>
          </div>
          <div>
            <div class="form-label">🌐 中文翻译</div>
            <div style="white-space: pre-wrap; font-size: 13px; line-height: 1.7; max-height: 40vh; overflow-y: auto; padding: 12px; background: #F8FAFC; border-radius: 8px;">
              {{ detailPost?.translation || '(无翻译)' }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ECharts 按容器实际尺寸画布，高度必须由 CSS 给死，否则初始化时算出 0 */
.trend-chart {
  width: 100%;
  height: 280px;
}
</style>
