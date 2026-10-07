<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NCheckbox, NEmpty, NInput, NSpin, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { IdeaChatResult } from '../types'

const props = defineProps<{
  novelId: string
  text: string
  conversationId?: string
  messageId?: string
  settingsOnly?: boolean
}>()
const emit = defineEmits<{
  (e: 'applied', summary: { type: string; action: string; id: string }[]): void
  (e: 'cancel'): void
}>()

const message = useMessage()
const loading = ref(true)
const applying = ref(false)
const result = ref<IdeaChatResult | null>(null)
const dataTexts = ref<string[]>([])
const selected = ref<boolean[]>([])
const completed = ref<boolean[]>([])
const errors = ref<Record<number, string>>({})
const appliedSummary = ref<{ type: string; action: string; id: string }[]>([])
type SuggestionSource = {
  novelId: string
  text: string
  conversationId?: string
  messageId?: string
  settingsOnly?: boolean
}
const sourceKey = () => JSON.stringify([props.novelId, props.text, props.conversationId ?? '', props.messageId ?? '', !!props.settingsOnly])
let generation = 0
let disposed = false
let parsedSource: { key: string; source: SuggestionSource } | null = null

function reset() {
  result.value = null
  dataTexts.value = []
  selected.value = []
  completed.value = []
  errors.value = {}
  appliedSummary.value = []
  parsedSource = null
  applying.value = false
}

const TYPE_LABEL: Record<string, string> = {
  character: '人物',
  location: '地点',
  world_setting: '世界观',
  timeline_event: '时间线',
  foreshadowing: '伏笔',
  relationship: '关系',
}

async function load() {
  const sequence = ++generation
  const key = sourceKey()
  const source: SuggestionSource = { novelId: props.novelId, text: props.text,
    conversationId: props.conversationId, messageId: props.messageId, settingsOnly: props.settingsOnly }
  const isCurrent = () => !disposed && sequence === generation && key === sourceKey()
  loading.value = true
  try {
    const r = source.settingsOnly && source.conversationId && source.messageId
      ? await api.previewMessageSettings(source.conversationId, source.messageId)
      : await api.ideaChat(source.novelId, source.text)
    if (!isCurrent()) return
    parsedSource = { key, source }
    result.value = r
    dataTexts.value = r.changes.map((c) => JSON.stringify(c.data, null, 2))
    selected.value = r.changes.map(() => true)
    completed.value = r.changes.map(() => false)
    errors.value = Object.fromEntries((r.failed ?? []).map((item) => [item.index, item.error]))
    appliedSummary.value = []
  } catch (e) {
    if (isCurrent()) {
      message.error((e as Error).message)
      emit('cancel')
    }
  } finally {
    if (isCurrent()) loading.value = false
  }
}

async function apply() {
  if (!result.value || applying.value || loading.value || disposed || parsedSource?.key !== sourceKey()) return
  const { key, source } = parsedSource
  const sequence = generation
  const isCurrent = () => !disposed && sequence === generation && key === sourceKey()
  const suggestions: Record<string, unknown>[] = []
  const sourceIndices: number[] = []
  for (let i = 0; i < result.value.changes.length; i++) {
    if (!selected.value[i] || completed.value[i]) continue
    const c = result.value.changes[i]
    try {
      const data = JSON.parse(dataTexts.value[i])
      if (['character', 'world_setting', 'location'].includes(c.type)
        && (typeof data.name !== 'string' || !data.name.trim())) {
        message.error(`第 ${i + 1} 条缺少名称，请填写后再应用`)
        return
      }
      if (c.type === 'foreshadowing'
        && (typeof data.content !== 'string' || !data.content.trim())) {
        message.error(`第 ${i + 1} 条缺少伏笔内容，请填写后再应用`)
        return
      }
      if (c.type === 'foreshadowing' && data.status !== undefined
        && !['planted', 'suspended', 'consumed'].includes(data.status)) {
        message.error(`第 ${i + 1} 条伏笔状态只能是 planted、suspended 或 consumed`)
        return
      }
      suggestions.push({ type: c.type, action: c.action, data })
      sourceIndices.push(i)
    } catch {
      message.error('变更数据 JSON 格式错误')
      return
    }
  }
  if (!suggestions.length) {
    if (appliedSummary.value.length) {
      emit('applied', appliedSummary.value)
      return
    }
    message.error('无可应用的变更')
    return
  }
  applying.value = true
  try {
    const r = source.settingsOnly && source.conversationId && source.messageId
      ? await api.applyMessageSettings(source.conversationId, source.messageId, suggestions)
      : await api.ideaApply(source.novelId, suggestions, source.text, 'chat')
    if (!isCurrent()) return
    errors.value = {}
    const failed = r.failed ?? []
    for (const item of failed) {
      const index = sourceIndices[item.index]
      if (index !== undefined) errors.value[index] = item.error
    }
    const failedIndices = new Set(failed.map((item) => item.index))
    const succeeded = r.applied.map((item, index) => item.index ??
      sourceIndices.map((_, i) => i).filter((i) => !failedIndices.has(i))[index])
    for (const index of succeeded) {
      const source = sourceIndices[index]
      if (source !== undefined) {
        completed.value[source] = true
        selected.value[source] = false
      }
    }
    appliedSummary.value.push(...r.applied.map(({ type, action, id }) => ({ type, action, id })))
    for (const warning of r.warnings ?? []) message.warning(warning)
    if (failed.length) {
      message.warning(`本次已应用 ${r.applied.length} 条，${failed.length} 条未应用；请修正标出的数据后重试`)
      return
    }
    if (!r.applied.length) {
      message.warning('本次未应用任何变更，请核对建议后重试')
      return
    }
    message.success(`已应用 ${appliedSummary.value.length} 条变更`)
    emit('applied', appliedSummary.value)
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  } finally {
    if (isCurrent()) applying.value = false
  }
}

function cancelOrFinish() {
  if (applying.value || disposed || parsedSource?.key !== sourceKey()) return
  if (appliedSummary.value.length) {
    const remaining = completed.value.filter((item) => !item).length
    if (remaining) message.warning(`${remaining} 条建议未应用，已保留实际保存的 ${appliedSummary.value.length} 条变更`)
    emit('applied', appliedSummary.value)
  } else emit('cancel')
}

watch(sourceKey, () => {
  if (disposed) return
  reset()
  void load()
}, { flush: 'sync' })
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  generation += 1
})
</script>

<template>
  <div class="apply-card">
    <n-spin :show="loading">
      <template v-if="result">
        <p v-if="settingsOnly">仅提炼人物、世界观、伏笔；确认时可能调整未生成章节的大纲。</p>
        <div class="reason">
          <n-tag type="info" size="small">解析结果</n-tag>
          <span v-if="result.reason">原因：{{ result.reason }}</span>
        </div>

        <div class="targets">
          <div v-for="(m, i) in result.matched" :key="i" class="target">
            <n-tag :type="m.matched ? 'success' : 'warning'" size="small">{{ m.name }}</n-tag>
            <span v-if="m.matched">{{ m.type }}（已匹配）</span>
            <span v-else-if="m.ambiguous">同名目标，确认时按变更类型核对</span>
            <span v-else>未匹配（将新建）</span>
          </div>
        </div>

        <div v-for="(c, i) in result.changes" :key="i" class="change-card">
          <div class="change-head">
            <n-checkbox v-model:checked="selected[i]" :disabled="completed[i] || applying">应用此条</n-checkbox>
            <n-tag v-if="completed[i]" type="success" size="small">已应用</n-tag>
            <n-tag size="small">{{ TYPE_LABEL[c.type] ?? c.type }}</n-tag>
            <n-tag :type="c.action === 'update' ? 'info' : 'success'" size="small">
              {{ c.action === 'update' ? '修改' : '新增' }}
            </n-tag>
          </div>
          <n-input
            v-if="selected[i]"
            v-model:value="dataTexts[i]"
            :disabled="applying"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
          />
          <p v-if="errors[i]" class="change-error">{{ errors[i] }}</p>
        </div>

        <div class="actions">
          <n-button size="small" :disabled="applying" @click="cancelOrFinish">{{ appliedSummary.length ? '完成已应用项' : '取消' }}</n-button>
          <n-button size="small" type="primary" :loading="applying" @click="apply">确认应用</n-button>
        </div>
      </template>
      <n-empty v-else-if="!loading" description="未解析出可应用的变更" />
    </n-spin>
  </div>
</template>

<style scoped>
.apply-card {
  margin-top: 8px;
  border: 1px solid #e5e5e5;
  border-radius: 6px;
  padding: 12px;
  background: #fafafa;
}
.reason {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.targets {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.target {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #666;
  font-size: 12px;
}
.change-card {
  margin-bottom: 10px;
}
.change-error {
  color: #b42318;
  white-space: pre-wrap;
}
.change-head {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
