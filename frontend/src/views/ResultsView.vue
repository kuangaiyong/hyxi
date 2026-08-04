<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/composables/useToast'
import { downloadFile } from '@/utils/download'
import PostContent from '@/components/PostContent.vue'
import type { PostData } from '@/types/result'

const route = useRoute()
const router = useRouter()
const taskStore = useTaskStore()
const toast = useToast()
const taskId = computed(() => route.params.id as string)
const downloading = ref('')

const currentPage = ref(1)
const pageSize = 50
const searchText = ref('')
const isSearching = ref(false)

/** 双语 / 只看译文 / 只看原文 */
const viewMode = ref<'bilingual' | 'zh' | 'orig'>('bilingual')
/** 展开了全部回复的主贴（按 source+index 记） */
const openThreads = ref<Set<string>>(new Set())
const zoomUrl = ref('')

// 一个主贴默认只展开这么多条回复，其余折叠 —— 十几条回复平铺会把整页撑开
const REPLY_PREVIEW = 3

const totalPages = computed(() =>
  Math.ceil(taskStore.postsTotal / taskStore.pageSize)
)

const threadKey = (p: PostData) => `${p.source}:${p.index}`

/**
 * 后端按主贴分页、评论挂在 replies 里。这里把每个主贴的后代**展平成带层级的列表**，
 * 卡片内用缩进渲染 —— 比递归组件简单，而嵌套最多也就两三层。
 */
const threads = computed(() =>
  taskStore.posts.map((root) => {
    const replies: PostData[] = []
    const walk = (p: PostData) => {
      ;(p.replies || []).forEach((c) => {
        replies.push(c)
        walk(c)
      })
    }
    walk(root)
    return { root, replies }
  })
)

function visibleReplies(t: { root: PostData; replies: PostData[] }): PostData[] {
  if (openThreads.value.has(threadKey(t.root))) return t.replies
  // 搜索命中的评论必须露出来，否则用户搜到了却看不见
  if (t.replies.some((r) => r.matched)) return t.replies
  return t.replies.slice(0, REPLY_PREVIEW)
}

function toggleThread(root: PostData) {
  const key = threadKey(root)
  const next = new Set(openThreads.value)
  next.has(key) ? next.delete(key) : next.add(key)
  openThreads.value = next
}

onMounted(async () => {
  taskStore.currentTaskId = taskId.value
  await taskStore.fetchResults()
  await taskStore.fetchTask(taskId.value)
})

async function handleSearch() {
  isSearching.value = true
  try {
    await taskStore.fetchResults(searchText.value, 1)
  } finally {
    isSearching.value = false
  }
}

function handleSearchClear() {
  searchText.value = ''
  taskStore.fetchResults('', 1)
}

function goToPage(p: number) {
  if (p < 1 || p > totalPages.value) return
  taskStore.fetchResults(searchText.value, p)
}

async function handleDownload(kind: 'excel' | 'csv' | 'json') {
  const targets = {
    excel: { url: taskStore.getDownloadUrl(), name: `任务结果_${taskId.value}.xlsx` },
    csv: { url: `/api/v1/tasks/${taskId.value}/export/csv`, name: `任务结果_${taskId.value}.csv` },
    json: { url: `/api/v1/tasks/${taskId.value}/export/json`, name: `任务结果_${taskId.value}.json` },
  }
  downloading.value = kind
  try {
    await downloadFile(targets[kind].url, targets[kind].name)
  } catch (e: any) {
    toast.error('下载失败: ' + (e?.message || '网络错误'))
  } finally {
    downloading.value = ''
  }
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
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-outline"
            @click="router.push(`/tasks/${taskId}/sentiment`)"
          >
            📊 舆情分析
          </button>
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-success"
            :disabled="!!downloading"
            @click="handleDownload('excel')"
          >
            {{ downloading === 'excel' ? '下载中...' : '📥 Excel' }}
          </button>
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-outline btn-sm"
            :disabled="!!downloading"
            @click="handleDownload('csv')"
          >
            {{ downloading === 'csv' ? '...' : 'CSV' }}
          </button>
          <button
            v-if="taskStore.isCompleted"
            class="btn btn-outline btn-sm"
            :disabled="!!downloading"
            @click="handleDownload('json')"
          >
            {{ downloading === 'json' ? '...' : 'JSON' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 加载失败：帖子拿到了就不该再报错，否则 allSettled 保住的数据又被红卡盖掉 -->
    <div v-if="taskStore.resultsError && !taskStore.posts.length" class="card text-center" style="padding: 32px;">
      <div style="font-size: 40px; margin-bottom: 12px;">⚠️</div>
      <p class="text-secondary mb-4">{{ taskStore.resultsError }}</p>
      <button class="btn btn-primary" @click="taskStore.fetchResults(searchText, taskStore.currentPage)">重试</button>
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
    <div v-if="taskStore.posts.length" class="card" style="padding: 0; overflow-x: auto;">
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

      <!-- 线程卡片：一个主贴一张卡，评论缩进挂在下面。
           分页粒度是主贴，所以一个主贴的评论不会被切在两页之间 -->
      <div class="thread-list">
        <article
          v-for="t in threads"
          :key="threadKey(t.root)"
          class="thread"
          :class="{ hit: t.root.matched }"
        >
          <header class="thread-head">
            <span class="badge-source">{{ t.root.source_name }}</span>
            <strong>{{ t.root.username }}</strong>
            <span class="text-sm text-secondary">{{ t.root.timestamp }}</span>
            <span class="grow" />
            <span class="text-sm text-secondary">#{{ t.root.index }}</span>
            <span v-if="t.replies.length" class="reply-count">💬 {{ t.replies.length }}</span>
          </header>

          <PostContent :post="t.root" :mode="viewMode" @zoom="zoomUrl = $event" />

          <div v-if="t.replies.length" class="replies">
            <div
              v-for="r in visibleReplies(t)"
              :key="threadKey(r)"
              class="reply"
              :class="{ hit: r.matched }"
              :style="{ marginLeft: Math.min(r.reply_level - 1, 3) * 18 + 'px' }"
            >
              <div class="reply-head">
                <strong>{{ r.username }}</strong>
                <span class="text-sm text-secondary">{{ r.timestamp }}</span>
                <span class="grow" />
                <span class="text-sm text-secondary">#{{ r.index }}</span>
              </div>
              <PostContent :post="r" :mode="viewMode" @zoom="zoomUrl = $event" />
            </div>

            <button
              v-if="t.replies.length > REPLY_PREVIEW && !t.replies.some((r) => r.matched)"
              class="more-replies"
              @click="toggleThread(t.root)"
            >
              {{ openThreads.has(threadKey(t.root))
                ? '收起回复 ▴'
                : `展开其余 ${t.replies.length - REPLY_PREVIEW} 条回复 ▾` }}
            </button>
          </div>
        </article>
      </div>

      <!-- 分页控件 -->
      <div class="flex items-center justify-between" style="padding: 12px 16px; border-top: 1px solid var(--border-light);">
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
.thread {
  border: 1px solid var(--border-light);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--bg-card, transparent);
}
.thread.hit,
.reply.hit {
  background: var(--warning-bg, #fef3c7);
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
.badge-source {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--border-light);
  color: var(--text-secondary);
}
.reply-count {
  font-size: 12px;
  color: var(--text-secondary);
}
.replies {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border-light);
}
/* 左侧竖线是层级的主要信号：光靠缩进，行一多就分不出谁回复谁 */
.reply {
  border-left: 2px solid var(--border-light);
  padding: 6px 0 6px 10px;
  margin-bottom: 6px;
}
.more-replies {
  border: none;
  background: none;
  padding: 4px 0 0 10px;
  cursor: pointer;
  font-size: 12px;
  color: var(--primary);
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
</style>
