<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getApiKey, setApiKey } from '@/api/client'
import { useConfigStore } from '@/stores/config'
import { useToast } from '@/composables/useToast'

const configStore = useConfigStore()
const toast = useToast()
const apiKey = ref('')
const saveSuccess = ref(false)
const accessKey = ref(getApiKey())

onMounted(async () => {
  await configStore.fetchConfig()
})

async function handleSave() {
  const ok = await configStore.saveConfig(apiKey.value)
  if (ok) {
    saveSuccess.value = true
    setTimeout(() => { saveSuccess.value = false }, 3000)
  } else {
    toast.error('保存配置失败，请确认后端服务可用')
  }
}

async function handleTest() {
  await configStore.testConnection(apiKey.value)
}

function handleSaveAccessKey() {
  setApiKey(accessKey.value.trim())
  toast.success(accessKey.value.trim() ? '访问密钥已保存' : '访问密钥已清除')
}

async function handleReset() {
  // 重置失败时不能清空输入框，否则界面显示「已重置」而服务端配置还在
  const ok = await configStore.resetConfig()
  if (ok) {
    apiKey.value = ''
  } else {
    toast.error('重置配置失败，请确认后端服务可用')
  }
}
</script>

<template>
  <div style="max-width: 600px;">
    <div class="card">
      <div class="card-header">🔑 LLM API 配置</div>
      <p class="text-secondary text-sm mb-4">
        配置大模型 API 连接信息。支持 OpenAI 兼容接口（如 DeepSeek）。
      </p>

      <div class="form-group">
        <label class="form-label">API Key</label>
        <input
          v-model="apiKey"
          type="password"
          class="form-input"
          placeholder="sk-xxxxxxxxxxxxxxxx"
          autocomplete="off"
        />
      </div>

      <div class="form-group">
        <label class="form-label">Base URL</label>
        <input
          v-model="configStore.baseUrl"
          type="text"
          class="form-input"
          placeholder="https://api.deepseek.com"
        />
      </div>

      <div class="form-group">
        <label class="form-label">Model Name</label>
        <input
          v-model="configStore.modelName"
          type="text"
          class="form-input"
          placeholder="deepseek-chat"
        />
      </div>

      <!-- 测试结果 -->
      <div
        v-if="configStore.testResult"
        class="mb-4"
        :class="configStore.testResult === 'success' ? 'text-success' : 'text-error'"
        style="padding: 10px 12px; border-radius: 8px; font-size: 13px;"
        :style="{ background: configStore.testResult === 'success' ? '#D1FAE5' : '#FEE2E2' }"
      >
        {{ configStore.testResult === 'success' ? '✅' : '❌' }} {{ configStore.testMessage }}
      </div>

      <!-- 保存成功 -->
      <div
        v-if="saveSuccess"
        class="mb-4 text-success"
        style="padding: 10px 12px; border-radius: 8px; font-size: 13px; background: #D1FAE5;"
      >
        ✅ 配置保存成功！
      </div>

      <div class="flex gap-2">
        <button
          class="btn btn-outline"
          :disabled="configStore.isTesting || !apiKey"
          @click="handleTest"
        >
          <span v-if="configStore.isTesting" class="spinner"></span>
          {{ configStore.isTesting ? '测试中...' : '🔍 测试连接' }}
        </button>
        <button
          class="btn btn-primary"
          :disabled="!apiKey"
          @click="handleSave"
        >
          💾 保存配置
        </button>
        <button class="btn btn-outline" @click="handleReset">
          重置
        </button>
      </div>
    </div>

    <div class="card">
      <div class="card-header">🔐 服务访问密钥</div>
      <p class="text-secondary text-sm mb-4">
        后端设置了 <code>TWEAKERS_API_KEY</code> 时必须在此填入相同的值，否则所有接口返回 401。
        后端未设置则留空即可。
      </p>

      <div class="form-group">
        <label class="form-label">Access Key</label>
        <input
          v-model="accessKey"
          type="password"
          class="form-input"
          placeholder="与后端 TWEAKERS_API_KEY 一致"
          autocomplete="off"
        />
      </div>

      <button class="btn btn-primary" @click="handleSaveAccessKey">
        💾 保存到本机
      </button>
    </div>

    <div class="card" v-if="configStore.isConfigured">
      <div class="card-header">📋 当前配置状态</div>
      <div class="flex gap-4">
        <div>
          <span class="text-secondary text-sm">Base URL：</span>
          <code>{{ configStore.baseUrl }}</code>
        </div>
        <div>
          <span class="text-secondary text-sm">Model：</span>
          <code>{{ configStore.modelName }}</code>
        </div>
        <div>
          <span class="badge badge-completed">已配置</span>
        </div>
      </div>
    </div>
  </div>
</template>
