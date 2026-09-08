<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NEmpty, NInput, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { IdeaChatResult } from '../types'

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'applied'): void }>()

const message = useMessage()

const input = ref('')
const loading = ref(false)
const result = ref<IdeaChatResult | null>(null)
const dataTexts = ref<string[]>([])

const TYPE_LABEL: Record<string, string> = {
  character: '人物',
  location: '地点',
  world_setting: '世界观',
  timeline_event: '时间线',
  foreshadowing: '伏笔',
  relationship: '关系',
}

async function send() {
  const text = input.value.trim()
  if (!text || loading.value) return
  loading.value = true
  try {
    const r = await api.ideaChat(props.novelId, text)
    result.value = r
    dataTexts.value = r.changes.map((c) => JSON.stringify(c.data, null, 2))
    input.value = ''
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function apply() {
  if (!result.value) return
  const suggestions: Record<string, unknown>[] = []
  for (let i = 0; i < result.value.changes.length; i++) {
    const c = result.value.changes[i]
    try {
      suggestions.push({ type: c.type, action: c.action, data: JSON.parse(dataTexts.value[i]) })
    } catch {
      message.error('变更数据 JSON 格式错误')
      return
    }
  }
  if (!suggestions.length) {
    message.error('无可应用的变更')
    return
  }
  try {
    const r = await api.ideaApply(props.novelId, suggestions)
    message.success(`已应用 ${r.applied.length} 条变更`)
    result.value = null
    emit('applied')
  } catch (e) {
    message.error((e as Error).message)
  }
}
</script>

<template>
  <div class="idea-panel">
    <div class="chat-input">
      <n-input
        v-model:value="input"
        placeholder="输入构思，如：让林越黑化，因为他知道了身世真相"
        @keyup.enter="send"
      />
      <n-button type="primary" :loading="loading" @click="send">发送</n-button>
    </div>

    <div v-if="result" class="suggestion">
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
          <n-tag size="small">{{ TYPE_LABEL[c.type] ?? c.type }}</n-tag>
          <n-tag :type="c.action === 'update' ? 'info' : 'success'" size="small">
            {{ c.action === 'update' ? '修改' : '新增' }}
          </n-tag>
        </div>
        <n-input
          v-model:value="dataTexts[i]"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
        />
      </div>

      <div class="actions">
        <n-button @click="result = null">取消</n-button>
        <n-button type="primary" @click="apply">确认应用</n-button>
      </div>
    </div>
    <n-empty v-else description="输入构思开始对话式共创" />
  </div>
</template>

<style scoped>
.idea-panel {
  padding: 16px;
  height: 100%;
  overflow: auto;
  max-width: 760px;
  margin: 0 auto;
}
.chat-input {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.suggestion {
  border: 1px solid #e5e5e5;
  border-radius: 6px;
  padding: 16px;
}
.reason {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.targets {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.target {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #666;
}
.change-card {
  margin-bottom: 12px;
}
.change-head {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 8px;
}
</style>
