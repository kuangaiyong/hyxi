<script setup lang="ts">
import { useToast } from '@/composables/useToast'

const { toasts, remove } = useToast()

const typeStyles: Record<string, Record<string, string>> = {
  success: { bg: '#D1FAE5', color: '#059669', icon: '✅' },
  error:   { bg: '#FEE2E2', color: '#DC2626', icon: '❌' },
  warning: { bg: '#FEF3C7', color: '#D97706', icon: '⚠️' },
  info:    { bg: '#DBEAFE', color: '#2563EB', icon: 'ℹ️' },
}
</script>

<template>
  <Teleport to="body">
    <div class="toast-container">
      <div
        v-for="t in toasts"
        :key="t.id"
        class="toast-item"
        :style="{ background: typeStyles[t.type].bg, color: typeStyles[t.type].color }"
        @click="remove(t.id)"
      >
        <span>{{ typeStyles[t.type].icon }}</span>
        <span class="toast-msg">{{ t.message }}</span>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-container {
  position: fixed;
  top: 20px;
  right: 20px;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 380px;
}

.toast-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  animation: toast-in 0.3s ease;
  transition: opacity 0.2s;
}

.toast-item:hover {
  opacity: 0.85;
}

.toast-msg {
  flex: 1;
  line-height: 1.4;
}

@keyframes toast-in {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
</style>
