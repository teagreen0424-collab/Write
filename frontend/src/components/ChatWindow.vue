<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { NButton, NEmpty, NInput, useMessage } from 'naive-ui'
import { api } from '../api'
import type { ChatMessage, Conversation, Novel } from '../types'
import MessageBubble from './MessageBubble.vue'

const props = defineProps<{ conversation: Conversation | null; novels: Novel[] }>()
const emit = defineEmits<{ (e: 'changed'): void; (e: 'promote'): void; (e: 'move'): void }>()

const toast = useMessage()
const messages = ref<ChatMessage[]>([])
const input = ref('')
const streaming = ref(false)
const streamingText = ref('')
const listEl = ref<HTMLElement | null>(null)

const isGlobal = computed(() => !!props.conversation && !props.conversation.novel_id)
const novelLabel = computed(() => {
  if (!props.conversation?.novel_id) return '全局'
  return props.novels.find((n) => n.id === props.conversation!.novel_id)?.title ?? '作品'
})

async function loadMessages() {
  if (!props.conversation) {
    messages.value = []
    return
  }
  try {
    messages.value = await api.listMessages(props.conversation.id)
    scrollToBottom()
  } catch (e) {
    toast.error((e as Error).message)
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value || !props.conversation) return
  input.value = ''
  streaming.value = true
  streamingText.value = ''
  messages.value.push({
    id: 'local-' + Date.now(),
    conversation_id: props.conversation.id,
    role: 'user',
    content: text,
    meta: null,
    created_at: '',
  })
  scrollToBottom()
  try {
    await api.sendMessage(props.conversation.id, text, {
      onToken: (t) => {
        streamingText.value += t
        scrollToBottom()
      },
      onDone: () => {
        streaming.value = false
        streamingText.value = ''
        loadMessages()
        emit('changed')
      },
      onError: (err) => {
        streaming.value = false
        streamingText.value = ''
        toast.error(err)
        loadMessages()
      },
    })
  } catch (e) {
    streaming.value = false
    streamingText.value = ''
    toast.error((e as Error).message)
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

watch(
  () => props.conversation?.id,
  () => loadMessages(),
  { immediate: true },
)
</script>

<template>
  <div class="chat-window">
    <template v-if="conversation">
      <header class="header">
        <div class="title-row">
          <span class="title">{{ conversation.title || '新对话' }}</span>
          <span class="badge">{{ novelLabel }}</span>
        </div>
        <div v-if="isGlobal" class="header-actions">
          <n-button size="small" quaternary @click="emit('promote')">转为作品</n-button>
          <n-button size="small" quaternary @click="emit('move')">移动到作品</n-button>
        </div>
      </header>

      <div ref="listEl" class="message-list">
        <MessageBubble
          v-for="m in messages"
          :key="m.id"
          :message="m"
          :novel-id="conversation.novel_id"
        />
        <div v-if="streaming" class="streaming-assistant">
          <div class="streaming-bubble">{{ streamingText || '正在思考…' }}</div>
        </div>
      </div>

      <div class="input-bar">
        <n-input
          v-model:value="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="输入构思、问题或想法，Enter 发送"
          :disabled="streaming"
          @keydown.enter.exact.prevent="send"
        />
        <n-button type="primary" :disabled="streaming || !input.trim()" @click="send">
          发送
        </n-button>
      </div>
    </template>

    <n-empty v-else description="选择或新建一个会话开始创作讨论" class="empty" />
  </div>
</template>

<style scoped>
.chat-window {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid #e5e5e5;
}
.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.title {
  font-weight: 600;
  font-size: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge {
  font-size: 12px;
  color: #888;
  border: 1px solid #e5e5e5;
  border-radius: 10px;
  padding: 1px 8px;
  flex-shrink: 0;
}
.header-actions {
  display: flex;
  gap: 4px;
}
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.streaming-assistant {
  display: flex;
  justify-content: flex-start;
}
.streaming-bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  background: #f3f4f6;
  color: #1f2937;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.input-bar {
  display: flex;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid #e5e5e5;
  align-items: flex-end;
}
.empty {
  margin: auto;
}
</style>
