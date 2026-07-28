<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import AppLayout from '@/components/layout/AppLayout.vue'

const router = useRouter()
const configStore = useConfigStore()

onMounted(async () => {
  await configStore.fetchConfig()
})
</script>

<template>
  <AppLayout>
    <router-view />
  </AppLayout>
</template>
