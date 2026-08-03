<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const pageTitle = computed(() => {
  const name = route.name
  switch (name) {
    case 'Config': return 'LLM 配置'
    case 'TaskManagement': return '任务管理'
    case 'TaskProgress': return '任务进度'
    case 'TaskResults': return '任务结果'
    case 'SentimentIndex': return '舆情分析'
    case 'Sentiment': return '舆情详情'
    case 'Schedules': return '定时任务'
    case 'Sources': return '数据源'
    default: return 'HYXi 舆情分析'
  }
})

const isDark = ref(false)

function toggleTheme() {
  isDark.value = !isDark.value
  document.documentElement.setAttribute('data-theme', isDark.value ? 'dark' : '')
  localStorage.setItem('theme', isDark.value ? 'dark' : 'light')
}

onMounted(() => {
  const saved = localStorage.getItem('theme')
  if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    isDark.value = true
    document.documentElement.setAttribute('data-theme', 'dark')
  }
})
</script>

<template>
  <div class="app-layout">
    <aside class="app-sidebar">
      <div class="sidebar-header">
        <h1>⚡ HYXi 舆情分析</h1>
      </div>
      <nav class="sidebar-nav">
        <router-link to="/tasks">
          <span class="nav-icon">📋</span> 任务管理
        </router-link>
        <router-link to="/sentiment">
          <span class="nav-icon">📊</span> 舆情分析
        </router-link>
        <router-link to="/schedules">
          <span class="nav-icon">⏰</span> 定时任务
        </router-link>
        <router-link to="/sources">
          <span class="nav-icon">🌐</span> 数据源
        </router-link>
        <router-link to="/config">
          <span class="nav-icon">⚙️</span> LLM 配置
        </router-link>
      </nav>
    </aside>

    <div class="app-main">
      <header class="app-header">
        <h2>{{ pageTitle }}</h2>
        <div style="display: flex; align-items: center; gap: 8px;">
          <slot name="header-actions" />
          <button class="theme-toggle" @click="toggleTheme" :title="isDark ? '切换亮色' : '切换暗色'">
            {{ isDark ? '☀️' : '🌙' }}
          </button>
        </div>
      </header>
      <div class="app-content">
        <slot />
      </div>
    </div>
  </div>
</template>
