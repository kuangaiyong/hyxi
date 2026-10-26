<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import * as notifyApi from '@/api/notify'
import { fetchSources } from '@/api/sources'
import { useToast } from '@/composables/useToast'
import type {
  NotifyCertInfo, NotifyConfigInput, NotifyConfigPublic, NotifyRun, NotifySourceBrief, NotifyStatus, NotifyTestResult,
  NotifyTrustedCert,
} from '@/types/notify'
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
// 信任的证书（SHA-256）。公司内网的邮件服务器常用自签证书，严格校验过不去，只能按指纹认确认过的那几张
const trustedCerts = ref<NotifyTrustedCert[]>([])

const saving = ref(false)
const testing = ref(false)
const starting = ref(false)
// 测试结果连同它测的那个地址一起存（见 smtpAddress 下面的 testResult）
const testOutcome = ref<{ address: string; result: NotifyTestResult } | null>(null)
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
    trusted_certs: trustedCerts.value.map((c) => ({ ...c })),
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
  // 8000 上跑着旧版本后端（便携包、改完没重启）时没有这个字段：别让整页加载失败、还说成「后端不可用」
  trustedCerts.value = (cfg.trusted_certs ?? []).map((c) => ({ ...c }))
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

/** AB:CD:… —— 和 openssl x509 -fingerprint -sha256 的输出一样（核对时比 SHA-256，大小写和冒号不算数） */
const fmtFingerprint = (fp: string) => (fp.toUpperCase().match(/.{2}/g) || []).join(':')

// 测试结果（连同「信任这张证书」）只对测的那个地址有效：地址对不上就不显示。改了地址还留着，或者请求在途时改了地址、
// 结果回来挂到新地址下面，点信任就会把上一台的证书加进来（评审发现）。一处判定，两种情况都管
const smtpAddress = () =>
  JSON.stringify([smtpHost.value.trim(), Number(smtpPort.value), Number(smtpSslPort.value), useSsl.value])
const testResult = computed(() =>
  testOutcome.value && testOutcome.value.address === smtpAddress() ? testOutcome.value.result : null)

// 发送记录里撞上的那张证书：只在它属于表单上这台服务器、又还没信任过时给「信任这张证书」——
// 改了服务器还点，信任进来的是一条用不上的；信任过了还留着按钮，再点就是白发一封测试信（评审发现）
const lastCertToTrust = computed(() => {
  const cert = status.value?.last?.untrusted_cert
  if (!cert || cert.host !== smtpHost.value.trim().toLowerCase()) return null
  return trustedCerts.value.some((c) => c.host === cert.host && c.fingerprint === cert.fingerprint) ? null : cert
})

/** 证书概况里有值的几行（cryptography 解析不了的证书概况是空的，空行不显示） */
const certLines = (c: NotifyCertInfo) => [
  ['证书主体', c.subject],
  ['签发者', c.issuer],
  ['有效期', c.not_after ? `${fmtTime(c.not_before)} ～ ${fmtTime(c.not_after)}` : ''],
].filter(([, value]) => value)

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
  testOutcome.value = null
  const address = smtpAddress()     // 记下发请求时的地址：等结果期间（双重校验最慢能等上一分钟）改了地址，这个结果就不显示
  let result: NotifyTestResult
  try {
    result = await notifyApi.sendTestMail(payload())
  } catch (e: any) {
    result = { success: false, message: errorText(e, '发送测试邮件失败，请确认后端服务可用') }
  } finally {
    testing.value = false
  }
  testOutcome.value = { address, result }
}

/** 信任撞上的那张证书（测试结果或发送记录里的；追加，不顶掉已信任的；绑在读到它的那台服务器上），并马上重发一封验证。
 *  要「保存配置」后「立即发送」和每天定时才用得上；密码框空着时后端会要求重填密码 */
function trustCert(cert: NotifyCertInfo) {
  const known = trustedCerts.value.some((c) => c.host === cert.host && c.fingerprint === cert.fingerprint)
  if (!known) trustedCerts.value = [...trustedCerts.value, { host: cert.host, fingerprint: cert.fingerprint }]
  handleTest()
}

// 所选数据源里今天还没采集翻译过的：不为空时弹确认框，等用户选（每天定时不弹，一律先采）
const staleSources = ref<NotifySourceBrief[]>([])

async function handleSendNow() {
  starting.value = true
  try {
    const check = await notifyApi.fetchPrecheck()
    if (check.stale.length) {
      staleSources.value = check.stale
      return
    }
    status.value = await notifyApi.sendNow(false)
    poll()
  } catch (e: any) {
    toast.error(errorText(e, '发送失败，请确认后端服务可用'))
  } finally {
    starting.value = false
  }
}

/** 确认框里的选择：true 先采集再发送，false 直接用已有数据发送 */
async function confirmSend(collect: boolean) {
  staleSources.value = []
  starting.value = true
  try {
    status.value = await notifyApi.sendNow(collect)
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
    testOutcome.value = null
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
        <div v-if="trustedCerts.length" class="form-group">
          <label class="form-label">信任的证书（服务器 · SHA-256 指纹）</label>
          <div v-for="c in trustedCerts" :key="`${c.host}|${c.fingerprint}`" class="flex gap-2 items-center"
               style="margin-bottom: 6px;">
            <input :value="`${c.host} · ${fmtFingerprint(c.fingerprint)}`" type="text" class="form-input" readonly
                   style="font-family: monospace; font-size: 12px;" data-testid="notify-trusted-cert" />
            <button class="btn btn-outline" data-testid="notify-untrust-cert"
                    @click="trustedCerts = trustedCerts.filter((x) => x !== c)">取消信任</button>
          </div>
        </div>

        <div
          v-if="testResult"
          class="mb-4"
          :class="testResult.success ? 'text-success' : 'text-error'"
          style="padding: 10px 12px; border-radius: 8px; font-size: 13px; overflow-wrap: anywhere;"
          :style="{ background: testResult.success ? '#D1FAE5' : '#FEE2E2' }"
          data-testid="notify-test-result"
        >
          {{ testResult.success ? '✅' : '❌' }} {{ testResult.message }}
          <div v-if="testResult.untrusted_cert" style="margin-top: 8px; line-height: 1.8;" data-testid="notify-cert">
            <div v-for="[label, value] in certLines(testResult.untrusted_cert)" :key="label">{{ label }}：{{ value }}</div>
            <button class="btn btn-outline" style="margin-top: 6px;" :disabled="testing" data-testid="notify-trust-cert"
                    @click="trustCert(testResult.untrusted_cert)">信任这张证书</button>
          </div>
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
          <span class="spinner"></span> {{ status.progress || '正在发送' }}
          <router-link v-if="status.task_id" :to="`/tasks/${status.task_id}/progress`" style="margin-left: 8px;"
                       data-testid="notify-task-link">查看任务进度</router-link>
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
          <p style="margin: 8px 0 0; overflow-wrap: anywhere;" data-testid="notify-last-message">{{ status.last.message }}</p>
          <p v-if="status.last.subject" class="text-secondary" style="margin: 4px 0 0;">
            邮件标题：{{ status.last.subject }}{{ status.last.attachment ? '（含 HTML 附件）' : '' }}
          </p>
          <p v-if="status.last.task_ids?.length" class="text-secondary" style="margin: 4px 0 0;" data-testid="notify-last-tasks">
            发送前自动发起的任务：
            <router-link v-for="tid in status.last.task_ids" :key="tid" :to="`/tasks/${tid}/progress`"
                         style="margin-right: 8px;">{{ tid.slice(0, 8) }}</router-link>
          </p>
          <div v-if="lastCertToTrust" style="margin-top: 6px; line-height: 1.8;" data-testid="notify-last-cert">
            <div v-for="[label, value] in certLines(lastCertToTrust)" :key="label">{{ label }}：{{ value }}</div>
            <button class="btn btn-outline" style="margin-top: 6px;" :disabled="testing"
                    data-testid="notify-last-trust-cert" @click="trustCert(lastCertToTrust)">信任这张证书</button>
          </div>
        </div>
        <p v-else-if="!status?.running" class="text-sm text-secondary">还没有发送过。</p>
        <p class="text-sm text-secondary" style="margin-top: 8px;" data-testid="notify-next-run">
          {{ status?.next_run ? `下次定时发送：${fmtTime(status.next_run)}` : '没有开启每天定时发送' }}
        </p>
      </div>

      <div v-if="staleSources.length" class="modal-overlay" data-testid="notify-collect-confirm" @click.self="staleSources = []">
        <div class="modal-content" style="max-width: 460px;">
          <div class="modal-header"><h3>先采集再发送？</h3></div>
          <p class="mb-2">今天还没有采集并翻译：</p>
          <ul style="margin: 0 0 16px 20px; line-height: 1.9;">
            <li v-for="s in staleSources" :key="s.id">{{ s.collector_name }}「{{ s.name }}」</li>
          </ul>
          <div class="flex gap-2" style="justify-content: flex-end; flex-wrap: wrap;">
            <button class="btn btn-outline" data-testid="notify-collect-cancel" @click="staleSources = []">取消</button>
            <button class="btn btn-outline" data-testid="notify-collect-skip" @click="confirmSend(false)">直接用已有数据发送</button>
            <button class="btn btn-primary" data-testid="notify-collect-go" @click="confirmSend(true)">先采集再发送</button>
          </div>
        </div>
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
