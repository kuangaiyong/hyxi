<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '@/stores/task'
import PostContent from '@/components/PostContent.vue'
import * as resultsApi from '@/api/results'
import * as sentimentApi from '@/api/sentiment'
import type { PostData } from '@/types/result'
import type { SentimentResult } from '@/types/sentiment'

const route = useRoute()
const router = useRouter()
const taskStore = useTaskStore()
const taskId = computed(() => route.params.id as string)

const currentPage = ref(1)
const pageSize = 50
const searchText = ref('')
// 「老帖新回复」：主贴按时间倒序排，今天发在两个月前主贴上的回复会被排到两个月前的
// 位置去，用户根本翻不到。这个开关把它们挑出来。
// **筛选在后端做**：一页只有 50 个主贴，在前端筛只能筛出当前页里的 —— 而它们恰恰
// 就在后面几页，等于什么都没做。
const FRESH_DAYS_CHOICES = [3, 7, 14]
const freshDays = ref(7)
const onlyFresh = ref(false)
const isSearching = ref(false)

/** 双语 / 只看译文 / 只看原文 */
const viewMode = ref<'bilingual' | 'zh' | 'orig'>('bilingual')
const zoomUrl = ref('')

const totalPages = computed(() =>
  Math.ceil(taskStore.postsTotal / taskStore.pageSize)
)

/** 当前有没有生效的筛选条件。
 *
 *  筛出 0 条时**必须**照样把搜索栏渲染出来 —— 它自己就是关掉筛选的唯一入口。
 *  整张卡片（搜索栏 + 列表 + 分页）本来一起挂在 posts.length 上，于是「只看新回复
 *  + 近 3 天」一旦没命中，用户看到的是半页空白，连开关都找不回来，只能刷新。 */
const hasFilter = computed(() => searchText.value.trim() !== '' || onlyFresh.value)

const threadKey = (p: PostData) => `${p.source}:${p.index}`

// 早期采集读不到 tooltip 的绝对时间，落盘就是空的（写相对时间会污染指纹）。
// 留一段空白看着像功能坏了，明说没有反而清楚。
const postTime = (p: PostData) => p.timestamp?.trim() || '时间未知'

/** 舆情结论，按扁平下标存：results[i] 对应 index 为 i+1 的那条帖子 */
const sentimentResults = ref<(SentimentResult | null)[]>([])

const SENTIMENT_LABEL: Record<string, string> = {
  positive: '正面', negative: '负面', neutral: '中立',
}

/**
 * 这条帖子的舆情结论；没分析过、或分析失败（sentiment 为 null）都返回 null。
 *
 * index 是**扁平存储数组里的绝对位置**（1-based），主贴和评论共用同一套编号，
 * 所以这个函数对两者一视同仁 —— 结论本来就是逐条给的，评论也占位。
 */
function sentimentOf(p: PostData): SentimentResult | null {
  const r = sentimentResults.value[p.index - 1]
  return r && r.sentiment ? r : null
}

/** 跳到舆情页并定位到这条帖子。带 index 过去，那边负责滚动 + 高亮 + 开详情 */
function viewSentiment(p: PostData) {
  router.push({
    path: `/tasks/${taskId.value}/sentiment`,
    query: { post: String(p.index) },
  })
}

/**
 * 后端按主贴分页、评论挂在 replies 里。这里把每个主贴的后代**按原帖顺序展平成带层级的列表**
 * （先序：评论 → 它的回复 → 回复的回复 → 下一条评论），卡片内用缩进渲染 —— 和 Facebook
 * 浮层里的样子一致，比递归组件简单。
 *
 * **全部展开，不折叠**（v1.12.0 用户定的口径）：用户是拿原帖对着看「采全了没有」的，
 * 藏掉一截就对不上了。以前「只显示前 3 条」还先后为搜索命中、老帖新回复各开过一次豁免。
 */
const threads = computed(() =>
  taskStore.posts.map((root) => {
    const replies: { post: PostData; parentName: string; descendants: number }[] = []
    const walk = (p: PostData) => {
      ;(p.replies || []).forEach((c) => {
        const row = { post: c, parentName: p.username, descendants: 0 }
        replies.push(row)
        const before = replies.length
        walk(c)
        row.descendants = replies.length - before
      })
    }
    walk(root)
    return { root, replies }
  })
)

/** 原帖上的评论数（含回复的回复）比采到的多 —— 可能没采全 */
const isShort = (t: { root: PostData; replies: unknown[] }) =>
  t.root.site_comment_count != null && t.replies.length < t.root.site_comment_count

// ===== 补译：统计栏下面那条提示条 =====
// 「还缺译文」的条数由后端一处算（needs_translation），前端只显示 —— 各算一份迟早对不上。
// 进度走 SSE，另每 10 秒拉一次 /stats 兜底：翻译可能是共用同一来源的别的任务发起的，
// 本任务的频道上收不到任何事件，只有 /stats 的 translating 看得见。
const backfillRunning = ref(false)
const backfillDone = ref(0)
const backfillTotal = ref(0)
const backfillError = ref('')
let translateSource: EventSource | null = null
let statsTimer: number | null = null

const untranslatedCount = computed(() => taskStore.stats?.untranslated_count ?? 0)
/** 任务没跑完（它自己的翻译步骤还会翻），或者没有要翻的也没在翻，就不显示这条 */
const showBackfillBar = computed(() =>
  taskStore.isCompleted && (untranslatedCount.value > 0 || backfillRunning.value)
)

function stopWatchingBackfill() {
  translateSource?.close()
  translateSource = null
  if (statsTimer !== null) {
    clearInterval(statsTimer)
    statsTimer = null
  }
}

/** 补译结束（不管是谁翻的）：帖子和统计都要重拉，N 归零提示条自己就消失了 */
async function finishBackfill(err = '') {
  stopWatchingBackfill()
  backfillRunning.value = false
  backfillError.value = err
  await reload(taskStore.currentPage)
}

function watchBackfill(total: number) {
  stopWatchingBackfill()
  backfillRunning.value = true
  backfillError.value = ''
  backfillDone.value = 0
  backfillTotal.value = total

  const es = new EventSource(resultsApi.getTranslationEventsUrl(taskId.value))
  translateSource = es
  es.addEventListener('translation_progress', (e: MessageEvent) => {
    const d = JSON.parse(e.data)
    backfillDone.value = d.done || 0
    if (d.total) backfillTotal.value = d.total
  })
  es.addEventListener('translation_complete', (e: MessageEvent) => {
    const d = JSON.parse(e.data)
    finishBackfill(d.status === 'completed' ? '' : d.error || '补译失败')
  })
  // 连接层错误不 close：EventSource 自己会重连，下面的轮询也兜得住

  statsTimer = window.setInterval(async () => {
    try {
      const stats = await resultsApi.fetchStats(taskId.value)
      if (!stats.translating) await finishBackfill()
    } catch {
      /* 后端暂时不可达，下一轮再看 */
    }
  }, 10000)
}

async function startBackfill() {
  if (backfillRunning.value) return
  backfillError.value = ''
  try {
    const body = await resultsApi.triggerBackfillTranslation(taskId.value)
    if (body.status === 'started' || body.status === 'running') {
      watchBackfill(body.pending_count ?? untranslatedCount.value)
    } else {
      await reload(taskStore.currentPage)   // completed：没有要翻的，刷新一下让提示条消失
    }
  } catch (e: any) {
    // 模型没配置这类前置条件，后端在起作业之前就回 400；原文照显，用户才知道去哪配
    backfillError.value = e?.response?.data?.detail || e?.message || '发起补译失败'
  }
}

onUnmounted(stopWatchingBackfill)

onMounted(async () => {
  taskStore.currentTaskId = taskId.value
  await reload()
  // 进页面时来源已经在翻了（别的标签页点的、或共用来源的任务在翻）：直接接上进度
  if (taskStore.stats?.translating) watchBackfill(untranslatedCount.value)
  await taskStore.fetchTask(taskId.value)
  // 舆情是附加信息，没分析过、正在分析、请求失败都只是不显示按钮，
  // 不能让它拖累帖子列表本身
  try {
    const data = await sentimentApi.getSentiment(taskId.value)
    if (data?.results) sentimentResults.value = data.results
  } catch {
    /* 静默 */
  }
})

function reload(page = 1) {
  return taskStore.fetchResults(searchText.value, page, freshDays.value, onlyFresh.value)
}

async function handleSearch() {
  isSearching.value = true
  try {
    await reload()
  } finally {
    isSearching.value = false
  }
}

function handleSearchClear() {
  searchText.value = ''
  reload()
}

/** 开关和窗口都要回到第 1 页：过滤后 total 变了，停在第 5 页会落到空页上 */
function toggleOnlyFresh() {
  onlyFresh.value = !onlyFresh.value
  reload()
}

function changeFreshDays(days: number) {
  if (days === freshDays.value) return
  freshDays.value = days
  reload()
}

function goToPage(p: number) {
  if (p < 1 || p > totalPages.value) return
  reload(p)
}

function getStatusText(): string {
  const status = taskStore.currentTask?.status
  const map: Record<string, string> = {
    completed: '已完成',
    failed: '失败',
    running: '执行中',
    pending: '等待中',
    parsing: '解析中',
    cancelled: '已取消',
  }
  return map[status || ''] || status || '未知'
}
</script>

<template>
  <div>
    <!-- 任务状态 -->
    <div class="card">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-4">
          <span
            v-if="taskStore.currentTask"
            class="badge"
            :class="'badge-' + taskStore.currentTask.status"
          >
            {{ getStatusText() }}
          </span>
          <span class="text-sm text-secondary">
            {{ taskStore.currentTask?.description }}
          </span>
        </div>
        <div class="flex gap-2">
          <!-- 导出口只有舆情页一处，这里是过去的路 -->
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-outline"
            @click="router.push(`/tasks/${taskId}/sentiment`)"
          >
            📊 舆情分析与导出
          </button>
        </div>
      </div>
    </div>

    <!-- 加载失败：帖子拿到了就不该再报错，否则 allSettled 保住的数据又被红卡盖掉 -->
    <div v-if="taskStore.resultsError && !taskStore.posts.length" class="card text-center" style="padding: 32px;">
      <div style="font-size: 40px; margin-bottom: 12px;">⚠️</div>
      <p class="text-secondary mb-4">{{ taskStore.resultsError }}</p>
      <button class="btn btn-primary" @click="reload(taskStore.currentPage)">重试</button>
    </div>

    <!-- 统计 -->
    <div v-if="taskStore.stats" class="stats-grid">
      <div class="stat-card">
        <div class="stat-value">{{ taskStore.stats.total_posts }}</div>
        <div class="stat-label">总帖子数</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ taskStore.stats.unique_users }}</div>
        <div class="stat-label">唯一用户</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ taskStore.stats.total_pages }}</div>
        <div class="stat-label">总页数</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-sm" style="font-size: 12px; word-break: break-all;">
          {{ taskStore.stats.time_range_start || 'N/A' }}
        </div>
        <div class="stat-label">时间开始</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-sm" style="font-size: 12px; word-break: break-all;">
          {{ taskStore.stats.time_range_end || 'N/A' }}
        </div>
        <div class="stat-label">时间结束</div>
      </div>
    </div>

    <!-- 补译：采集留下的没译文的帖子，在这里一键补齐（只翻缺的，同舆情页的增量分析） -->
    <div v-if="showBackfillBar" class="card backfill-bar" data-testid="backfill-bar">
      <div class="flex items-center justify-between gap-4">
        <div>
          <span v-if="backfillRunning" class="text-sm" data-testid="backfill-status">
            ⏳ 正在补译 {{ backfillDone }} / {{ backfillTotal }}
          </span>
          <span v-else class="text-sm" data-testid="backfill-status">
            📝 有 {{ untranslatedCount }} 条帖子还没有译文
          </span>
          <p v-if="backfillError" class="text-sm backfill-error" data-testid="backfill-error">
            ⚠️ {{ backfillError }}
          </p>
        </div>
        <button
          class="btn btn-primary"
          data-testid="backfill-start"
          :disabled="backfillRunning"
          @click="startBackfill"
        >
          {{ backfillRunning ? '正在补译…' : `翻译这 ${untranslatedCount} 条` }}
        </button>
      </div>
    </div>

    <!-- 活跃用户 -->
    <div v-if="taskStore.stats?.top_users?.length" class="card">
      <div class="card-header">👥 活跃用户 Top 10</div>
      <div class="flex gap-2" style="flex-wrap: wrap;">
        <span
          v-for="(user, idx) in taskStore.stats.top_users"
          :key="idx"
          style="padding: 4px 10px; background: var(--border-light); border-radius: 16px; font-size: 12px;"
        >
          <strong>{{ user.username }}</strong>
          <span class="text-secondary"> ({{ user.count }})</span>
        </span>
      </div>
    </div>

    <!-- 帖子表格 -->
    <div v-if="taskStore.posts.length || hasFilter" class="card" style="padding: 0; overflow-x: auto;">
      <!-- 搜索栏 -->
      <div class="flex items-center gap-2" style="padding: 12px 16px; border-bottom: 1px solid var(--border-light);">
        <div class="flex items-center gap-2" style="flex: 1; max-width: 400px;">
          <input
            v-model="searchText"
            type="text"
            class="form-input"
            placeholder="搜索用户名、原文或翻译..."
            style="padding: 6px 10px; font-size: 13px;"
            @keyup.enter="handleSearch"
          />
          <button class="btn btn-primary btn-sm" @click="handleSearch" :disabled="isSearching">
            {{ isSearching ? '搜索中...' : '搜索' }}
          </button>
          <button
            v-if="searchText"
            class="btn btn-outline btn-sm"
            @click="handleSearchClear"
          >清除</button>
        </div>
        <div class="flex items-center gap-2">
          <button
            class="btn btn-sm fresh-toggle"
            :class="{ on: onlyFresh }"
            title="只看老主贴上的新回复 —— 它们因为主贴时间倒序被压在后面几页"
            @click="toggleOnlyFresh"
          >🔥 只看新回复</button>
          <div v-if="onlyFresh" class="fresh-days">
            <button
              v-for="d in FRESH_DAYS_CHOICES"
              :key="d"
              :class="{ active: d === freshDays }"
              @click="changeFreshDays(d)"
            >近 {{ d }} 天</button>
          </div>
          <div class="mode-switch">
            <button
              v-for="m in ([['bilingual', '双语'], ['zh', '只看译文'], ['orig', '只看原文']] as const)"
              :key="m[0]"
              :class="{ active: viewMode === m[0] }"
              @click="viewMode = m[0]"
            >{{ m[1] }}</button>
          </div>
          <span class="text-sm text-secondary">共 {{ taskStore.postsTotal }} 个主贴</span>
        </div>
      </div>

      <!-- 筛完一条不剩：说清楚是被什么条件筛掉的，别让用户以为数据没了 -->
      <div v-if="!taskStore.posts.length" class="empty-filtered">
        <div class="icon">🔍</div>
        <p v-if="onlyFresh && searchText.trim()">
          近 {{ freshDays }} 天内没有匹配「{{ searchText.trim() }}」的新回复
        </p>
        <p v-else-if="onlyFresh">近 {{ freshDays }} 天内没有老帖收到新回复</p>
        <p v-else>没有匹配「{{ searchText.trim() }}」的帖子</p>
        <p class="hint">
          {{ onlyFresh ? '换个时间窗口，或关掉「只看新回复」看全部帖子' : '换个关键词，或点「清除」看全部帖子' }}
        </p>
      </div>

      <!-- 线程卡片：一个主贴一张卡，评论缩进挂在下面。
           分页粒度是主贴，所以一个主贴的评论不会被切在两页之间 -->
      <div v-else class="thread-list">
        <article
          v-for="t in threads"
          :key="threadKey(t.root)"
          class="thread"
          :class="{ hit: t.root.matched }"
        >
          <header class="thread-head">
            <!-- 排在树根的不一定是主贴：父贴没采到的回复也显示在这里（存储层只清父指针、
                 不再抹平 reply_level，见存储红线）。标成「主贴」会让人以为原帖就长这样，
                 而它恰恰是**唯一没有「🔗 原帖」链接**的那种卡片 —— 徽标得说清为什么 -->
            <span
              class="badge-role"
              :class="{ 'is-orphan': t.root.reply_level > 0 }"
              :title="t.root.reply_level > 0
                ? '这是一条回复，它的主贴没有采到，所以单独显示在这里；也因此没有原帖链接'
                : ''"
            >{{ t.root.reply_level > 0 ? '回复（主贴缺失）' : '主贴' }}</span>
            <span class="badge-source">{{ t.root.source_name }}</span>
            <strong class="root-user">{{ t.root.username }}</strong>
            <span class="text-sm text-secondary">{{ postTime(t.root) }}</span>
            <span class="grow" />
            <button
              v-if="sentimentOf(t.root)"
              class="sentiment-chip"
              :class="'is-' + sentimentOf(t.root)!.sentiment"
              :title="`查看舆情分析详情：${sentimentOf(t.root)!.reason_cn}`"
              @click="viewSentiment(t.root)"
            >
              📊 {{ SENTIMENT_LABEL[sentimentOf(t.root)!.sentiment!] }} ›
            </button>
            <!-- 原帖链接。在用户自己的浏览器里打开，用的就是他本人的登录态；
                 没登录时 Facebook 会自己带着这个目标走一遍登录再跳回原贴 ——
                 所以这里不需要、也不该做任何自动登录。
                 rel 两个值都必要：noopener 断掉新页面对 window.opener 的引用，
                 noreferrer 不把本机的内网地址带到外站去 -->
            <a
              v-if="t.root.source_url"
              class="source-link"
              :href="t.root.source_url"
              target="_blank"
              rel="noopener noreferrer"
              :title="`在新标签页打开原帖：${t.root.source_url}`"
              data-testid="source-link"
            >🔗 原帖</a>
            <span class="text-sm text-secondary">#{{ t.root.index }}</span>
            <!-- 采集时读到了原帖上的评论数，就摆出来对账：用户判断「采全了没有」靠的就是它。
                 对不上标黄，但只说「可能不全」—— 原帖那个数字也算已删除 / 被隐藏的评论 -->
            <span
              v-if="t.root.site_comment_count != null"
              class="reply-count completeness"
              :class="{ short: isShort(t) }"
              :title="isShort(t)
                ? `原帖显示 ${t.root.site_comment_count} 条评论与回复，这里采到 ${t.replies.length} 条，可能不全`
                  + '（原帖的数字也算已删除或被隐藏的评论；账号的评论排序是「最相关」时，疑似垃圾评论也看不到）'
                : `原帖显示 ${t.root.site_comment_count} 条评论与回复，已全部采到`"
              data-testid="completeness"
            >已采 {{ t.replies.length }} · 原帖 {{ t.root.site_comment_count }}</span>
            <span v-else-if="t.replies.length" class="reply-count">💬 {{ t.replies.length }}</span>
            <!-- 这条主贴很旧，但下面有新回复。按主贴时间倒序排的话它会沉到下面去，
                 徽标是用户在列表里唯一能看出「这里有新动静」的东西 -->
            <span
              v-if="t.root.fresh_reply_count"
              class="fresh-count-badge"
              :title="`这个讨论串上有 ${t.root.fresh_reply_count} 条新回复`"
            >🔥 {{ t.root.fresh_reply_count }} 条新回复</span>
          </header>

          <PostContent :post="t.root" :mode="viewMode" @zoom="zoomUrl = $event" />

          <div v-if="t.replies.length" class="replies">
            <div class="replies-label">💬 {{ t.replies.length }} 条评论与回复</div>
            <!-- 缩进随层级递增：评论一层，回复挂在它回复的那条下面再缩一层，和原帖浮层一样 -->
            <div
              v-for="{ post: r, parentName, descendants } in t.replies"
              :key="threadKey(r)"
              class="reply"
              :class="{ hit: r.matched, fresh: r.fresh_reply }"
              :style="{ marginLeft: Math.min(r.reply_level - 1, 3) * 18 + 'px' }"
            >
              <div class="reply-head">
                <span class="reply-arrow" aria-hidden="true">↳</span>
                <span class="reply-user">{{ r.username }}</span>
                <!-- 第 2 层起写明回的是谁 —— 缩进一深，光靠对齐很难看出挂在哪条下面 -->
                <span v-if="r.reply_level >= 2" class="reply-to" data-testid="reply-to">回复 {{ parentName }}</span>
                <span
                  v-if="r.fresh_reply"
                  class="fresh-tag"
                  :title="`老主贴上的新回复：主贴发表于 ${r.days_since_root} 天前`"
                >🔥 新回复 · 主贴 {{ r.days_since_root }} 天前</span>
                <span class="text-sm text-secondary">{{ postTime(r) }}</span>
                <span class="grow" />
                <span v-if="r.reply_level === 1 && descendants" class="reply-count">{{ descendants }} 条回复</span>
                <button
                  v-if="sentimentOf(r)"
                  class="sentiment-chip"
                  :class="'is-' + sentimentOf(r)!.sentiment"
                  :title="`查看舆情分析详情：${sentimentOf(r)!.reason_cn}`"
                  @click="viewSentiment(r)"
                >
                  📊 {{ SENTIMENT_LABEL[sentimentOf(r)!.sentiment!] }} ›
                </button>
                <span class="text-sm text-secondary">#{{ r.index }}</span>
              </div>
              <PostContent :post="r" :mode="viewMode" @zoom="zoomUrl = $event" />
            </div>
          </div>
        </article>
      </div>

      <!-- 分页控件。一条都没有时不渲染，否则会显示成「第 1/0 页」 -->
      <div
        v-if="taskStore.posts.length"
        class="flex items-center justify-between"
        style="padding: 12px 16px; border-top: 1px solid var(--border-light);"
      >
        <span class="text-sm text-secondary">
          第 {{ taskStore.currentPage }}/{{ totalPages }} 页，共 {{ taskStore.postsTotal }} 条
        </span>
        <div class="flex gap-1" v-if="totalPages > 1">
          <button class="btn btn-outline btn-sm" :disabled="taskStore.currentPage <= 1" @click="goToPage(1)">«</button>
          <button class="btn btn-outline btn-sm" :disabled="taskStore.currentPage <= 1" @click="goToPage(taskStore.currentPage - 1)">‹</button>
          <template v-for="p in totalPages" :key="p">
            <button
              v-if="p === 1 || p === totalPages || Math.abs(p - taskStore.currentPage) <= 2"
              class="btn btn-sm"
              :class="p === taskStore.currentPage ? 'btn-primary' : 'btn-outline'"
              @click="goToPage(p)"
            >{{ p }}</button>
            <span v-else-if="Math.abs(p - taskStore.currentPage) === 3" class="text-secondary">…</span>
          </template>
          <button class="btn btn-outline btn-sm" :disabled="taskStore.currentPage >= totalPages" @click="goToPage(taskStore.currentPage + 1)">›</button>
          <button class="btn btn-outline btn-sm" :disabled="taskStore.currentPage >= totalPages" @click="goToPage(totalPages)">»</button>
        </div>
      </div>
    </div>

    <!-- 空状态 -->
    <div v-if="!taskStore.posts.length && !taskStore.resultsError && !taskStore.isCompleted" class="empty-state">
      <div class="icon">📭</div>
      <p>暂无结果数据，等待任务完成...</p>
    </div>

    <!-- 图片灯箱。原来的帖子详情弹窗去掉了：卡片里已经是全文 + 双语，
         再点开一个弹窗看同样的内容没有意义 -->
    <div v-if="zoomUrl" class="lightbox" @click="zoomUrl = ''">
      <img :src="zoomUrl" alt="帖子配图（放大）" />
    </div>
  </div>
</template>

<style scoped>
.thread-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 16px 0;
}
/* 主贴 vs 回复的区分靠四路信号叠加，缺一路都会有人看不出来：
   ①「主贴」文字徽标 ② 主贴左侧的主色粗竖条 ③ 回复整块下沉成一个内嵌面板
   ④ 每条回复前的 ↳（与导出 Excel 的 └─ 前缀同一个语义）。
   **不能只靠边框颜色**：深色主题下 --border-light 恰好等于 --bg-card，
   改造前回复那条 2px 竖线在深色下完全看不见。 */
.thread-list {
  --reply-panel: #F1F5F9;
  --reply-rail: #94A3B8;
}
[data-theme="dark"] .thread-list {
  --reply-panel: #172033;
  --reply-rail: #475569;
}
.thread {
  border: 1px solid var(--border);
  border-left: 4px solid var(--primary);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--bg-card, transparent);
}
.thread-head,
.reply-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.grow {
  flex: 1;
}
.badge-role {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--primary);
  color: #fff;
  letter-spacing: 0.5px;
}
/* 父贴缺失的那种：换个不抢眼的底色，一眼能和真主贴区分开 */
.badge-role.is-orphan {
  background: var(--text-secondary);
}
.root-user {
  font-size: 15px;
}
.badge-source {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--border-light);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}
.reply-count {
  font-size: 12px;
  color: var(--text-secondary);
}
/* 已采 X · 原帖 Y。对不上时用和「老帖新回复」同一套暖色：要人多看一眼，但不是报错 */
.completeness {
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  white-space: nowrap;
}
.completeness.short {
  background: #FEF3C7;
  border-color: #F59E0B;
  color: #92400E;
}
.reply-to {
  font-size: 12px;
  color: var(--text-secondary);
}
/* 原帖链接。做成和舆情徽标同一个量级的小胶囊，而不是一眼就抢注意力的主按钮 ——
   它是「需要时点一下」的辅助入口，不是这一页的主操作 */
.source-link {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  text-decoration: none;
  white-space: nowrap;
}
.source-link:hover {
  color: var(--primary);
  border-color: var(--primary);
}
/* 情感标签本身就是入口：既让人一眼看到结论，又不用再多摆一个「查看」按钮。
   主贴和评论共用同一个样式 —— 结论是逐条给的，评论也有自己那份 */
.sentiment-chip {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  border: 1px solid currentColor;
  background: transparent;
  cursor: pointer;
  white-space: nowrap;
  opacity: 0.9;
}
.sentiment-chip:hover {
  opacity: 1;
  filter: brightness(1.15);
}
.sentiment-chip.is-positive { color: #10B981; }
.sentiment-chip.is-negative { color: #EF4444; }
.sentiment-chip.is-neutral { color: #94A3B8; }
/* 回复整块缩进 + 换底色：一眼就能看出它从属于上面那张卡，而不是并列的另一条帖子 */
.replies {
  margin: 12px 0 0 12px;
  padding: 8px 12px 10px;
  background: var(--reply-panel);
  border-left: 3px solid var(--reply-rail);
  border-radius: 0 6px 6px 0;
}
.replies-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.reply {
  border-left: 2px solid var(--reply-rail);
  padding: 6px 0 6px 10px;
  margin-bottom: 6px;
}
.reply-arrow {
  color: var(--reply-rail);
  font-weight: 700;
}
.reply-user {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}
/* 搜索命中的高亮压在层级配色之上，否则回复面板的底色会把它盖掉 */
.thread.hit,
.reply.hit {
  background: var(--warning-bg, #fef3c7);
}
[data-theme="dark"] .thread.hit,
[data-theme="dark"] .reply.hit {
  background: #4A3A12;
}
.mode-switch {
  display: inline-flex;
  border: 1px solid var(--border-light);
  border-radius: 6px;
  overflow: hidden;
}
.mode-switch button {
  border: none;
  background: none;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  color: var(--text-secondary);
}
.mode-switch button.active {
  background: var(--primary);
  color: #fff;
}
.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  cursor: zoom-out;
}
.lightbox img {
  max-width: 92vw;
  max-height: 92vh;
  object-fit: contain;
}

/* ===== 老主贴上的新回复 =====
   页面上的展示只在这里（舆情页那套面板已整体拆除）。颜色与 Excel 报告里的暖色
   一致（#F59E0B / #B45309），页面和报告看到的是同一个视觉信号 */
/* 筛完一条不剩时的占位。搜索栏在它上方照常渲染 ——
   那是关掉筛选的唯一入口 */
.empty-filtered {
  text-align: center;
  padding: 48px 24px;
  color: var(--text-secondary);
}
.empty-filtered .icon { font-size: 40px; margin-bottom: 12px; opacity: 0.45; }
.empty-filtered p { margin: 0; font-size: 14px; }
.empty-filtered .hint { margin-top: 8px; font-size: 12px; opacity: 0.75; }

.fresh-toggle {
  border: 1px solid var(--border);
  color: var(--text-secondary);
  white-space: nowrap;
}
.fresh-toggle.on { background: #F59E0B; color: #fff; border-color: #F59E0B; }
.fresh-days { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.fresh-days button {
  padding: 4px 10px;
  font-size: 12px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}
.fresh-days button.active { background: #F59E0B; color: #fff; font-weight: 600; }

.fresh-count-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(245, 158, 11, 0.15);
  color: #B45309;
  white-space: nowrap;
}
.reply.fresh {
  border-left: 3px solid #F59E0B;
  background: rgba(245, 158, 11, 0.07);
}
.fresh-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #FDE68A;
  color: #92400E;
  white-space: nowrap;
}
.backfill-bar {
  border-left: 3px solid var(--primary);
}
.backfill-error {
  margin: 4px 0 0;
  color: var(--error);
}
</style>
