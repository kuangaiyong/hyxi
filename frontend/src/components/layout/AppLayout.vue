<script setup lang="ts">
import { computed } from 'vue'
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
    default: return 'HYXi 舆情分析'
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
        <router-link to="/config">
          <span class="nav-icon">⚙️</span> LLM 配置
        </router-link>
      </nav>
    </aside>

    <div class="app-main">
      <header class="app-header">
        <h2>{{ pageTitle }}</h2>
        <slot name="header-actions" />
      </header>
      <div class="app-content">
        <slot />
      </div>
    </div>
  </div>
</template>
