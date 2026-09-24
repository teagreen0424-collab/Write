<script setup lang="ts">
import { onMounted, ref } from 'vue'
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

const TYPE_LABEL: Record<string, string> = {
  character: '人物',
  location: '地点',
  world_setting: '世界观',
  timeline_event: '时间线',
  foreshadowing: '伏笔',
  relationship: '关系',
}

async function load() {
  loading.value = true
  try {
    const r = props.settingsOnly && props.conversationId && props.messageId
      ? await api.previewMessageSettings(props.conversationId, props.messageId)
      : await api.ideaChat(props.novelId, props.text)
    result.value = r
    dataTexts.value = r.changes.map((c) => JSON.stringify(c.data, null, 2))
    selected.value = r.changes.map(() => true)
  } catch (e) {
    message.error((e as Error).message)
    emit('cancel')
  } finally {
    loading.value = false
  }
}

async function apply() {
  if (!result.value) return
  const suggestions: Record<string, unknown>[] = []
  for (let i = 0; i < result.value.changes.length; i++) {
    if (!selected.value[i]) continue
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
    } catch {
      message.error('变更数据 JSON 格式错误')
      return
    }
  }
  if (!suggestions.length) {
    message.error('无可应用的变更')
    return
  }
  applying.value = true
  try {
    const r = props.settingsOnly && props.conversationId && props.messageId
      ? await api.applyMessageSettings(props.conversationId, props.messageId, suggestions)
      : await api.ideaApply(props.novelId, suggestions, props.text, 'chat')
    message.success(`已应用 ${r.applied.length} 条变更`)
    emit('applied', r.applied)
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    applying.value = false
  }
}

onMounted(load)
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
            <span v-else>未匹配（将新建）</span>
          </div>
        </div>

        <div v-for="(c, i) in result.changes" :key="i" class="change-card">
          <div class="change-head">
            <n-checkbox v-model:checked="selected[i]">应用此条</n-checkbox>
            <n-tag size="small">{{ TYPE_LABEL[c.type] ?? c.type }}</n-tag>
            <n-tag :type="c.action === 'update' ? 'info' : 'success'" size="small">
              {{ c.action === 'update' ? '修改' : '新增' }}
            </n-tag>
          </div>
          <n-input
            v-if="selected[i]"
            v-model:value="dataTexts[i]"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
          />
        </div>

        <div class="actions">
          <n-button size="small" @click="emit('cancel')">取消</n-button>
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
