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

const showOrig = computed(() => props.mode !== 'zh')
const showZh = computed(() => props.mode !== 'orig')

function clip(text: string): string {
  if (expanded.value || !text || text.length <= LIMIT) return text
  return text.slice(0, LIMIT) + '…'
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
</script>

<template>
  <div class="post-content">
    <p v-if="showOrig && post.content" class="pc-orig">{{ clip(post.content) }}</p>
    <p v-if="showZh && post.translation" class="pc-zh">{{ clip(post.translation) }}</p>
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
