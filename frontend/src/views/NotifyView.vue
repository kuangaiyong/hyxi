<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import * as notifyApi from '@/api/notify'
import { fetchSources } from '@/api/sources'
import { useToast } from '@/composables/useToast'
import type { NotifyConfigInput, NotifyConfigPublic, NotifyRun, NotifyStatus } from '@/types/notify'
import type { SourcePublic } from '@/types/source'

/** 邮件通知：把所选数据源近几天的新回复（原文 + 译文）发到邮箱。规格见 docs/features/email-notify.md */

const toast = useToast()

const DAY_OPTIONS: { value: number | null; label: string }[] = [
  { value: null, label: '不设置（近 1 天）' },
  { value: 3, label: '近 3 天' },
  { value: 7, label: '近 7 天' },
  { value: 14, label: '近 14 天' },
]
const OUTCOME: Record<NotifyRun['outcome'], { label: string; badge: string }> = {
  sent: { label: '已发送', badge: 'badge-completed' },
  skipped: { label: '没有新内容，未发送', badge: 'badge-pending' },
  failed: { label: '失败，未发送', badge: 'badge-failed' },
}

const sources = ref<SourcePublic[]>([])
const loaded = ref(false)
const loadError = ref('')

const recipientsText = ref('')
const sourceIds = ref<string[]>([])
const days = ref<number | null>(null)
const scheduleEnabled = ref(false)
const scheduleTime = ref('09:00')
const smtpHost = ref('')
const smtpPort = ref(25)
const smtpSslPort = ref(465)
const useSsl = ref(true)
const sender = ref('')
const username = ref('')
const password = ref('')
const hasPassword = ref(false)
const isConfigured = ref(false)

const saving = ref(false)
const testing = ref(false)
const starting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const status = ref<NotifyStatus | null>(null)
let pollTimer: number | undefined

/** 收件人一行一个，也认逗号、分号和空格 */
function recipients(): string[] {
  return recipientsText.value.split(/[\s,;，；]+/).map((s) => s.trim()).filter(Boolean)
}

function payload(): NotifyConfigInput {
  return {
    recipients: recipients(),
    source_ids: [...sourceIds.value],
    days: days.value,
    schedule_enabled: scheduleEnabled.value,
    schedule_time: scheduleTime.value,
    smtp_host: smtpHost.value.trim(),
    smtp_port: Number(smtpPort.value),
    smtp_ssl_port: Number(smtpSslPort.value),
    use_ssl: useSsl.value,
    sender: sender.value.trim(),
    username: username.value.trim(),
    password: password.value,
  }
}

// 「立即发送」用的是**已保存**的配置。表单改了没保存就点，用户会以为发的是眼前这一份
const savedSnapshot = ref('')
const snapshot = () => JSON.stringify({ ...payload(), password: '' })
const dirty = computed(() => loaded.value && (snapshot() !== savedSnapshot.value || !!password.value))

function apply(cfg: NotifyConfigPublic) {
  recipientsText.value = cfg.recipients.join('\n')
  sourceIds.value = [...cfg.source_ids]
  days.value = cfg.days
  scheduleEnabled.value = cfg.schedule_enabled
  scheduleTime.value = cfg.schedule_time
  smtpHost.value = cfg.smtp_host
  smtpPort.value = cfg.smtp_port
  smtpSslPort.value = cfg.smtp_ssl_port
  useSsl.value = cfg.use_ssl
  sender.value = cfg.sender
  username.value = cfg.username
  password.value = ''
  hasPassword.value = cfg.has_password
  isConfigured.value = cfg.is_configured
  savedSnapshot.value = snapshot()
}

/** 400 的 detail 是一句人话；422（字段类型 / 范围不对）是一个数组 */
function errorText(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail
  if (Array.isArray(detail)) {
    return '填写的内容不对：' + detail.map((d: any) => `${(d.loc || []).slice(-1)[0]} ${d.msg}`).join('；')
  }
  return detail || fallback
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}

const sourceLabel = (s: SourcePublic) => `${s.collector_name}「${s.name}」`

onMounted(async () => {
  try {
    const [cfg, srcs, st] = await Promise.all([
      notifyApi.fetchNotifyConfig(), fetchSources(), notifyApi.fetchNotifyStatus(),
    ])
    sources.value = srcs
    apply(cfg)
    status.value = st
    loaded.value = true
    if (st.running) poll()
  } catch (e: any) {
    loadError.value = errorText(e, '加载邮件通知配置失败，请确认后端服务可用')
  }
})

onBeforeUnmount(stopPoll)

async function handleSave() {
  saving.value = true
  try {
    apply(await notifyApi.saveNotifyConfig(payload()))
    status.value = await notifyApi.fetchNotifyStatus()
    toast.success(scheduleEnabled.value ? `已保存，每天 ${scheduleTime.value} 自动发送` : '已保存')
  } catch (e: any) {
    toast.error(errorText(e, '保存失败，请确认后端服务可用'))
  } finally {
    saving.value = false
  }
}

async function handleTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await notifyApi.sendTestMail(payload())
  } catch (e: any) {
    testResult.value = { success: false, message: errorText(e, '发送测试邮件失败，请确认后端服务可用') }
  } finally {
    testing.value = false
  }
}

async function handleSendNow() {
  starting.value = true
  try {
    status.value = await notifyApi.sendNow()
    poll()
  } catch (e: any) {
    toast.error(errorText(e, '发送失败，请确认后端服务可用'))
  } finally {
    starting.value = false
  }
}

async function handleClear() {
  if (!confirm('清除邮件通知配置？已保存的密码和每天定时发送会一起删掉。')) return
  try {
    apply(await notifyApi.deleteNotifyConfig())
    status.value = await notifyApi.fetchNotifyStatus()
    testResult.value = null
    toast.success('已清除邮件通知配置')
  } catch (e: any) {
    toast.error(errorText(e, '清除失败，请确认后端服务可用'))
  }
}

function stopPoll() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

/** 发送在后台跑（补译可能要几分钟），轮询到结束为止，然后把结果说出来 */
function poll() {
  stopPoll()
  pollTimer = window.setInterval(async () => {
    try {
      status.value = await notifyApi.fetchNotifyStatus()
    } catch {
      return
    }
    if (status.value.running) return
    stopPoll()
    const last = status.value.last
    if (!last) return
    if (last.outcome === 'sent') toast.success(last.message)
    else if (last.outcome === 'skipped') toast.info(last.message)
    else toast.error(last.message)
  }, 1500)
}
</script>

<template>
  <div style="max-width: 720px;">
    <div v-if="loadError" class="card text-error" data-testid="notify-load-error">{{ loadError }}</div>

    <template v-else>
      <div class="card">
        <div class="card-header">📧 发什么、发给谁</div>
        <div class="form-group">
          <label class="form-label" for="notify-recipients">收件人</label>
          <textarea
            id="notify-recipients"
            v-model="recipientsText"
            class="form-input"
            rows="3"
            placeholder="一行一个邮箱，例如 someone@example.com"
            data-testid="notify-recipients"
          ></textarea>
        </div>

        <div class="form-group">
          <label class="form-label">数据源（可多选）</label>
          <p v-if="!sources.length" class="text-secondary text-sm">还没有数据源，先到「数据源」页添加。</p>
          <label v-for="s in sources" :key="s.id" class="check-line">
            <input v-model="sourceIds" type="checkbox" :value="s.id" data-testid="notify-source" />
            {{ sourceLabel(s) }}
          </label>
        </div>

        <div class="form-group">
          <label class="form-label" for="notify-days">新回复时间</label>
          <select id="notify-days" v-model="days" class="form-input" style="width: auto; min-width: 180px;" data-testid="notify-days">
            <option v-for="o in DAY_OPTIONS" :key="String(o.value)" :value="o.value">{{ o.label }}</option>
          </select>
        </div>

        <div class="form-group" style="margin-bottom: 0;">
          <label class="check-line">
            <input v-model="scheduleEnabled" type="checkbox" data-testid="notify-schedule" />
            每天定时发送
          </label>
          <div v-if="scheduleEnabled" class="flex items-center gap-2" style="margin-top: 6px;">
            <span class="text-sm text-secondary">每天</span>
            <input v-model="scheduleTime" type="time" class="form-input" style="width: auto;" data-testid="notify-schedule-time" />
            <span class="text-sm text-secondary">发送</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">📮 发件邮箱（SMTP）</div>
        <div class="form-group">
          <label class="form-label" for="notify-smtp-host">SMTP 服务器</label>
          <input id="notify-smtp-host" v-model="smtpHost" type="text" class="form-input" data-testid="notify-smtp-host" />
        </div>

        <div class="flex gap-4">
          <div class="form-group">
            <label class="form-label" for="notify-smtp-port">端口</label>
            <input id="notify-smtp-port" v-model.number="smtpPort" type="number" min="1" max="65535" class="form-input" style="width: 120px;" data-testid="notify-smtp-port" />
          </div>
          <div class="form-group">
            <label class="form-label" for="notify-smtp-ssl-port">SSL 端口</label>
            <input id="notify-smtp-ssl-port" v-model.number="smtpSslPort" type="number" min="1" max="65535" class="form-input" style="width: 120px;" data-testid="notify-smtp-ssl-port" />
          </div>
          <div class="form-group" style="align-self: flex-end;">
            <label class="check-line">
              <input v-model="useSsl" type="checkbox" data-testid="notify-use-ssl" />
              使用 SSL（连 SSL 端口）
            </label>
          </div>
        </div>

        <div class="form-group">
          <label class="form-label" for="notify-sender">发件人邮箱</label>
          <input id="notify-sender" v-model="sender" type="text" class="form-input" data-testid="notify-sender" />
        </div>
        <div class="form-group">
          <label class="form-label" for="notify-username">用户名</label>
          <input id="notify-username" v-model="username" type="text" class="form-input" autocomplete="off" data-testid="notify-username" />
        </div>
        <div class="form-group">
          <label class="form-label" for="notify-password">密码 / 授权码</label>
          <input
            id="notify-password"
            v-model="password"
            type="password"
            class="form-input"
            :placeholder="hasPassword ? '已保存，留空则不修改' : ''"
            autocomplete="new-password"
            data-testid="notify-password"
          />
        </div>

        <div
          v-if="testResult"
          class="mb-4"
          :class="testResult.success ? 'text-success' : 'text-error'"
          style="padding: 10px 12px; border-radius: 8px; font-size: 13px;"
          :style="{ background: testResult.success ? '#D1FAE5' : '#FEE2E2' }"
          data-testid="notify-test-result"
        >
          {{ testResult.success ? '✅' : '❌' }} {{ testResult.message }}
        </div>

        <div class="flex gap-2 items-center" style="flex-wrap: wrap;">
          <button class="btn btn-primary" :disabled="saving" data-testid="notify-save" @click="handleSave">
            <span v-if="saving" class="spinner"></span> 💾 保存配置
          </button>
          <button class="btn btn-outline" :disabled="testing" data-testid="notify-test" @click="handleTest">
            <span v-if="testing" class="spinner"></span>
            {{ testing ? '发送中...' : '✉️ 发送测试邮件' }}
          </button>
          <button
            class="btn btn-outline"
            :disabled="!isConfigured || dirty || starting || status?.running"
            :title="!isConfigured ? '先保存配置' : dirty ? '有未保存的修改，先保存再发送' : ''"
            data-testid="notify-send"
            @click="handleSendNow"
          >
            🚀 立即发送
          </button>
          <button
            v-if="isConfigured"
            class="btn btn-outline"
            :disabled="status?.running"
            :title="status?.running ? '正在发送，等这一轮结束再清除' : ''"
            data-testid="notify-clear"
            @click="handleClear"
          >清除</button>
          <span v-if="isConfigured && dirty" class="text-sm text-secondary">有未保存的修改，「立即发送」用的是已保存的配置</span>
        </div>
      </div>

      <div class="card" data-testid="notify-status">
        <div class="card-header">📋 发送记录</div>
        <p v-if="status?.running" class="text-sm" data-testid="notify-progress">
          <span class="spinner"></span> 正在发送：{{ status.progress }}
        </p>
        <div v-if="status?.last" class="text-sm" data-testid="notify-last">
          <div class="flex items-center gap-2" style="flex-wrap: wrap;">
            <span class="badge" :class="OUTCOME[status.last.outcome].badge" data-testid="notify-last-outcome">
              {{ OUTCOME[status.last.outcome].label }}
            </span>
            <span class="text-secondary">
              {{ fmtTime(status.last.finished_at) }} · {{ status.last.trigger === 'schedule' ? '每天定时' : '手动发送' }}
            </span>
          </div>
          <p style="margin: 8px 0 0;" data-testid="notify-last-message">{{ status.last.message }}</p>
          <p v-if="status.last.subject" class="text-secondary" style="margin: 4px 0 0;">
            邮件标题：{{ status.last.subject }}{{ status.last.attachment ? '（含 HTML 附件）' : '' }}
          </p>
        </div>
        <p v-else-if="!status?.running" class="text-sm text-secondary">还没有发送过。</p>
        <p class="text-sm text-secondary" style="margin-top: 8px;" data-testid="notify-next-run">
          {{ status?.next_run ? `下次定时发送：${fmtTime(status.next_run)}` : '没有开启每天定时发送' }}
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  margin: 4px 0;
  cursor: pointer;
}
</style>
