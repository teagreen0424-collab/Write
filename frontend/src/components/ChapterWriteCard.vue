<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NInput, NSelect, useMessage } from 'naive-ui'
import { api } from '../api'
import type { Chapter, OutlineNode } from '../types'

const props = defineProps<{
  novelId: string
  conversationId: string
  messageId: string
  sourceText: string
}>()
const emit = defineEmits<{
  (e: 'applied', chapter: Chapter): void
  (e: 'cancel'): void
}>()

const toast = useMessage()
const title = ref('')
const directive = ref(props.sourceText)
const outlineNodeId = ref('')
const previousChapterId = ref('')
const outlineOptions = ref<{ label: string; value: string }[]>([])
const latestTitle = ref('暂无章节，从第一章开始')
const generating = ref(false)
const previewText = ref('')

onMounted(async () => {
  try {
    const [chapters, outline] = await Promise.all([
      api.listChapters(props.novelId), api.listOutline(props.novelId),
    ])
    const latest = chapters[chapters.length - 1]
    previousChapterId.value = latest?.id ?? ''
    latestTitle.value = latest ? `承接第 ${latest.seq} 章《${latest.title}》` : '暂无章节，从第一章开始'
    const chapterNodes = (outline as OutlineNode[]).filter((node) => node.level === 'chapter')
    outlineOptions.value = chapterNodes.map((node) => ({ label: node.title, value: node.id }))
    const currentIndex = chapterNodes.findIndex((node) => node.id === latest?.outline_node_id)
    const nextIndex = currentIndex >= 0 ? currentIndex + 1 : chapters.length
    outlineNodeId.value = chapterNodes[nextIndex]?.id ?? ''
  } catch (error) {
    toast.error((error as Error).message)
  }
})

async function write() {
  if (!directive.value.trim()) {
    toast.warning('请确认或编辑章前指令')
    return
  }
  generating.value = true
  previewText.value = ''
  try {
    await api.writeMessageChapter(
      props.conversationId, props.messageId,
      {
        title: title.value.trim(), directive: directive.value.trim(),
        outline_node_id: outlineNodeId.value,
        previous_chapter_id: previousChapterId.value,
      },
      {
        onToken: (text) => { previewText.value += text },
        onDone: (data) => {
          toast.success('章节草稿已保存')
          emit('applied', data.chapter as Chapter)
        },
        onError: (error) => { toast.error(error) },
      },
    )
  } catch (error) {
    toast.error((error as Error).message)
  } finally {
    generating.value = false
  }
}
</script>

<template>
  <div class="write-card">
    <p>确认后创建一章草稿；{{ latestTitle }}。生成期间若最新章改变，将拒绝落库。</p>
    <n-input v-model:value="title" placeholder="新章标题（留空则自动编号）" />
    <n-input v-model:value="directive" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" placeholder="章前指令" />
    <n-select v-model:value="outlineNodeId" :options="outlineOptions" placeholder="可选：绑定章级大纲" clearable />
    <div class="actions">
      <n-button size="small" :disabled="generating" @click="emit('cancel')">取消</n-button>
      <n-button size="small" type="primary" :loading="generating" @click="write">确认写这一章</n-button>
    </div>
    <div v-if="previewText" class="preview">{{ previewText }}</div>
  </div>
</template>

<style scoped>
.write-card { display: grid; gap: 10px; width: min(680px, 90vw); padding: 12px; background: #fafafa; border: 1px solid #e5e7eb; border-radius: 8px; }
.write-card p { margin: 0; color: #666; }
.actions { display: flex; gap: 8px; justify-content: flex-end; }
.preview { white-space: pre-wrap; max-height: 300px; overflow: auto; }
</style>
