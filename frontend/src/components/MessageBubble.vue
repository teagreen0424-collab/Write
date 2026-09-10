<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { ChatMessage } from '../types'
import ApplySuggestionCard from './ApplySuggestionCard.vue'

const props = defineProps<{ message: ChatMessage; novelId: string | null }>()

const toast = useMessage()
const showApply = ref(false)
const appliedInfo = ref<Record<string, unknown> | null>(
  props.message.meta && (props.message.meta as any).applied ? props.message.meta : null,
)

const isAssistant = computed(() => props.message.role === 'assistant')
const canApply = computed(() => isAssistant.value && !!props.novelId)

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
</script>

<template>
  <div class="bubble-row" :class="message.role">
    <div class="bubble" :class="message.role">
      <div class="content">{{ message.content }}</div>
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
      </div>
    </div>
    <ApplySuggestionCard
      v-if="showApply && canApply"
      :novel-id="novelId!"
      :text="message.content"
      @applied="onApplied"
      @cancel="showApply = false"
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
  white-space: pre-wrap;
}
.footer {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
</style>
