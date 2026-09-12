<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getApiKey, setApiKey } from '@/api/client'
import * as configApi from '@/api/config'
import { useConfigStore } from '@/stores/config'
import { useToast } from '@/composables/useToast'

const configStore = useConfigStore()
const toast = useToast()
const apiKey = ref('')
const saveSuccess = ref(false)
const accessKey = ref(getApiKey())
const visionKey = ref('')
const visionSaveSuccess = ref(false)

onMounted(async () => {
  await Promise.all([configStore.fetchConfig(), configStore.fetchVisionConfig()])
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

/**
 * 存下来不等于填对了 —— 当场拿它请求一次。
 *
 * 曾经这里只写 localStorage 就报「已保存」：填错了也这么说；填对了，右上角那条常驻的
 * 401 提示还挂着、下面两张卡片仍是空的（它们是在 401 时加载的），用户看不出自己做对
 * 没有（实测反馈过）。验证成功的那次响应会顺手撤掉 401 提示（见 api/client.ts）。
 */
async function handleSaveAccessKey() {
  const key = accessKey.value.trim()
  setApiKey(key)
  // 请求头只装得下 Latin-1：axios 发出去之前会把 U+00FF 以上的字符（中文等）直接删掉，
  // X-API-Key 变成空串，接口必然 401。后端是按 utf-8 字节比的（auth.py 专门为中文密钥
  // 改过），密钥本身没错 —— 这时报「与后端不一致」会让人反复重填一个本来正确的密钥
  if (/[^\x00-\xff]/.test(key)) {
    toast.error('服务访问密钥含中文等非 Latin-1 字符，浏览器无法把它放进请求头，'
      + '接口会一直 401。请把后端 .env 里的 TWEAKERS_API_KEY 换成 ASCII 字符')
    return
  }
  try {
    await configApi.fetchConfig()
  } catch (e: any) {
    toast.error(e?.response?.status === 401
      ? '服务访问密钥与后端不一致，接口仍然返回 401，请检查后重新填写'
      : '已保存到本机，但验证时后端没有响应，请确认服务在运行')
    return
  }
  // 两张卡片的值是在 401 时加载的（加载失败留着默认值），用新密钥补一遍
  await Promise.all([configStore.fetchConfig(), configStore.fetchVisionConfig()])
  toast.success(key ? '服务访问密钥已保存，验证通过' : '服务访问密钥已清除')
}

async function handleSaveVision() {
  const ok = await configStore.saveVisionConfig(visionKey.value)
  if (ok) {
    visionSaveSuccess.value = true
    setTimeout(() => { visionSaveSuccess.value = false }, 3000)
  } else {
    toast.error('保存多模态模型配置失败，请确认后端服务可用')
  }
}

async function handleTestVision() {
  await configStore.testVisionConnection(visionKey.value)
}

async function handleResetVision() {
  const ok = await configStore.resetVisionConfig()
  if (ok) {
    visionKey.value = ''
    toast.success('已清除，舆情分析将回到纯文本模式')
  } else {
    toast.error('清除多模态模型配置失败，请确认后端服务可用')
  }
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
    <!-- **必须排在最前面**：它是另外两张卡的前置条件 —— 后端设了密钥而这里没填，
         下面两张卡连自己的值都读不回来（401）。用户被 401 提示打发到这一页时，
         唯一该做的就是这一件事；排在两组「API Key」后面的话它落在首屏之外，
         而首屏那两个「API Key」是完全不同的东西，极易填错（用户实测反馈过）。 -->
    <div class="card">
      <div class="card-header">🔐 服务访问密钥</div>
      <p class="text-secondary text-sm mb-4">
        后端设置了 <code>TWEAKERS_API_KEY</code> 时必须在此填入相同的值，否则所有接口返回 401。
        后端未设置则留空即可。<strong>它与下面的大模型 API Key 是两回事。</strong>
      </p>

      <div class="form-group">
        <!-- 标签跟报错提示、《使用说明》逐字统一成「服务访问密钥」。曾经这里叫
             「Access Key」、提示里叫「服务访问密钥」、说明书里叫「接口密钥」——
             同一个东西三个名字，用户照着提示在页面上根本找不到它 -->
        <label class="form-label">服务访问密钥</label>
        <input
          v-model="accessKey"
          type="password"
          class="form-input"
          placeholder="与后端 TWEAKERS_API_KEY 一致"
          autocomplete="off"
          data-testid="access-key-input"
        />
      </div>

      <button class="btn btn-primary" @click="handleSaveAccessKey">
        💾 保存到本机
      </button>
    </div>

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
      <div class="card-header">🖼️ 多模态模型配置（图片理解）</div>
      <p class="text-secondary text-sm mb-4">
        <strong>可选。</strong>配置后，做<strong>舆情分析</strong>时会先把帖子配图交给多模态模型理解成文字，
        再连同正文与整串上下文一起交给上面的大模型判断情感倾向。翻译不使用图片。
        留空则舆情分析按纯文本进行，不影响任何既有功能。
      </p>

      <div class="form-group">
        <label class="form-label">API Key</label>
        <input
          v-model="visionKey"
          type="password"
          class="form-input"
          placeholder="sk-xxxxxxxxxxxxxxxx"
          autocomplete="off"
        />
      </div>

      <div class="form-group">
        <label class="form-label">Base URL</label>
        <input
          v-model="configStore.visionBaseUrl"
          type="text"
          class="form-input"
          placeholder="https://api.kimi.com/coding/v1"
        />
      </div>

      <div class="form-group">
        <label class="form-label">Model Name</label>
        <input
          v-model="configStore.visionModelName"
          type="text"
          class="form-input"
          placeholder="kimi-for-coding"
        />
        <p class="text-secondary text-sm" style="margin-top: 6px;">
          必须填一个<strong>支持图片输入</strong>的模型。填了不支持图片的模型不会报错，
          只会每次都拿不到描述、退回纯文本分析（后台日志里能看到失败原因）。
        </p>
      </div>

      <div
        v-if="configStore.visionTestResult"
        class="mb-4"
        :class="configStore.visionTestResult === 'success' ? 'text-success' : 'text-error'"
        style="padding: 10px 12px; border-radius: 8px; font-size: 13px;"
        :style="{ background: configStore.visionTestResult === 'success' ? '#D1FAE5' : '#FEE2E2' }"
      >
        {{ configStore.visionTestResult === 'success' ? '✅' : '❌' }} {{ configStore.visionTestMessage }}
      </div>

      <div
        v-if="visionSaveSuccess"
        class="mb-4 text-success"
        style="padding: 10px 12px; border-radius: 8px; font-size: 13px; background: #D1FAE5;"
      >
        ✅ 多模态模型配置保存成功！
      </div>

      <div class="flex gap-2">
        <button
          class="btn btn-outline"
          :disabled="configStore.visionTesting || !visionKey"
          @click="handleTestVision"
        >
          <span v-if="configStore.visionTesting" class="spinner"></span>
          {{ configStore.visionTesting ? '测试中...' : '🔍 测试连接' }}
        </button>
        <button
          class="btn btn-primary"
          :disabled="!visionKey"
          @click="handleSaveVision"
        >
          💾 保存配置
        </button>
        <button class="btn btn-outline" @click="handleResetVision">
          清除
        </button>
      </div>

      <p class="text-secondary text-sm" style="margin-top: 12px;">
        已配置状态：
        <span v-if="configStore.visionConfigured" class="badge badge-completed">已配置</span>
        <span v-else>未配置（纯文本分析）</span>
      </p>
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
