<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NCheckbox,
  NEmpty,
  NInput,
  NList,
  NListItem,
  NModal,
  NSelect,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, streamSSE } from '../api'
import type { Chapter, Conflict, Suggestion } from '../types'

const props = defineProps<{ novelId: string }>()

const message = useMessage()

const chapters = ref<Chapter[]>([])
const selectedId = ref<string | null>(null)
const title = ref('')
const directive = ref('')
const content = ref('')
const contentRef = ref<HTMLTextAreaElement | null>(null)

const streaming = ref(false)
const streamText = ref('')
const conflicts = ref<Conflict[]>([])
const rewriteIntent = ref('')

const showSettle = ref(false)
const settleSummary = ref('')
const suggestions = ref<Suggestion[]>([])
const checkedSuggestion = ref<boolean[]>([])

const REWRITE_INTENT_OPTIONS = [
  { label: '更细腻', value: '更细腻' },
  { label: '更爽', value: '更爽' },
  { label: '去 AI 味', value: '去AI味' },
  { label: '压缩', value: '压缩' },
]

const segments = computed(() => {
  const text = content.value
  if (!conflicts.value.length || !text) return [{ text, highlighted: false }]
  const sorted = [...conflicts.value].sort((a, b) => a.start - b.start)
  const out: { text: string; highlighted: boolean }[] = []
  let pos = 0
  for (const c of sorted) {
    if (c.start > pos) out.push({ text: text.slice(pos, c.start), highlighted: false })
    out.push({ text: text.slice(c.start, c.end), highlighted: true })
    pos = c.end
  }
  if (pos < text.length) out.push({ text: text.slice(pos), highlighted: false })
  return out
})

async function loadChapters() {
  if (!props.novelId) return
  chapters.value = await api.listChapters(props.novelId)
}

function selectChapter(c: Chapter) {
  selectedId.value = c.id
  title.value = c.title
  content.value = c.content
  directive.value = c.directive
  conflicts.value = []
}

function startNew() {
  selectedId.value = null
  title.value = ''
  directive.value = ''
  content.value = ''
  conflicts.value = []
  streamText.value = ''
}

async function reloadAndReselect() {
  await loadChapters()
  const c = chapters.value.find((x) => x.id === selectedId.value)
  if (c) {
    content.value = c.content
    title.value = c.title
    directive.value = c.directive
  }
}

async function doWrite() {
  if (streaming.value) return
  streaming.value = true
  streamText.value = ''
  conflicts.value = []
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/write`,
    { title: title.value, directive: directive.value },
    {
      onToken: (t) => (streamText.value += t),
      onDone: async () => {
        streaming.value = false
        await loadChapters()
        const last = chapters.value[chapters.value.length - 1]
        if (last) selectChapter(last)
        streamText.value = ''
        message.success('已生成新章')
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

async function doContinue() {
  if (!selectedId.value || streaming.value) return
  streaming.value = true
  streamText.value = ''
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/continue`,
    { directive: directive.value },
    {
      onToken: (t) => (streamText.value += t),
      onDone: async () => {
        streaming.value = false
        await reloadAndReselect()
        streamText.value = ''
        message.success('已续写')
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

function getSelection(): string {
  const el = contentRef.value
  if (!el) return ''
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  return end > start ? content.value.slice(start, end) : ''
}

async function doRewrite() {
  if (!selectedId.value || streaming.value) return
  const passage = getSelection().trim()
  if (!passage) {
    message.error('请先在正文中选中要改写的段落')
    return
  }
  streaming.value = true
  streamText.value = ''
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/rewrite`,
    { passage, intent: rewriteIntent.value },
    {
      onToken: (t) => (streamText.value += t),
      onDone: async () => {
        streaming.value = false
        await reloadAndReselect()
        streamText.value = ''
        message.success('已改写')
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

async function saveContent() {
  if (!selectedId.value) return
  try {
    await api.updateChapter(props.novelId, selectedId.value, {
      title: title.value,
      content: content.value,
      directive: directive.value,
    })
    message.success('已保存')
    await loadChapters()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function doConsistency() {
  if (!content.value.trim()) return
  try {
    const res = await api.checkConsistency(props.novelId, content.value)
    conflicts.value = res.conflicts
    if (res.conflicts.length) message.warning(`发现 ${res.conflicts.length} 处冲突`)
    else message.success('未发现冲突')
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function doSettle() {
  if (!selectedId.value) return
  try {
    const res = await api.settleChapter(props.novelId, selectedId.value)
    settleSummary.value = res.summary
    suggestions.value = res.suggestions
    checkedSuggestion.value = res.suggestions.map(() => true)
    showSettle.value = true
    await reloadAndReselect()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function confirmSettle() {
  if (!selectedId.value) return
  const picked = suggestions.value.filter((_, i) => checkedSuggestion.value[i])
  try {
    const res = await api.settleApply(props.novelId, selectedId.value, picked)
    message.success(`已落库 ${res.created.length} 条设定变更`)
    showSettle.value = false
  } catch (e) {
    message.error((e as Error).message)
  }
}

function suggestionLabel(s: Suggestion): string {
  const labels: Record<string, string> = {
    character: '人物',
    timeline_event: '事件',
    foreshadowing: '伏笔',
  }
  const name =
    (s.data.name as string) || (s.data.title as string) || (s.data.content as string) || ''
  return `[${labels[s.type] ?? s.type}] ${name}`
}

async function doExport(kind: 'markdown' | 'txt') {
  try {
    const text = kind === 'markdown' ? await api.exportMarkdown(props.novelId) : await api.exportTxt(props.novelId)
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = kind === 'markdown' ? 'novel.md' : 'novel.txt'
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    message.error((e as Error).message)
  }
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) {
      startNew()
      loadChapters()
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="writing-panel">
    <div class="chapter-sidebar">
      <div class="pane-header">
        <span>章节</span>
        <n-button size="small" type="primary" @click="startNew">新章</n-button>
      </div>
      <n-empty v-if="!chapters.length" description="暂无章节" />
      <n-list v-else clickable hoverable>
        <n-list-item
          v-for="c in chapters"
          :key="c.id"
          :class="{ active: c.id === selectedId }"
          @click="selectChapter(c)"
        >
          <div class="chapter-item">
            <span>第{{ c.seq }}章 {{ c.title }}</span>
            <n-tag size="tiny" :type="c.status === 'finalized' ? 'success' : 'default'">
              {{ c.status === 'finalized' ? '已定稿' : '草稿' }}
            </n-tag>
          </div>
        </n-list-item>
      </n-list>
    </div>

    <div class="editor">
      <div class="editor-toolbar">
        <n-input v-model:value="title" placeholder="章节标题" class="title-input" />
        <n-input v-model:value="directive" placeholder="章前指令（如：让 A 与 B 决裂）" class="directive-input" />
        <div class="btn-row">
          <n-button type="primary" :loading="streaming" @click="doWrite">写新章</n-button>
          <n-button :disabled="!selectedId" :loading="streaming" @click="doContinue">续写</n-button>
          <n-select
            v-model:value="rewriteIntent"
            :options="REWRITE_INTENT_OPTIONS"
            placeholder="改写意图"
            style="width: 120px"
            clearable
          />
          <n-button :disabled="!selectedId" @click="doRewrite">改写选中</n-button>
          <n-button :disabled="!selectedId" @click="saveContent">保存</n-button>
          <n-button :disabled="!selectedId" @click="doConsistency">一致性检查</n-button>
          <n-button type="success" :disabled="!selectedId" @click="doSettle">定稿沉淀</n-button>
          <n-button @click="doExport('markdown')">导出 MD</n-button>
          <n-button @click="doExport('txt')">导出 TXT</n-button>
        </div>
      </div>

      <div v-if="streaming" class="stream-box">
        <div class="stream-title">生成中…</div>
        <div class="stream-text">{{ streamText }}</div>
      </div>

      <div v-else-if="selectedId" class="editor-body">
        <textarea
          ref="contentRef"
          v-model="content"
          class="content-editor"
          placeholder="在此写作，或点击上方按钮生成"
        ></textarea>
      </div>
      <div v-else class="editor-empty">
        <n-empty description="选择章节或点击「新章」开始写作" />
      </div>

      <div v-if="conflicts.length" class="conflicts">
        <div class="conflicts-title">一致性冲突（高亮显示）</div>
        <div class="preview">
          <span v-for="(seg, i) in segments" :key="i" :class="{ hl: seg.highlighted }">{{
            seg.text
          }}</span>
        </div>
        <n-list>
          <n-list-item v-for="(c, i) in conflicts" :key="i">
            <n-tag type="error" size="small">{{ c.rule }}</n-tag>
            {{ c.message }}
          </n-list-item>
        </n-list>
      </div>
    </div>

    <n-modal v-model:show="showSettle" preset="card" title="设定变更建议" style="width: 560px">
      <p class="settle-summary">摘要：{{ settleSummary }}</p>
      <div v-if="suggestions.length">
        <div v-for="(s, i) in suggestions" :key="i" class="suggestion-row">
          <n-checkbox v-model:checked="checkedSuggestion[i]">{{ suggestionLabel(s) }}</n-checkbox>
        </div>
      </div>
      <n-empty v-else description="未识别到新增设定" />
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showSettle = false">取消</n-button>
          <n-button type="primary" @click="confirmSettle">确认落库</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.writing-panel {
  display: flex;
  height: 100%;
}
.chapter-sidebar {
  width: 240px;
  border-right: 1px solid #e5e5e5;
  padding: 12px;
  overflow: auto;
}
.pane-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-weight: 600;
}
.chapter-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.chapter-sidebar :deep(.n-list-item.active) {
  background: #e8f4ff;
}
.editor {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 12px 16px;
}
.editor-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.title-input {
  width: 100%;
}
.directive-input {
  width: 100%;
}
.btn-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.editor-body {
  flex: 1;
  min-height: 0;
}
.content-editor {
  width: 100%;
  height: 100%;
  min-height: 320px;
  padding: 12px;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  font-family: inherit;
  line-height: 1.7;
  resize: none;
  box-sizing: border-box;
}
.editor-empty {
  padding: 40px;
}
.stream-box {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 12px;
}
.stream-title {
  color: #888;
  margin-bottom: 8px;
}
.stream-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.conflicts {
  margin-top: 12px;
  border: 1px solid #f0c0c0;
  border-radius: 4px;
  padding: 12px;
  max-height: 240px;
  overflow: auto;
}
.conflicts-title {
  color: #d03050;
  font-weight: 600;
  margin-bottom: 8px;
}
.preview {
  white-space: pre-wrap;
  line-height: 1.7;
  margin-bottom: 8px;
}
.preview .hl {
  background: #ffe58f;
}
.settle-summary {
  color: #666;
}
.suggestion-row {
  margin: 6px 0;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
