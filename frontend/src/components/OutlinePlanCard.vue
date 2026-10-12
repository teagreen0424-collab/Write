<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NInput, NSpin, useMessage } from 'naive-ui'
import { api } from '../api'
import type { OutlineApplyResult, OutlineVolumeDraft } from '../types'
import { createAsyncScope } from '../utils/asyncScope'

const props = defineProps<{
  novelId: string
  conversationId?: string
  messageId?: string
}>()
const emit = defineEmits<{
  (e: 'applied', result: OutlineApplyResult): void
  (e: 'cancel'): void
}>()

const toast = useMessage()
const sourceText = ref('')
const plan = ref<OutlineVolumeDraft | null>(null)
const loading = ref(false)
const applying = ref(false)
const appendSubmission = ref<{
  requestId: string; draft: OutlineVolumeDraft; sourceRevision: number; planRevision: number
} | null>(null)
const appendUnconfirmed = ref(false)
const originalAppendText = computed(() => {
  const original = appendSubmission.value?.draft
  if (!original) return ''
  const chapterText = (chapter: { title: string; summary: string }) => `章：${chapter.title}\n${chapter.summary}`
  return [`卷：${original.title}`, original.summary, ...original.chapters.map(chapterText),
    ...original.acts.flatMap(act => [`幕：${act.title}`, act.summary, ...act.chapters.map(chapterText)])].join('\n\n')
})
const asyncScope = createAsyncScope(() => JSON.stringify([props.novelId, props.conversationId ?? '', props.messageId ?? '']))
let previewSequence = 0
let applySequence = 0
let sourceRevision = 0
let planRevision = 0
watch(sourceText, () => {
  sourceRevision += 1
  previewSequence += 1
  loading.value = false
}, { flush: 'sync' })
watch(() => JSON.stringify(plan.value), () => { planRevision += 1 }, { flush: 'sync' })
watch(() => [props.novelId, props.conversationId, props.messageId], () => {
  asyncScope.invalidate()
  previewSequence += 1
  applySequence += 1
  sourceText.value = ''
  plan.value = null
  loading.value = false
  applying.value = false
  appendSubmission.value = null
  appendUnconfirmed.value = false
}, { flush: 'sync' })

async function preview() {
  if (!props.novelId || loading.value || applying.value || appendUnconfirmed.value) return
  if (!props.messageId && !sourceText.value.trim()) {
    toast.warning('请先写下这卷的构思')
    return
  }
  const scope = asyncScope.capture()
  const sequence = ++previewSequence
  const novelId = props.novelId, conversationId = props.conversationId, messageId = props.messageId
  const submittedSource = sourceText.value.trim()
  const expectedSourceRevision = sourceRevision
  const expectedPlanRevision = planRevision
  const isCurrent = () => asyncScope.isCurrent(scope) && sequence === previewSequence && sourceRevision === expectedSourceRevision
  loading.value = true
  try {
    const result = conversationId && messageId
      ? await api.previewMessageOutline(conversationId, messageId)
      : await api.previewOutline(novelId, submittedSource)
    if (isCurrent() && planRevision === expectedPlanRevision) plan.value = result
  } catch (error) {
    if (isCurrent()) toast.error((error as Error).message)
  } finally {
    if (isCurrent()) loading.value = false
  }
}

async function apply() {
  if (!props.novelId || applying.value || loading.value) return
  const draft: OutlineVolumeDraft | null = appendSubmission.value?.draft ?? (plan.value ? JSON.parse(JSON.stringify(plan.value)) : null)
  if (!draft || !draft.title.trim()) {
    toast.warning('卷标题不能为空')
    return
  }
  if (!draft.chapters.length && !draft.acts.some((act) => act.chapters.length)) {
    toast.warning('请至少保留一个章节')
    return
  }
  const scope = asyncScope.capture()
  const sequence = ++applySequence
  const novelId = props.novelId, conversationId = props.conversationId, messageId = props.messageId
  const workbench = !conversationId || !messageId
  if (workbench && !appendSubmission.value) appendSubmission.value = {
    requestId: crypto.randomUUID(), draft, sourceRevision, planRevision,
  }
  const submission = appendSubmission.value
  const expectedSourceRevision = submission?.sourceRevision ?? sourceRevision
  const expectedPlanRevision = submission?.planRevision ?? planRevision
  const isCurrent = () => asyncScope.isCurrent(scope) && sequence === applySequence
  const wasUnconfirmed = appendUnconfirmed.value
  const qualifyReceipt = (result: OutlineApplyResult) => {
    const count = 1 + draft.chapters.length + draft.acts.reduce((total, act) => total + 1 + act.chapters.length, 0)
    if (!result || result.request_id !== submission!.requestId || typeof result.append_replayed !== 'boolean'
      || typeof result.volume_id !== 'string' || !result.volume_id
      || !Array.isArray(result.node_ids) || result.node_ids.length !== count
      || result.node_ids[0] !== result.volume_id || new Set(result.node_ids).size !== count
      || result.node_ids.some(id => typeof id !== 'string' || !id)) {
      throw new Error('服务未返回完整的原追加回执，请更新并重启服务后核对；原稿已保留')
    }
  }
  applying.value = true
  try {
    let result: OutlineApplyResult
    if (conversationId && messageId) result = await api.applyMessageOutline(conversationId, messageId, draft)
    else if (wasUnconfirmed) {
      try {
        result = await api.checkOutlineAppend(novelId, submission!.requestId, draft)
      } catch (error) {
        if (!isCurrent()) return
        const failure = error as Error & { status?: number; code?: string }
        if (failure.status === 404 && failure.code === 'outline_append_not_found') {
          result = await api.applyOutline(novelId, draft, submission!.requestId)
        } else if (failure.status === 404 || failure.status === 405) {
          throw new Error('当前服务不支持原追加核对，请更新并重启服务后再核对；不会再次追加')
        } else throw error
      }
    } else result = await api.applyOutline(novelId, draft, submission!.requestId)
    if (!isCurrent()) return
    if (workbench) qualifyReceipt(result)
    appendSubmission.value = null
    appendUnconfirmed.value = false
    if (sourceRevision !== expectedSourceRevision || planRevision !== expectedPlanRevision) {
      toast.success('大纲已追加到作品，当前新增编辑已保留')
      return
    }
    toast.success('大纲已追加到作品')
    emit('applied', result)
  } catch (error) {
    if (!isCurrent()) return
    const failure = error as Error & { status?: number; code?: string }
    if (workbench && (!failure.status || failure.status >= 500 || failure.status === 408 || failure.status === 429 || failure.code === 'outline_append_busy')) {
      appendUnconfirmed.value = true
      toast.warning(`大纲追加结果未确认：${failure.message}。请核对原追加，原提交与当前新编辑均保留。`)
    } else {
      if (workbench && !appendUnconfirmed.value) appendSubmission.value = null
      toast.error(failure.message)
    }
  } finally {
    if (isCurrent()) applying.value = false
  }
}
onBeforeUnmount(() => {
  asyncScope.dispose()
  previewSequence += 1
  applySequence += 1
})
</script>

<template>
  <div class="planner">
    <p>先预览、可编辑，确认后仅追加一卷，不覆盖已有大纲。</p>
    <n-input
      v-if="!messageId"
      v-model:value="sourceText"
      type="textarea"
      :autosize="{ minRows: 2, maxRows: 5 }"
      placeholder="描述这一卷的人物、目标、冲突与预期转折"
    />
    <div class="actions">
      <n-button size="small" :loading="loading" :disabled="applying || appendUnconfirmed" @click="preview">
        {{ plan ? '重新生成预览' : '生成预览' }}
      </n-button>
      <n-button size="small" @click="emit('cancel')">关闭</n-button>
    </div>
    <div v-if="appendUnconfirmed && appendSubmission" class="append-warning" role="status">
      <p>本次追加可能已经保存。核对先读取原追加回执；只有服务明确查无原请求才按原大纲重试。旧服务不支持核对时，请更新并重启服务，原稿保持可复制。</p>
      <p>关闭后将无法在这次预览中核对，请先复制下方原大纲与当前编辑。</p>
      <n-input :value="originalAppendText" type="textarea" readonly :autosize="{ minRows: 2, maxRows: 5 }" :input-props="{ 'aria-label': '原追加大纲，可手动复制' }" />
    </div>
    <n-spin :show="loading">
      <div v-if="plan" class="draft">
        <label>卷标题<n-input v-model:value="plan.title" /></label>
        <label>卷概要<n-input v-model:value="plan.summary" type="textarea" /></label>
        <div class="group">
          <strong>直属章节</strong>
          <div v-for="(chapter, index) in plan.chapters" :key="index" class="item">
            <n-input v-model:value="chapter.title" placeholder="章节标题" />
            <n-input v-model:value="chapter.summary" placeholder="章节概要" />
            <n-button size="tiny" @click="plan.chapters.splice(index, 1)">移除</n-button>
          </div>
          <n-button size="tiny" @click="plan.chapters.push({ title: '', summary: '' })">+ 直属章节</n-button>
        </div>
        <div v-for="(act, actIndex) in plan.acts" :key="actIndex" class="group">
          <div class="actions">
            <strong>第 {{ actIndex + 1 }} 幕</strong>
            <n-button size="tiny" @click="plan.acts.splice(actIndex, 1)">移除幕</n-button>
          </div>
          <n-input v-model:value="act.title" placeholder="幕标题" />
          <n-input v-model:value="act.summary" placeholder="幕概要" />
          <div v-for="(chapter, index) in act.chapters" :key="index" class="item">
            <n-input v-model:value="chapter.title" placeholder="章节标题" />
            <n-input v-model:value="chapter.summary" placeholder="章节概要" />
            <n-button size="tiny" @click="act.chapters.splice(index, 1)">移除</n-button>
          </div>
          <n-button size="tiny" @click="act.chapters.push({ title: '', summary: '' })">+ 幕内章节</n-button>
        </div>
        <div class="actions confirmation-actions">
          <n-button size="tiny" @click="plan.acts.push({ title: '', summary: '', chapters: [] })">+ 幕</n-button>
          <n-button type="primary" size="small" :loading="applying" @click="apply">{{ appendUnconfirmed ? '核对原追加' : '确认追加到大纲' }}</n-button>
        </div>
      </div>
    </n-spin>
  </div>
</template>

<style scoped>
.planner { display: grid; gap: 12px; }
.planner p { margin: 0; color: #666; }
.draft, .group { display: grid; gap: 8px; min-width: 0; }
.group { border: 1px solid #e5e7eb; padding: 10px; border-radius: 6px; }
.item, .actions { display: flex; align-items: center; gap: 8px; }
.item > :first-child { flex: 1; min-width: 120px; }
.item > :nth-child(2) { flex: 1; min-width: 0; }
.confirmation-actions { position: sticky; bottom: 0; padding: 8px 0; background: #fff; z-index: 1; }
@media (max-width: 600px) {
  .item { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
  .item > :first-child { min-width: 0; }
  .item > :nth-child(2) { grid-column: 1; }
  .item > :last-child { grid-column: 2; grid-row: 1 / span 2; }
}
</style>
