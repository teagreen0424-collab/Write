<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NInput, NSelect, useDialog, useMessage } from 'naive-ui'
import { api } from '../api'
import type { Chapter, OutlineNode } from '../types'
import { chapterGenerationOperation, chapterGenerationState, type ChapterGenerationState } from '../utils/chapterGeneration'

const props = defineProps<{
  novelId: string
  conversationId: string
  messageId: string
  sourceText: string
  actionsReady?: boolean
}>()
const emit = defineEmits<{
  (e: 'applied', chapter: Chapter): void
  (e: 'cancel'): void
}>()

const toast = useMessage()
const dialog = useDialog()
const state = ref(chapterGenerationState(props.novelId, props.conversationId, props.messageId, props.sourceText))
let alive = true, sourceEpoch = 0, loadSequence = 0
const sourceLoading = ref(false)
const sourceError = ref('')
const copyFallback = ref(false)
const ideaCopyFallback = ref(false)
const candidateRef = ref<HTMLTextAreaElement | null>(null)
const ideaRef = ref<HTMLTextAreaElement | null>(null)

function field(name: 'title' | 'directive' | 'outlineNodeId') {
  return computed({ get: () => state.value[name], set: (value: string | null) => {
    const text = value ?? ''
    if (state.value[name] === text) return
    state.value[name] = text
    state.value.revision += 1
    if (name === 'outlineNodeId') state.value.outlineRevision += 1
    if (state.value.outcome === 'saved') state.value.unsavedEdits = true
  } })
}
const title = field('title'), directive = field('directive'), outlineNodeId = field('outlineNodeId')
const previousChapterId = computed(() => state.value.previousChapterId)
const outlineOptions = computed(() => state.value.outlineOptions)
const latestTitle = computed(() => state.value.latestTitle)
const generating = computed(() => state.value.pending !== null)
const previewText = computed(() => state.value.candidate)
const candidateStatus = computed(() => state.value.outcome)
const savedChapter = computed(() => state.value.savedChapter)
const unsavedEdits = computed(() => state.value.unsavedEdits)
const candidateDiffers = computed(() => state.value.outcome === 'saved' && !!state.value.candidate && state.value.candidate !== state.value.savedChapter?.content)
const currentIdea = computed(() => `标题：${title.value}\n章前指令：${directive.value}\n大纲：${outlineOptions.value.find(option => option.value === outlineNodeId.value)?.label ?? '未绑定'}`)

function captureSource() {
  return { novelId: props.novelId, conversationId: props.conversationId, messageId: props.messageId,
    sourceText: props.sourceText, epoch: sourceEpoch, state: state.value }
}
function current(source: ReturnType<typeof captureSource>) {
  return alive && source.epoch === sourceEpoch && source.state === state.value
    && source.novelId === props.novelId && source.conversationId === props.conversationId
    && source.messageId === props.messageId && source.sourceText === props.sourceText
}

async function loadSource(explicit = true) {
  const source = captureSource(), request = ++loadSequence, outlineRevision = source.state.outlineRevision
  if (!source.novelId || !source.conversationId || !source.messageId || generating.value) return false
  sourceLoading.value = true; sourceError.value = ''
  try {
    const [chapters, outline] = await Promise.all([
      api.listChapterMetadata(source.novelId), api.listOutline(source.novelId),
    ])
    if (!current(source) || request !== loadSequence) return false
    const latest = chapters[chapters.length - 1]
    source.state.previousChapterId = latest?.id ?? ''
    source.state.latestTitle = latest ? `承接第 ${latest.seq} 章《${latest.title}》` : '暂无章节，从第一章开始'
    const chapterNodes = (outline as OutlineNode[]).filter((node) => node.level === 'chapter')
    source.state.outlineOptions = chapterNodes.map((node) => ({ label: node.title, value: node.id }))
    const currentIndex = chapterNodes.findIndex((node) => node.id === latest?.outline_node_id)
    const nextIndex = currentIndex >= 0 ? currentIndex + 1 : chapters.length
    if (!source.state.sourceLoaded && source.state.outlineRevision === outlineRevision) source.state.outlineNodeId = chapterNodes[nextIndex]?.id ?? ''
    source.state.sourceLoaded = true; source.state.needsSourceRefresh = false
    if (explicit) toast.info('已读取最新章节来源，构思和候选仍保留；尚未重新生成')
    return true
  } catch (error) {
    if (current(source) && request === loadSequence) {
      sourceError.value = `章节来源读取失败：${(error as Error).message}`
      source.state.needsSourceRefresh = true
      toast.error(sourceError.value)
    }
    return false
  } finally {
    if (current(source) && request === loadSequence) sourceLoading.value = false
  }
}

watch(() => [props.novelId, props.conversationId, props.messageId, props.sourceText], () => {
  sourceEpoch += 1; loadSequence += 1
  state.value = chapterGenerationState(props.novelId, props.conversationId, props.messageId, props.sourceText)
  sourceLoading.value = false; sourceError.value = ''; copyFallback.value = false; ideaCopyFallback.value = false
  if (!state.value.sourceLoaded && !state.value.pending) void loadSource(false)
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { alive = false; sourceEpoch += 1; loadSequence += 1 })

function confirmDiscardCandidate() {
  return new Promise<boolean>(resolve => {
    let resolved = false
    const finish = (value: boolean) => { if (!resolved) { resolved = true; resolve(value) } }
    dialog.warning({ title: '确认重新生成', content: '重新生成会放弃卡片中的旧候选。请先复制需要保留的文本；本次不会自动应用旧候选。',
      positiveText: '放弃旧候选并生成', negativeText: '继续保留',
      onPositiveClick: () => finish(true), onNegativeClick: () => finish(false), onClose: () => finish(false), onMaskClick: () => finish(false),
    })
  })
}

function validChapter(value: unknown, novelId: string): value is Chapter {
  if (!value || typeof value !== 'object') return false
  const chapter = value as Chapter
  return typeof chapter.id === 'string' && !!chapter.id && chapter.novel_id === novelId
    && Number.isInteger(chapter.seq) && chapter.seq > 0 && typeof chapter.title === 'string'
    && typeof chapter.content === 'string' && typeof chapter.directive === 'string'
    && typeof chapter.outline_node_id === 'string' && typeof chapter.summary === 'string'
    && (chapter.status === 'draft' || chapter.status === 'finalized')
    && [chapter.tension_score, chapter.drift_score].every(score => score === null || (typeof score === 'number' && Number.isFinite(score)))
    && typeof chapter.created_at === 'string' && typeof chapter.updated_at === 'string'
}

function failed(target: ChapterGenerationState, error: string, code?: string, status?: number) {
  target.outcome = code === 'chapter_write_conflict' || (status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429) ? 'failed' : 'unknown'
  target.error = error || '未收到生成完成确认，请保留候选并核对'
  target.needsSourceRefresh = true
}

async function write() {
  const source = captureSource(), target = source.state
  const actionsAvailable = () => props.actionsReady !== false
  if (!source.novelId || !source.conversationId || !source.messageId || target.pending !== null || sourceLoading.value || target.outcome === 'saved') return
  if (!actionsAvailable()) { toast.warning('动作记录尚未核对，当前构思与候选仍保留；请先重试动作记录'); return }
  if (!target.sourceLoaded || target.needsSourceRefresh) { toast.warning('请先明确读取最新章节来源；候选和构思仍保留'); return }
  if (!directive.value.trim()) {
    toast.warning('请确认或编辑章前指令')
    return
  }
  const operation = chapterGenerationOperation(), revision = target.revision
  const payload = { title: title.value.trim(), directive: directive.value.trim(), outline_node_id: outlineNodeId.value, previous_chapter_id: previousChapterId.value }
  target.pending = operation
  try {
    if (target.candidate || target.outcome === 'failed' || target.outcome === 'unknown') {
      if (!await confirmDiscardCandidate() || !current(source) || target.revision !== revision || target.pending !== operation) return
    }
    if (!current(source) || target.pending !== operation || !actionsAvailable()) return
    target.candidate = ''; target.outcome = 'generating'; target.error = ''; copyFallback.value = false
    await api.writeMessageChapter(
      source.conversationId, source.messageId, payload,
      {
        onToken: (text) => { if (target.pending === operation) target.candidate += text },
        onDone: (data) => {
          if (target.pending !== operation) return
          if (!validChapter(data?.chapter, source.novelId)) {
            failed(target, '完成回包缺少对应章节详情，保存状态尚未确认，请复制候选并核对目录')
            if (current(source)) toast.warning(target.error)
            return
          }
          target.savedChapter = data.chapter; target.outcome = 'saved'; target.error = ''
          // 已落地动作的重放可返回旧章；回执不能把本次不同构思冒称为已保存。
          target.unsavedEdits = target.revision !== revision || data.chapter.directive !== payload.directive
            || (!!payload.title && data.chapter.title !== payload.title) || data.chapter.outline_node_id !== payload.outline_node_id
          if (!current(source)) return
          toast.success(target.unsavedEdits ? '章节草稿已保存；等待期间的新构思尚未保存，请先复制' : '章节草稿已保存')
          if (!target.unsavedEdits && (!target.candidate || target.candidate === data.chapter.content)) emit('applied', data.chapter)
        },
        onError: (error, code, status) => {
          if (target.pending !== operation) return
          failed(target, error, code, status)
          if (current(source)) toast.error(error)
        },
      },
    )
    if (target.pending === operation && target.outcome === 'generating') failed(target, '未收到完成确认，请保留候选并核对章节目录')
  } catch (error) {
    if (target.pending === operation) {
      failed(target, (error as Error).message)
      if (current(source)) toast.error(target.error)
    }
  } finally {
    if (target.pending === operation) target.pending = null
  }
}

async function copyCandidate() {
  if (!previewText.value) return
  const source = captureSource()
  try { await navigator.clipboard.writeText(previewText.value); if (current(source)) toast.success('已复制章节候选') }
  catch { if (current(source)) { copyFallback.value = true; await nextTick(); if (current(source)) { candidateRef.value?.focus(); candidateRef.value?.select() } } }
}
async function copyCurrentIdea() {
  const source = captureSource()
  try { await navigator.clipboard.writeText(currentIdea.value); if (current(source)) toast.success('已复制当前构思') }
  catch { if (current(source)) { ideaCopyFallback.value = true; await nextTick(); if (current(source)) { ideaRef.value?.focus(); ideaRef.value?.select() } } }
}
function finishSaved() {
  const source = captureSource(), revision = source.state.revision, chapter = source.state.savedChapter
  if (!chapter || source.state.pending !== null) return
  const finish = () => { if (current(source) && source.state.revision === revision) emit('applied', chapter) }
  if (!source.state.unsavedEdits && !candidateDiffers.value) { finish(); return }
  dialog.warning({ title: '完成本次写章', content: '章节保存回执已确认，但卡片中仍有未保存构思或不同的候选。请先复制需要保留的内容；完成后将关闭卡片。',
    positiveText: '已保留构思，完成', negativeText: '继续编辑', onPositiveClick: finish,
  })
}
function cancelCard() { if (generating.value) return; if (savedChapter.value) finishSaved(); else emit('cancel') }
</script>

<template>
  <div class="write-card">
    <p>确认后创建一章草稿；{{ latestTitle }}。生成期间若最新章或章序改变，将拒绝落库。</p>
    <p v-if="sourceError" role="alert">{{ sourceError }}</p>
    <n-input v-model:value="title" placeholder="新章标题（留空则自动编号）" />
    <n-input v-model:value="directive" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" placeholder="章前指令" />
    <n-select v-model:value="outlineNodeId" :options="outlineOptions" placeholder="可选：绑定章级大纲" clearable />
    <div class="actions">
      <n-button size="small" :disabled="generating" @click="cancelCard">{{ savedChapter ? '完成本次写章' : '关闭并保留页内稿件' }}</n-button>
      <n-button v-if="!savedChapter" size="small" :disabled="generating || sourceLoading" :loading="sourceLoading" @click="loadSource()">读取最新章节来源</n-button>
      <n-button v-if="!savedChapter" size="small" type="primary" :loading="generating" :disabled="generating || sourceLoading || !state.sourceLoaded || state.needsSourceRefresh || actionsReady === false" @click="write">{{ candidateStatus === 'idle' ? '确认写这一章' : '确认重新生成' }}</n-button>
    </div>
    <section v-if="previewText || candidateStatus !== 'idle'" aria-label="聊天写章候选" class="preview">
      <p role="status">{{ candidateStatus === 'saved' ? '章节草稿已保存' : candidateStatus === 'unknown' ? '候选保存状态未确认' : candidateStatus === 'failed' ? '未完成，候选未保存' : '生成中，候选尚未确认保存' }}</p>
      <p v-if="candidateDiffers" role="alert">本次候选与已保存章节不同，以下候选未保存；请查看实际章节。</p>
      <p v-if="state.error" role="alert">{{ state.error }}</p>
      <p v-if="unsavedEdits">等待期间的新构思尚未保存，仍保留在上方；请先复制再完成本次写章。</p>
      <p>候选与当前构思在本页面内暂留；完整刷新或关闭页面前，请先复制需要保留的内容。</p>
      <n-button size="small" :disabled="!previewText" @click="copyCandidate">复制候选</n-button>
      <n-button size="small" @click="copyCurrentIdea">复制当前构思</n-button>
      <p v-if="copyFallback">自动复制失败，请在下方选择并复制章节候选。</p>
      <textarea ref="candidateRef" :value="previewText" readonly aria-label="未完成章节候选" class="candidate-text"></textarea>
      <p v-if="ideaCopyFallback">自动复制失败，请在下方选择并复制当前构思。</p>
      <textarea v-if="ideaCopyFallback" ref="ideaRef" :value="currentIdea" readonly aria-label="当前构思" class="candidate-text"></textarea>
    </section>
  </div>
</template>

<style scoped>
.write-card { display: grid; gap: 10px; width: min(680px, 90vw); padding: 12px; background: #fafafa; border: 1px solid #e5e7eb; border-radius: 8px; }
.write-card p { margin: 0; color: #666; }
.actions { display: flex; gap: 8px; justify-content: flex-end; }
.preview { white-space: pre-wrap; max-height: 300px; overflow: auto; }
.candidate-text { display: block; box-sizing: border-box; width: 100%; min-height: 140px; margin-top: 8px; resize: vertical; }
</style>
