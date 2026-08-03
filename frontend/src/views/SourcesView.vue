<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useToast } from '@/composables/useToast'
import * as sourcesApi from '@/api/sources'
import type { CollectorInfo, SourcePublic } from '@/types/source'

const toast = useToast()

const collectors = ref<CollectorInfo[]>([])
const sources = ref<SourcePublic[]>([])
const loading = ref(true)
const loadError = ref('')

// 新建 / 编辑同一个表单：editingId 为空即新建
const showForm = ref(false)
const editingId = ref('')
const formCollectorId = ref('')
const formName = ref('')
const formParams = ref<Record<string, string>>({})
const formEnabled = ref(true)
const formError = ref('')

// 凭据表单（按 source 展开）
const credentialFor = ref('')
const credUsername = ref('')
const credPassword = ref('')
const credError = ref('')

const formCollector = computed(() =>
  collectors.value.find(c => c.id === formCollectorId.value)
)

const credentialSource = computed(() =>
  sources.value.find(s => s.id === credentialFor.value)
)

onMounted(loadData)

async function loadData() {
  loading.value = true
  loadError.value = ''
  try {
    const [cols, srcs] = await Promise.all([
      sourcesApi.fetchCollectors(),
      sourcesApi.fetchSources(),
    ])
    collectors.value = cols
    sources.value = srcs
  } catch (e: any) {
    // 后端不可达时不渲染空状态，否则会诱导用户重复注册已存在的数据源
    loadError.value = '加载失败: ' + (e.response?.data?.detail || e.message || '网络错误')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = ''
  formCollectorId.value = collectors.value[0]?.id || ''
  formName.value = ''
  formParams.value = {}
  formEnabled.value = true
  formError.value = ''
  showForm.value = true
}

function openEdit(source: SourcePublic) {
  editingId.value = source.id
  formCollectorId.value = source.collector_id
  formName.value = source.name
  formParams.value = Object.fromEntries(
    Object.entries(source.params || {}).map(([k, v]) => [k, String(v)])
  )
  formEnabled.value = source.enabled
  formError.value = ''
  showForm.value = true
}

async function handleSubmit() {
  formError.value = ''
  try {
    if (editingId.value) {
      await sourcesApi.updateSource(editingId.value, {
        name: formName.value,
        params: { ...formParams.value },
        enabled: formEnabled.value,
      })
    } else {
      await sourcesApi.createSource({
        collector_id: formCollectorId.value,
        name: formName.value,
        params: { ...formParams.value },
        enabled: formEnabled.value,
      })
    }
    showForm.value = false
    await loadData()
    toast.success(editingId.value ? '数据源已更新' : '数据源已创建')
  } catch (e: any) {
    formError.value = e.response?.data?.detail || e.message || '保存失败'
  }
}

async function handleToggle(source: SourcePublic) {
  try {
    await sourcesApi.updateSource(source.id, { enabled: !source.enabled })
    await loadData()
  } catch (e: any) {
    toast.error('切换启用状态失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function handleDelete(source: SourcePublic) {
  if (!confirm(`确定删除数据源「${source.name}」？其凭据会一并删除。`)) return
  try {
    await sourcesApi.deleteSource(source.id)
    if (credentialFor.value === source.id) credentialFor.value = ''
    await loadData()
    toast.success('数据源已删除')
  } catch (e: any) {
    toast.error('删除失败: ' + (e.response?.data?.detail || e.message))
  }
}

function openCredential(source: SourcePublic) {
  credentialFor.value = source.id
  credUsername.value = source.credential_username
  credPassword.value = ''
  credError.value = ''
}

async function handleSaveCredential() {
  credError.value = ''
  try {
    await sourcesApi.setCredential(credentialFor.value, {
      username: credUsername.value,
      password: credPassword.value,
    })
    credentialFor.value = ''
    credPassword.value = ''
    await loadData()
    toast.success('凭据已加密保存')
  } catch (e: any) {
    credError.value = e.response?.data?.detail || e.message || '保存失败'
  }
}

async function handleDeleteCredential(source: SourcePublic) {
  if (!confirm(`确定清除数据源「${source.name}」的凭据？`)) return
  try {
    await sourcesApi.deleteCredential(source.id)
    credentialFor.value = ''
    await loadData()
    toast.success('凭据已清除')
  } catch (e: any) {
    toast.error('清除失败: ' + (e.response?.data?.detail || e.message))
  }
}

// ===== 人工登录 =====

const authorizingId = ref('')
const authLogs = ref<string[]>([])
let authStream: EventSource | null = null

function closeAuthStream() {
  authStream?.close()
  authStream = null
}

async function handleAuthorize(source: SourcePublic) {
  authorizingId.value = source.id
  authLogs.value = []
  try {
    await sourcesApi.authorizeSource(source.id)
  } catch (e: any) {
    authorizingId.value = ''
    toast.error('发起授权失败: ' + (e.response?.data?.detail || e.message))
    return
  }
  closeAuthStream()
  authStream = new EventSource(sourcesApi.authorizeEventsUrl(source.id))
  authStream.addEventListener('log', (ev: MessageEvent) => {
    authLogs.value.push(JSON.parse(ev.data).message)
  })
  authStream.addEventListener('step_progress', (ev: MessageEvent) => {
    authLogs.value.push(JSON.parse(ev.data).message)
  })
  authStream.addEventListener('task_complete', async (ev: MessageEvent) => {
    const payload = JSON.parse(ev.data)
    closeAuthStream()
    authorizingId.value = ''
    if (payload.status === 'completed') {
      toast.success('授权成功，之后的采集会直接复用会话')
    } else {
      toast.error('授权失败: ' + (payload.error || '未知原因'))
    }
    await loadData()
  })
  authStream.onerror = () => {
    closeAuthStream()
    authorizingId.value = ''
  }
}

onUnmounted(closeAuthStream)

function paramSummary(source: SourcePublic): string {
  const entries = Object.entries(source.params || {})
  if (!entries.length) return '-'
  return entries.map(([k, v]) => `${k}=${v}`).join('  ')
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<template>
  <div style="max-width: 1100px;">
    <div class="flex items-center justify-between mb-4">
      <h2 style="font-size: 18px; font-weight: 600;">🌐 数据源</h2>
      <button class="btn btn-primary" :disabled="!collectors.length" @click="showForm ? (showForm = false) : openCreate()">
        {{ showForm ? '取消' : '+ 新增数据源' }}
      </button>
    </div>

    <p class="text-secondary text-sm mb-4">
      在这里注册要采集的来源。任务仍用自然语言提交，系统会把已启用的数据源清单交给大模型去选。
    </p>

    <!-- 新建 / 编辑表单 -->
    <div v-if="showForm" class="card">
      <div class="card-header">{{ editingId ? '编辑数据源' : '新增数据源' }}</div>

      <div class="form-group">
        <label class="form-label">采集器</label>
        <select v-model="formCollectorId" class="form-input" :disabled="!!editingId">
          <option v-for="c in collectors" :key="c.id" :value="c.id">{{ c.display_name }}</option>
        </select>
        <p v-if="editingId" class="text-secondary text-sm">采集器决定参数形态，创建后不可更换。</p>
      </div>

      <div class="form-group">
        <label class="form-label">显示名称</label>
        <input v-model="formName" type="text" class="form-input"
          :placeholder="formCollector?.display_name || '留空则用采集器名'" />
      </div>

      <div v-for="field in formCollector?.param_fields || []" :key="field.name" class="form-group">
        <label class="form-label">
          {{ field.label }}<span v-if="field.required" class="text-error"> *</span>
        </label>
        <input v-model="formParams[field.name]" :type="field.type === 'number' ? 'number' : 'text'"
          class="form-input" :placeholder="field.placeholder || ''" />
      </div>

      <div class="form-group">
        <label class="form-label">
          <input v-model="formEnabled" type="checkbox" /> 启用（只有启用的数据源会参与采集）
        </label>
      </div>

      <div v-if="formError" class="mb-4"
        style="padding: 8px 12px; border-radius: 6px; font-size: 13px; background: #FEE2E2; color: #DC2626;">
        ❌ {{ formError }}
      </div>

      <div class="flex gap-2">
        <button class="btn btn-primary" @click="handleSubmit">💾 保存</button>
        <button class="btn btn-outline" @click="showForm = false">取消</button>
      </div>
    </div>

    <!-- 凭据表单 -->
    <div v-if="credentialSource" class="card">
      <div class="card-header">🔐 {{ credentialSource.name }} — 登录凭据</div>
      <p class="text-secondary text-sm mb-4">
        密码经 Fernet 加密后落库，任何接口都不会把它读回来。
        后端未设置 <code>TWEAKERS_SECRET_KEY</code> 时会拒绝保存而不是明文存盘。
      </p>

      <div class="form-group">
        <label class="form-label">账号</label>
        <input v-model="credUsername" type="text" class="form-input" autocomplete="off" />
      </div>

      <div class="form-group">
        <label class="form-label">密码</label>
        <input v-model="credPassword" type="password" class="form-input" autocomplete="new-password" />
      </div>

      <div v-if="credError" class="mb-4"
        style="padding: 8px 12px; border-radius: 6px; font-size: 13px; background: #FEE2E2; color: #DC2626;">
        ❌ {{ credError }}
      </div>

      <div class="flex gap-2">
        <button class="btn btn-primary" :disabled="!credUsername || !credPassword" @click="handleSaveCredential">
          💾 保存凭据
        </button>
        <button v-if="credentialSource.has_credential" class="btn btn-outline" style="color: var(--error);"
          @click="handleDeleteCredential(credentialSource)">
          清除凭据
        </button>
        <button class="btn btn-outline" @click="credentialFor = ''">取消</button>
      </div>
    </div>

    <!-- 人工登录进度 -->
    <div v-if="authorizingId" class="card">
      <div class="card-header">🧑 人工登录进行中</div>
      <p class="text-secondary text-sm mb-4">
        已在服务器上打开一个浏览器窗口，请在里面完成登录（含两步验证 / 安全检查）。
        登录成功后会话会被保存，之后的采集不再需要密码。5 分钟内未完成即超时。
      </p>
      <div style="font-family: monospace; font-size: 12px; max-height: 180px; overflow-y: auto;">
        <div v-for="(line, i) in authLogs" :key="i" class="text-secondary">{{ line }}</div>
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="card text-center" style="padding: 48px;">
      <span class="spinner spinner-lg"></span>
    </div>

    <!-- 加载失败 -->
    <div v-else-if="loadError" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">⚠️</div>
      <p class="text-secondary mb-4">{{ loadError }}</p>
      <button class="btn btn-primary" @click="loadData">重试</button>
    </div>

    <!-- 空状态 -->
    <div v-else-if="!sources.length" class="card text-center" style="padding: 48px;">
      <div style="font-size: 48px; margin-bottom: 12px;">🌐</div>
      <p class="text-secondary mb-4">还没有数据源，注册第一个来源后就能提交采集任务了</p>
      <button class="btn btn-primary" @click="openCreate">新增数据源</button>
    </div>

    <!-- 数据源列表 -->
    <div v-else class="card" style="padding: 0; overflow-x: auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th style="width: 40px; text-align: center;">#</th>
            <th>名称</th>
            <th style="width: 140px;">采集器</th>
            <th>参数</th>
            <th style="width: 150px;">凭据</th>
            <th style="width: 80px; text-align: center;">状态</th>
            <th style="width: 220px; text-align: center;">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, idx) in sources" :key="item.id">
            <td style="text-align: center; font-size: 12px; color: var(--text-light);">{{ idx + 1 }}</td>
            <td style="font-weight: 500;">
              {{ item.name }}
              <div class="text-sm text-secondary">建于 {{ formatTime(item.created_at) }}</div>
            </td>
            <td class="text-sm">{{ item.collector_name }}</td>
            <td class="text-sm text-secondary">{{ paramSummary(item) }}</td>
            <td class="text-sm">
              <!-- has_credential 优先于 needs_credentials：存过的凭据不能因为
                   采集器声明不需要登录就在界面上消失 -->
              <template v-if="item.has_credential">
                <span class="badge badge-completed">已配置</span>
                <div class="text-secondary">{{ item.credential_username }}</div>
                <div class="text-secondary">上次授权 {{ formatTime(item.last_auth_at) }}</div>
              </template>
              <template v-else-if="!item.needs_credentials">
                <span class="text-secondary">无需登录</span>
              </template>
              <template v-else>
                <span class="badge badge-failed">未配置</span>
              </template>
              <div v-if="item.needs_credentials" class="text-secondary" style="margin-top: 2px;">
                {{ item.last_auth_at ? '会话正常' : '需重新授权' }}
              </div>
            </td>
            <td style="text-align: center;">
              <span :style="{
                display: 'inline-block', width: '8px', height: '8px', borderRadius: '50%',
                background: item.enabled ? '#10B981' : '#94A3B8',
              }" :title="item.enabled ? '已启用' : '已停用'"></span>
              {{ item.enabled ? '启用' : '停用' }}
            </td>
            <td style="text-align: center;">
              <div class="flex gap-1" style="justify-content: center;">
                <button class="btn btn-outline btn-sm" @click="openEdit(item)">编辑</button>
                <button v-if="item.needs_credentials || item.has_credential"
                  class="btn btn-outline btn-sm" @click="openCredential(item)">
                  🔐 凭据
                </button>
                <!-- 两步验证和安全检查交给人过，脚本不做任何绕过 -->
                <button v-if="item.needs_credentials" class="btn btn-outline btn-sm"
                  :disabled="!!authorizingId" @click="handleAuthorize(item)">
                  <span v-if="authorizingId === item.id" class="spinner"></span>
                  {{ authorizingId === item.id ? '授权中' : '🧑 人工登录' }}
                </button>
                <button class="btn btn-outline btn-sm" @click="handleToggle(item)">
                  {{ item.enabled ? '⏸' : '▶' }}
                </button>
                <button class="btn btn-outline btn-sm" style="color: var(--error);" @click="handleDelete(item)">
                  删除
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
