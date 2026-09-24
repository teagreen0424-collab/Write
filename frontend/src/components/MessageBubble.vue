<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { Chapter, ChatMessage, OutlineApplyResult } from '../types'
import ApplySuggestionCard from './ApplySuggestionCard.vue'
import OutlinePlanCard from './OutlinePlanCard.vue'
import ChapterWriteCard from './ChapterWriteCard.vue'

const props = defineProps<{ message: ChatMessage; novelId: string | null }>()
const emit = defineEmits<{
  (e: 'open-outline', novelId: string): void
  (e: 'open-writing', novelId: string, chapterId: string): void
}>()

const toast = useMessage()
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
const canApply = computed(
  () => isAssistant.value && !!props.novelId && !props.message.id.startsWith('local-'),
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
  try {
    await api.updateMessage(props.message.conversation_id, props.message.id, meta)
    appliedInfo.value = meta
    showApply.value = false
    toast.success('已应用到作品')
  } catch (e) {
    toast.error((e as Error).message)
  }
}

watch(
  () => [props.novelId, props.message.id],
  async () => {
    outlineResult.value = null
    chapterResult.value = null
    settingsApplied.value = false
    outlineDeleted.value = false
    chapterDeleted.value = false
    if (!props.novelId || !isAssistant.value || props.message.id.startsWith('local-')) return
    try {
      const actions = await api.listMessageActions(props.message.conversation_id, props.message.id)
      const outline = actions.find((action) => action.action_type === 'outline')
      if (outline?.result_summary?.volume_id) {
        outlineResult.value = outline.result_summary as unknown as OutlineApplyResult
        outlineDeleted.value = outline.target_deleted
      }
      const chapter = actions.find((action) => action.action_type === 'chapter')
      if (chapter?.result_summary?.chapter_id) {
        chapterResult.value = chapter.result_summary as unknown as { chapter_id: string; title: string }
        chapterDeleted.value = chapter.target_deleted
      }
      settingsApplied.value = actions.some((action) => action.action_type === 'settings')
    } catch {
      // 动作标签读取失败不影响消息展示。
    }
  },
  { immediate: true },
)

function onOutlineApplied(result: OutlineApplyResult) {
  outlineResult.value = result
  showOutline.value = false
}

function onChapterApplied(chapter: Chapter) {
  chapterResult.value = { chapter_id: chapter.id, title: chapter.title }
  showWriting.value = false
}

function onSettingsApplied() {
  settingsApplied.value = true
  showSettings.value = false
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
        <n-tag v-if="settingsApplied" type="success" size="small">设定已提炼</n-tag>
        <n-button
          v-else size="tiny" quaternary :disabled="!canApply"
          :title="canApply ? '提炼人物、世界观与伏笔草稿' : '先转为作品'"
          @click="showSettings = !showSettings"
        >提炼设定</n-button>
      </div>
    </div>
    <ApplySuggestionCard
      v-if="showApply && canApply"
      :novel-id="novelId!"
      :text="message.content"
      @applied="onApplied"
      @cancel="showApply = false"
    />
    <div v-if="showOutline && canApply" class="outline-card">
      <OutlinePlanCard
        :novel-id="novelId!"
        :conversation-id="message.conversation_id"
        :message-id="message.id"
        @applied="onOutlineApplied"
        @cancel="showOutline = false"
      />
    </div>
    <ChapterWriteCard
      v-if="showWriting && canApply"
      :novel-id="novelId!"
      :conversation-id="message.conversation_id"
      :message-id="message.id"
      :source-text="message.content"
      @applied="onChapterApplied"
      @cancel="showWriting = false"
    />
    <ApplySuggestionCard
      v-if="showSettings && canApply"
      :novel-id="novelId!"
      :text="message.content"
      :conversation-id="message.conversation_id"
      :message-id="message.id"
      settings-only
      @applied="onSettingsApplied"
      @cancel="showSettings = false"
    />
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
</style>
