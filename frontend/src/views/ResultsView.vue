<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '@/stores/task'

const route = useRoute()
const router = useRouter()
const taskStore = useTaskStore()
const taskId = computed(() => route.params.id as string)

const currentPage = ref(1)
const pageSize = 50
const showDetail = ref(false)
const detailPost = ref<any>(null)

const totalPages = computed(() =>
  Math.ceil(taskStore.postsTotal / pageSize)
)

onMounted(async () => {
  taskStore.currentTaskId = taskId.value
  await taskStore.fetchResults()
  await taskStore.fetchTask(taskId.value)
})

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
          <a
            v-if="taskStore.isCompleted"
            :href="taskStore.getDownloadUrl()"
            class="btn btn-success"
            download
          >
            📥 下载 Excel
          </a>
        </div>
      </div>
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
      <table class="data-table">
        <thead>
          <tr>
            <th class="col-narrow">#</th>
            <th class="col-medium">用户</th>
            <th class="col-medium">时间</th>
            <th class="col-wide">原文（荷兰语）</th>
            <th class="col-wide">中文翻译</th>
            <th class="col-narrow">页</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="post in taskStore.posts"
            :key="post.index"
            @click="viewDetail(post)"
            style="cursor: pointer;"
          >
            <td class="col-narrow">{{ post.index }}</td>
            <td class="col-medium">
              <strong>{{ post.username }}</strong>
            </td>
            <td class="col-medium text-sm text-secondary">{{ post.timestamp }}</td>
            <td class="col-wide">{{ formatContent(post.content) }}</td>
            <td class="col-wide">{{ formatContent(post.translation) }}</td>
            <td class="col-narrow">{{ post.page_number }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 分页信息 -->
      <div class="flex items-center justify-between" style="padding: 12px 16px; border-top: 1px solid var(--border-light);">
        <span class="text-sm text-secondary">
          共 {{ taskStore.postsTotal }} 条帖子
        </span>
      </div>
    </div>

    <!-- 空状态 -->
    <div v-if="!taskStore.posts.length && !taskStore.isCompleted" class="empty-state">
      <div class="icon">📭</div>
      <p>暂无结果数据，等待任务完成...</p>
    </div>

    <!-- 帖子详情弹窗 -->
    <div v-if="showDetail" class="modal-overlay" @click.self="showDetail = false">
      <div class="modal-content">
        <div class="modal-header">
          <h3>📝 帖子详情 #{{ detailPost?.index }}</h3>
          <button class="btn btn-sm btn-outline" @click="showDetail = false">✕</button>
        </div>
        <div class="flex items-center gap-4 mb-4 text-sm text-secondary">
          <span><strong>{{ detailPost?.username }}</strong></span>
          <span>{{ detailPost?.timestamp }}</span>
          <span>第 {{ detailPost?.page_number }} 页</span>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
          <div>
            <div class="form-label">原文（荷兰语）</div>
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
