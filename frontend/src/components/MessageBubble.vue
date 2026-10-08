<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { Chapter, ChatAction, ChatMessage, OutlineApplyResult } from '../types'
import ApplySuggestionCard from './ApplySuggestionCard.vue'
import OutlinePlanCard from './OutlinePlanCard.vue'
import ChapterWriteCard from './ChapterWriteCard.vue'
import { findChapterGenerationState } from '../utils/chapterGeneration'
import { generationConsistencyMessage } from '../utils/generationConsistency'

const props = defineProps<{ message: ChatMessage; novelId: string | null; actions?: ChatAction[]; actionsStatus?: 'pending' | 'ready' | 'failed' }>()
const emit = defineEmits<{
  (e: 'open-outline', novelId: string): void
  (e: 'open-writing', novelId: string, chapterId: string): void
  (e: 'reload-actions'): void
  (e: 'message-updated', message: ChatMessage): void
  (e: 'action-confirmed', action: ChatAction): void
}>()

const toast = useMessage()
let alive = true
let actionEpoch = 0
const standaloneActionsStatus = ref<'pending' | 'ready' | 'failed'>('pending')
const actionsStatus = computed(() => props.actionsStatus ?? standaloneActionsStatus.value)
const showApply = ref(false)
const showSettings = ref(false)
const showOutline = ref(false)
const showWriting = ref(false)
const outlineResult = ref<OutlineApplyResult | null>(null)
const chapterResult = ref<{ chapter_id: string; title: string } | null>(null)
const settingsApplied = ref(false)
const outlineDeleted = ref(false)
const chapterDeleted = ref(false)
const appliedInfo = ref<Record<string, unknown> | null>(
  props.message.meta && (props.message.meta as any).applied ? props.message.meta : null,
)

const isAssistant = computed(() => props.message.role === 'assistant')
const hasActionSource = computed(() => isAssistant.value && !!props.novelId && !props.message.id.startsWith('local-'))
const writingDraft = computed(() => hasActionSource.value
  ? findChapterGenerationState(props.novelId!, props.message.conversation_id, props.message.id) : undefined)
const hasWritingDraft = computed(() => {
  const draft = writingDraft.value
  return !!draft && (!!draft.candidate || draft.revision > 0 || draft.outcome !== 'idle' || draft.pending !== null)
})
const canApply = computed(
  () => hasActionSource.value && actionsStatus.value === 'ready',
)

interface InlinePart {
  text: string
  bold: boolean
}

interface ContentBlock {
  type: 'paragraph' | 'list'
  lines: InlinePart[][]
}

function parseInline(text: string): InlinePart[] {
  const parts: InlinePart[] = []
  const pattern = /\*\*(.+?)\*\*/g
  let position = 0
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > position) parts.push({ text: text.slice(position, index), bold: false })
    parts.push({ text: match[1], bold: true })
    position = index + match[0].length
  }
  if (position < text.length) parts.push({ text: text.slice(position), bold: false })
  return parts.length ? parts : [{ text, bold: false }]
}

const contentBlocks = computed<ContentBlock[]>(() => {
  const paragraphs = props.message.content.trim().split(/\n\s*\n/)
  return paragraphs.filter(Boolean).map((paragraph) => {
    const lines = paragraph.split('\n')
    const isList = lines.every((line) => /^\s*[-*]\s+/.test(line))
    return {
      type: isList ? 'list' : 'paragraph',
      lines: lines.map((line) => parseInline(isList ? line.replace(/^\s*[-*]\s+/, '') : line)),
    }
  })
})

async function onApplied(summary: { type: string; action: string; id: string }[]) {
  const meta = { applied: summary, at: new Date().toISOString() }
  const source = [props.novelId, props.message.conversation_id, props.message.id].join(':')
  try {
    const updated = await api.updateMessage(props.message.conversation_id, props.message.id, meta)
    if (!alive || source !== [props.novelId, props.message.conversation_id, props.message.id].join(':')) return
    appliedInfo.value = meta
    showApply.value = false
    toast.success('已应用到作品')
    emit('message-updated', updated)
    if (props.actionsStatus !== undefined) emit('reload-actions')
  } catch (e) {
    if (alive && source === [props.novelId, props.message.conversation_id, props.message.id].join(':')) toast.error((e as Error).message)
  }
}

function adoptActions(actions: ChatAction[]) {
  const summary = (value: unknown): Record<string, unknown> | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
  const outline = actions.find((action) => action.action_type === 'outline')
  const outlineSummary = summary(outline?.result_summary)
  outlineResult.value = typeof outlineSummary?.volume_id === 'string' && outlineSummary.volume_id ? outlineSummary as unknown as OutlineApplyResult : null
  outlineDeleted.value = !!outline?.target_deleted
  const chapter = actions.find((action) => action.action_type === 'chapter')
  const chapterSummary = summary(chapter?.result_summary)
  chapterResult.value = typeof chapterSummary?.chapter_id === 'string' && chapterSummary.chapter_id ? chapterSummary as unknown as { chapter_id: string; title: string } : null
  chapterDeleted.value = !!chapter?.target_deleted
  settingsApplied.value = actions.some((action) => action.action_type === 'settings')
}
async function loadStandaloneActions() {
  if (props.actionsStatus !== undefined) { emit('reload-actions'); return }
  const epoch = ++actionEpoch, source = [props.novelId, props.message.conversation_id, props.message.id].join(':')
  if (!props.novelId || !isAssistant.value || props.message.id.startsWith('local-')) { standaloneActionsStatus.value = 'ready'; return }
  standaloneActionsStatus.value = 'pending'
  try {
    const actions = await api.listMessageActions(props.message.conversation_id, props.message.id)
    if (!alive || epoch !== actionEpoch || source !== [props.novelId, props.message.conversation_id, props.message.id].join(':')) return
    adoptActions(actions); standaloneActionsStatus.value = 'ready'
  } catch {
    if (alive && epoch === actionEpoch) standaloneActionsStatus.value = 'failed'
  }
}
watch(
  () => [props.novelId, props.message.id, props.message.conversation_id, props.actions],
  () => {
    actionEpoch += 1
    outlineResult.value = null
    chapterResult.value = null
    settingsApplied.value = false
    outlineDeleted.value = false
    chapterDeleted.value = false
    if (props.actionsStatus !== undefined) adoptActions(props.actions ?? [])
    else void loadStandaloneActions()
  },
  { immediate: true },
)
watch(() => props.message.meta, meta => { appliedInfo.value = meta && (meta as any).applied ? meta : null })
onBeforeUnmount(() => { alive = false; actionEpoch += 1 })

function onOutlineApplied(result: OutlineApplyResult) {
  if (!alive) return
  outlineResult.value = result
  showOutline.value = false
  if (props.actionsStatus !== undefined) {
    emit('action-confirmed', { action_type: 'outline', status: 'done', result_id: result.volume_id, result_summary: result, target_deleted: false })
    emit('reload-actions')
  }
}

function onChapterApplied(chapter: Chapter) {
  if (!alive) return
  chapterResult.value = { chapter_id: chapter.id, title: chapter.title }
  showWriting.value = false
  if (props.actionsStatus !== undefined) {
    emit('action-confirmed', { action_type: 'chapter', status: 'done', result_id: chapter.id, result_summary: { chapter_id: chapter.id, title: chapter.title }, target_deleted: false })
    emit('reload-actions')
  }
}

function onSettingsApplied(summary: { type: string; action: string; id: string }[] = []) {
  if (!alive) return
  settingsApplied.value = true
  showSettings.value = false
  if (props.actionsStatus !== undefined) {
    if (summary.length) emit('action-confirmed', { action_type: 'settings', status: 'done', result_id: summary[0].id, result_summary: { applied: summary }, target_deleted: false })
    emit('reload-actions')
  }
}
</script>

<template>
  <div class="bubble-row" :class="message.role">
    <div class="bubble" :class="message.role">
      <div class="content">
        <template v-for="(block, blockIndex) in contentBlocks" :key="blockIndex">
          <ul v-if="block.type === 'list'">
            <li v-for="(line, lineIndex) in block.lines" :key="lineIndex">
              <span v-for="(part, partIndex) in line" :key="partIndex" :class="{ strong: part.bold }">
                {{ part.text }}
              </span>
            </li>
          </ul>
          <p v-else>
            <template v-for="(line, lineIndex) in block.lines" :key="lineIndex">
              <span v-for="(part, partIndex) in line" :key="partIndex" :class="{ strong: part.bold }">
                {{ part.text }}
              </span>
              <br v-if="lineIndex < block.lines.length - 1" />
            </template>
          </p>
        </template>
      </div>
      <div v-if="isAssistant" class="footer">
        <span v-if="novelId && !message.id.startsWith('local-') && actionsStatus !== 'ready'" role="status">{{ actionsStatus === 'pending' ? '动作记录读取中…' : '动作记录读取未完成' }}</span>
        <n-button v-if="actionsStatus === 'failed'" size="tiny" quaternary @click="loadStandaloneActions">重试动作记录</n-button>
        <n-tag v-if="appliedInfo?.applied" type="success" size="small">已落地</n-tag>
        <n-button
          v-else
          size="tiny"
          quaternary
          :disabled="!canApply"
          :title="canApply ? '将该回复解析为设定变更并应用到作品' : '先转为作品'"
          @click="showApply = !showApply"
        >
          应用到作品
        </n-button>
        <n-button
          v-if="outlineResult && novelId"
          size="tiny"
          quaternary
          :disabled="outlineDeleted"
          @click="emit('open-outline', novelId)"
        >{{ outlineDeleted ? '大纲目标已删除' : '大纲已落地 · 查看' }}</n-button>
        <n-button
          v-else
          size="tiny"
          quaternary
          :disabled="!canApply"
          :title="canApply ? '将这条讨论生成一卷可编辑大纲预览' : '先转为作品'"
          @click="showOutline = !showOutline"
        >生成大纲</n-button>
        <n-button
          v-if="chapterResult && novelId"
          size="tiny" quaternary :disabled="chapterDeleted"
          @click="emit('open-writing', novelId, chapterResult.chapter_id)"
        >{{ chapterDeleted ? '章节目标已删除' : '章节已落地 · 查看' }}</n-button>
        <n-button
          v-else size="tiny" quaternary :disabled="!canApply"
          :title="canApply ? '确认指令后生成一章草稿' : '先转为作品'"
          @click="showWriting = !showWriting"
        >写这一章</n-button>
        <n-button v-if="hasWritingDraft" size="tiny" quaternary aria-label="写章构思与候选" @click="showWriting = !showWriting">写章构思与候选</n-button>
        <n-tag v-if="settingsApplied" type="success" size="small">设定已提炼</n-tag>
        <n-button
          v-else size="tiny" quaternary :disabled="!canApply"
          :title="canApply ? '提炼人物、世界观与伏笔草稿' : '先转为作品'"
          @click="showSettings = !showSettings"
        >提炼设定</n-button>
      </div>
    </div>
    <div v-if="showApply && hasActionSource" :inert="actionsStatus !== 'ready'">
    <ApplySuggestionCard
      :novel-id="novelId!"
      :text="message.content"
      @applied="onApplied"
      @cancel="showApply = false"
    />
    </div>
    <div v-if="showOutline && hasActionSource" class="outline-card" :inert="actionsStatus !== 'ready'">
      <OutlinePlanCard
        :novel-id="novelId!"
        :conversation-id="message.conversation_id"
        :message-id="message.id"
        @applied="onOutlineApplied"
        @cancel="showOutline = false"
      />
    </div>
    <section v-if="writingDraft?.ruleCheck" aria-label="聊天写章规则检查" class="generation-rule-result">
      <p role="status">{{ generationConsistencyMessage(writingDraft.ruleCheck) }}</p>
      <p>对应本页生成时返回的章节正文。{{ chapterDeleted ? '章节目标已删除，此处仅保留当时的检查记录。' : '通过“写章构思与候选”可查看完整记录。' }}</p>
      <ul v-if="writingDraft.ruleCheck.conflicts.length">
        <li v-for="(item, i) in writingDraft.ruleCheck.conflicts" :key="i">{{ item.message }}</li>
      </ul>
      <p v-for="(warning, i) in writingDraft.ruleCheck.warnings" :key="i">{{ warning }}</p>
    </section>
    <div v-if="showWriting && hasActionSource" :inert="actionsStatus !== 'ready' && !hasWritingDraft">
    <ChapterWriteCard
      :novel-id="novelId!"
      :conversation-id="message.conversation_id"
      :message-id="message.id"
      :source-text="message.content"
      :actions-ready="actionsStatus === 'ready'"
      @applied="onChapterApplied"
      @cancel="showWriting = false"
    />
    </div>
    <div v-if="showSettings && hasActionSource" :inert="actionsStatus !== 'ready'">
    <ApplySuggestionCard
      :novel-id="novelId!"
      :text="message.content"
      :conversation-id="message.conversation_id"
      :message-id="message.id"
      settings-only
      @applied="onSettingsApplied"
      @cancel="showSettings = false"
    />
    </div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  flex-direction: column;
  margin-bottom: 12px;
}
.bubble-row.user {
  align-items: flex-end;
}
.bubble-row.assistant {
  align-items: flex-start;
}
.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble.user {
  background: #2563eb;
  color: #fff;
}
.bubble.assistant {
  background: #f3f4f6;
  color: #1f2937;
}
.content {
  white-space: normal;
}
.content p {
  margin: 0 0 0.75em;
}
.content p:last-child,
.content ul:last-child {
  margin-bottom: 0;
}
.content ul {
  margin: 0 0 0.75em;
  padding-left: 1.4em;
}
.strong {
  font-weight: 700;
}
.outline-card {
  width: min(680px, 90vw);
  padding: 12px;
  background: #fafafa;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  margin-top: 8px;
}
.footer {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.generation-rule-result { width: min(680px, 90vw); box-sizing: border-box; padding: 10px 12px; background: #fffbe6; border: 1px solid #f0dfa3; border-radius: 8px; }
.generation-rule-result p { margin: 0 0 6px; }
.generation-rule-result ul { margin: 0; padding-left: 1.4em; }
</style>
