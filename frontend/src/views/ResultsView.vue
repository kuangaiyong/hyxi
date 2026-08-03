<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/composables/useToast'
import { downloadFile } from '@/utils/download'
import type { PostData } from '@/types/result'

const route = useRoute()
const router = useRouter()
const taskStore = useTaskStore()
const toast = useToast()
const taskId = computed(() => route.params.id as string)
const downloading = ref('')

const currentPage = ref(1)
const pageSize = 50
const showDetail = ref(false)
const detailPost = ref<any>(null)
const searchText = ref('')
const isSearching = ref(false)

const totalPages = computed(() =>
  Math.ceil(taskStore.postsTotal / taskStore.pageSize)
)

/** 后端按主贴分页并把评论挂在 replies 里，表格渲染成一行一条，靠 reply_level 缩进 */
const flatPosts = computed(() => {
  const out: PostData[] = []
  const walk = (p: PostData) => {
    out.push(p)
    ;(p.replies || []).forEach(walk)
  }
  taskStore.posts.forEach(walk)
  return out
})

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

function viewDetail(post: any) {
  detailPost.value = post
  showDetail.value = true
}

function formatContent(text: string, maxLen = 200): string {
  if (!text) return ''
  return text.length > maxLen ? text.slice(0, maxLen) + '...' : text
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
        <span class="text-sm text-secondary">共 {{ taskStore.postsTotal }} 个主贴</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th class="col-narrow">#</th>
            <th class="col-medium">来源</th>
            <th class="col-medium">用户</th>
            <th class="col-medium">时间</th>
            <th class="col-wide">原文</th>
            <th class="col-wide">中文翻译</th>
            <th class="col-narrow">页</th>
          </tr>
        </thead>
        <tbody>
          <!-- 评论跟着父贴走：分页粒度是主贴，一个主贴的评论不会被切在两页之间 -->
          <template v-for="post in flatPosts" :key="post.source + post.index">
            <tr
              @click="viewDetail(post)"
              style="cursor: pointer;"
              :style="{
                background: post.matched ? 'var(--warning-bg, #FEF3C7)' : undefined,
              }"
            >
              <td class="col-narrow">{{ post.index }}</td>
              <td class="col-medium text-sm text-secondary">{{ post.source_name }}</td>
              <td class="col-medium">
                <span v-if="post.reply_level" class="text-secondary"
                  :style="{ paddingLeft: (post.reply_level - 1) * 14 + 'px' }">└─ </span>
                <strong>{{ post.username }}</strong>
              </td>
              <td class="col-medium text-sm text-secondary">{{ post.timestamp }}</td>
              <td class="col-wide">{{ formatContent(post.content) }}</td>
              <td class="col-wide">{{ formatContent(post.translation) }}</td>
              <td class="col-narrow">{{ post.page_number }}</td>
            </tr>
          </template>
        </tbody>
      </table>

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

    <!-- 帖子详情弹窗 -->
    <div v-if="showDetail" class="modal-overlay" @click.self="showDetail = false">
      <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="post-detail-title">
        <div class="modal-header">
          <h3 id="post-detail-title">📝 帖子详情 #{{ detailPost?.index }}</h3>
          <button class="btn btn-sm btn-outline" @click="showDetail = false">✕</button>
        </div>
        <div class="flex items-center gap-4 mb-4 text-sm text-secondary">
          <span><strong>{{ detailPost?.username }}</strong></span>
          <span>{{ detailPost?.timestamp }}</span>
          <span>第 {{ detailPost?.page_number }} 页</span>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
          <div>
            <div class="form-label">原文</div>
            <div style="white-space: pre-wrap; font-size: 13px; line-height: 1.6; max-height: 50vh; overflow-y: auto;">
              {{ detailPost?.content || '(空)' }}
            </div>
          </div>
          <div>
            <div class="form-label">中文翻译</div>
            <div style="white-space: pre-wrap; font-size: 13px; line-height: 1.6; max-height: 50vh; overflow-y: auto;">
              {{ detailPost?.translation || '(无翻译)' }}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
