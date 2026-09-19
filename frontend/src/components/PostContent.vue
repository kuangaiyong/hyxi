<script setup lang="ts">
import { ref, computed } from 'vue'
import { withApiKey } from '@/api/client'
import type { PostData } from '@/types/result'

/** 一条帖子的正文：原文 / 译文 / 图片。主贴和评论共用同一套渲染。 */
const props = defineProps<{
  post: PostData
  mode: 'bilingual' | 'zh' | 'orig'
}>()
const emit = defineEmits<{ (e: 'zoom', url: string): void }>()

// 超过这个长度才折叠。刚修好的多段评论有 800+ 字符，一屏放不下
const LIMIT = 600
const expanded = ref(false)
// 引用框单独折叠：它是**上下文**，不是这一条要说的话。被引用的楼层很长时，
// 不折叠会把引用者的正文顶到屏幕外 —— 那正好把这条回复本身藏起来了
const quoteExpanded = ref(false)

const showOrig = computed(() => props.mode !== 'zh')
const showZh = computed(() => props.mode !== 'orig')

function clip(text: string): string {
  if (expanded.value || !text || text.length <= LIMIT) return text
  return text.slice(0, LIMIT) + '…'
}

const QUOTE_LIMIT = 300
/** 一条楼层可以引多人（原站支持多引用），逐条渲染，不丢后面的 */
const quotes = computed(() => props.post.quotes || [])
function clipQuote(text: string): string {
  if (quoteExpanded.value || !text || text.length <= QUOTE_LIMIT) return text
  return text.slice(0, QUOTE_LIMIT) + '…'
}
function quoteTooLong(q: { content?: string; translation?: string }): boolean {
  const o = showOrig.value ? (q.content || '').length : 0
  const z = showZh.value ? (q.translation || '').length : 0
  return Math.max(o, z) > QUOTE_LIMIT
}

const tooLong = computed(() => {
  const o = showOrig.value ? (props.post.content || '').length : 0
  const z = showZh.value ? (props.post.translation || '').length : 0
  return Math.max(o, z) > LIMIT
})

/** 图片走后端受保护端点；<img> 带不了请求头，密钥只能挂 query（同 SSE） */
function mediaUrl(rel: string): string {
  const safe = rel.split('/').map(encodeURIComponent).join('/')
  return withApiKey(`/api/v1/media/${safe}`)
}

/** 引用被人时用谁的名字：解析到楼层用它自己的，否则用快照里从引用行解出来的 */
function quoteWho(q: { username?: string; cite?: string }): string {
  return q.username || q.cite || '（未署名）'
}
</script>

<template>
  <div class="post-content">
    <!-- 引用框在正文**上方** —— 和原站一样（引用块就是 .messagecontent 的第一个子元素）。
         它显示的永远是被引用者的内容与配图，与引用者自己的正文/配图分开渲染：
         混在一起就是把人家的图和话算到引用者头上 -->
    <blockquote
      v-for="(quote, qi) in quotes"
      :key="qi"
      class="pc-quote"
      data-testid="quote-box"
    >
      <div class="pc-quote-head">
        <span class="pc-quote-mark">↖</span>
        <span class="pc-quote-label">引用</span>
        <strong class="pc-quote-user">{{ quoteWho(quote) }}</strong>
        <span v-if="quote.resolved && quote.timestamp" class="pc-quote-time">{{ quote.timestamp }}</span>
        <!-- 解析到楼层时给出它在列表里的位置（内部锚点跳转）。
             没解析到时明说：这段可能只是原站给的一截，别让用户以为被引用的人就说了这么多 -->
        <a
          v-if="quote.resolved && quote.index"
          class="pc-quote-jump"
          :href="'#post-' + quote.index"
          :title="`跳到被引用的第 ${quote.index} 条`"
        >#{{ quote.index }}</a>
        <span v-else class="pc-quote-note" data-testid="quote-unresolved">
          引用片段（原楼未采集）
        </span>
        <span v-if="quote.truncated" class="pc-quote-note">原站只提供截断片段</span>
      </div>
      <p v-if="showOrig && quote.content" class="pc-quote-orig">{{ clipQuote(quote.content) }}</p>
      <p v-if="showZh && (quote.translation || '').trim()" class="pc-quote-zh">
        {{ clipQuote(quote.translation) }}
      </p>
      <p v-else-if="showZh && quote.content" class="pc-quote-untranslated">（这一段尚无译文）</p>
      <button v-if="quoteTooLong(quote)" class="pc-more" @click="quoteExpanded = !quoteExpanded">
        {{ quoteExpanded ? '收起引用 ▴' : '展开引用 ▾' }}
      </button>
      <div v-if="quote.images && quote.images.length" class="pc-images pc-quote-images">
        <img
          v-for="(im, i) in quote.images"
          :key="i"
          :src="mediaUrl(im)"
          loading="lazy"
          alt="被引用内容的配图"
          @click="emit('zoom', mediaUrl(im))"
        />
      </div>
    </blockquote>

    <p v-if="showOrig && post.content" class="pc-orig">{{ clip(post.content) }}</p>
    <!-- 判据必须和后端 needs_translation 一样去空白：只有空格的译文渲染成一行空白，
         看着像「翻了但是空的」，而提示条上的 N 把它算作待补译，两处对不上 -->
    <p v-if="showZh && (post.translation || '').trim()" class="pc-zh">{{ clip(post.translation) }}</p>
    <p v-else-if="showZh && post.content" class="pc-untranslated">（尚未翻译）</p>

    <button v-if="tooLong" class="pc-more" @click="expanded = !expanded">
      {{ expanded ? '收起 ▴' : '展开全文 ▾' }}
    </button>

    <div v-if="post.images && post.images.length" class="pc-images">
      <img
        v-for="(im, i) in post.images"
        :key="i"
        :src="mediaUrl(im)"
        loading="lazy"
        alt="帖子配图"
        @click="emit('zoom', mediaUrl(im))"
      />
    </div>
  </div>
</template>

<style scoped>
.pc-orig,
.pc-zh,
.pc-untranslated {
  /* 采集时多段正文用 \n 拼接，pre-wrap 才能把段落还原出来 */
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0 0 6px;
  line-height: 1.65;
}
.pc-orig {
  font-size: 13px;
  color: var(--text-secondary);
}
.pc-zh {
  font-size: 14px;
  color: var(--text-primary);
}
.pc-untranslated {
  font-size: 13px;
  color: var(--text-secondary);
  font-style: italic;
}
.pc-more {
  border: none;
  background: none;
  padding: 0;
  cursor: pointer;
  font-size: 12px;
  color: var(--primary);
}

/* ===== 引用框 =====
   四路信号和「回复面板」的既有做法一致，缺一路在深色主题下就会看不见：
   ① 左侧竖条 ② 另一个底色 ③ ↖ 箭头 ④ 「引用」字样。
   **不能只靠左边框**：--border-light 在深色主题下恰好等于 --bg-card */
.pc-quote {
  --quote-panel: #EEF2F7;
  --quote-rail: #7C8DA6;
  margin: 0 0 8px;
  padding: 6px 10px 8px;
  background: var(--quote-panel);
  border-left: 3px solid var(--quote-rail);
  border-radius: 0 6px 6px 0;
}
[data-theme="dark"] .pc-quote {
  --quote-panel: #1B2436;
  --quote-rail: #52627A;
}
.pc-quote-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.pc-quote-mark {
  color: var(--quote-rail);
  font-weight: 700;
}
.pc-quote-label {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--quote-rail);
  color: #fff;
}
.pc-quote-user {
  font-size: 12px;
  color: var(--text-secondary);
}
.pc-quote-time {
  font-size: 11px;
  color: var(--text-secondary);
}
.pc-quote-jump {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 10px;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  text-decoration: none;
}
.pc-quote-jump:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.pc-quote-note {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 10px;
  background: #FDE68A;
  color: #92400E;
}
.pc-quote-orig,
.pc-quote-zh,
.pc-quote-untranslated {
  /* 采集时多段正文用 \n 拼接，pre-wrap 才能把段落还原出来 */
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0 0 4px;
  line-height: 1.6;
}
.pc-quote-orig {
  font-size: 12px;
  color: var(--text-secondary);
}
.pc-quote-zh {
  font-size: 13px;
  color: var(--text-primary);
}
.pc-quote-untranslated {
  font-size: 12px;
  color: var(--text-secondary);
  font-style: italic;
}
/* 引用图比正文图小一号：它是「别人发过的那张」，不该抢正文配图的注意力 */
.pc-quote-images img {
  width: 84px;
  height: 84px;
}
.pc-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.pc-images img {
  width: 120px;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--border-light);
  cursor: zoom-in;
}
</style>
