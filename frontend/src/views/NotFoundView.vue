<script setup lang="ts">
import { useRouter } from 'vue-router'
const router = useRouter()
</script>

<template>
  <div class="empty-state" style="padding-top: 80px;">
    <div class="icon" style="font-size: 64px;">🔍</div>
    <h2 style="font-size: 24px; font-weight: 700; margin-bottom: 8px; color: var(--text);">404</h2>
    <p style="margin-bottom: 24px;">页面不存在</p>
    <button class="btn btn-primary" @click="router.push('/tasks')">← 返回首页</button>
  </div>
</template>
