<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { NButton, NEmpty, NInput, useMessage } from 'naive-ui'
import { api } from '../api'
import type { ChatMessage, Conversation, Novel } from '../types'
import MessageBubble from './MessageBubble.vue'

const props = defineProps<{ conversation: Conversation | null; novels: Novel[]; conversationIds?: string[] }>()
const emit = defineEmits<{
  (e: 'changed'): void; (e: 'promote'): void; (e: 'move'): void
  (e: 'open-outline', novelId: string): void
  (e: 'open-writing', novelId: string, chapterId: string): void
  (e: 'dirty-change', dirty: boolean): void
}>()

const toast = useMessage()
interface ChatState {
  messages: ChatMessage[]
  input: string
  streaming: boolean
  streamingText: string
  replyNotice: string
  loadId: number
}
const states = reactive(new Map<string, ChatState>())
let alive = true
const conversationScope = () => props.conversation
  ? JSON.stringify([props.conversation.id, props.conversation.novel_id]) : ''
function stateFor(scope: string): ChatState {
  if (!states.has(scope)) states.set(scope, reactive({ messages: [], input: '', streaming: false, streamingText: '', replyNotice: '', loadId: 0 }))
  return states.get(scope)!
}
const currentState = computed(() => conversationScope() ? stateFor(conversationScope()) : null)
const messages = computed(() => currentState.value?.messages ?? [])
const input = computed({ get: () => currentState.value?.input ?? '', set: (text: string) => {
  if (currentState.value) currentState.value.input = text
} })
const streaming = computed(() => !!props.conversation && [...states.entries()].some(([scope, state]) =>
  JSON.parse(scope)[0] === props.conversation!.id && state.streaming,
))
const streamingText = computed(() => currentState.value?.streamingText ?? '')
const replyNotice = computed(() => currentState.value?.replyNotice ?? '')
const dirty = computed(() => [...states.values()].some((state) => !!state.input.trim()))
const listEl = ref<HTMLElement | null>(null)

const isGlobal = computed(() => !!props.conversation && !props.conversation.novel_id)
const novelLabel = computed(() => {
  if (!props.conversation?.novel_id) return '全局'
  return props.novels.find((n) => n.id === props.conversation!.novel_id)?.title ?? '作品'
})

async function loadMessages(conversationId = props.conversation?.id, scope = conversationScope()) {
  if (!conversationId || !scope || !alive) return false
  const state = stateFor(scope)
  const requestId = ++state.loadId
  try {
    const loaded = await api.listMessages(conversationId)
    if (!alive || requestId !== state.loadId) return false
    state.messages = loaded.filter((item) => item.conversation_id === conversationId)
    if (scope === conversationScope()) scrollToBottom()
    return true
  } catch (e) {
    if (alive && requestId === state.loadId && scope === conversationScope()) toast.error((e as Error).message)
    return false
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value || !props.conversation) return
  const conversationId = props.conversation.id
  const scope = conversationScope()
  const state = stateFor(scope)
  state.loadId += 1
  state.input = ''
  state.streaming = true
  state.streamingText = ''
  state.replyNotice = ''
  state.messages.push({
    id: 'local-' + Date.now(),
    conversation_id: conversationId,
    role: 'user',
    content: text,
    meta: null,
    created_at: '',
  })
  scrollToBottom()
  try {
    await api.sendMessage(conversationId, text, {
      onToken: (t) => {
        if (!alive || states.get(scope) !== state) return
        state.streamingText += t
        if (scope === conversationScope()) scrollToBottom()
      },
      onDone: async () => {
        state.streaming = false
        if (!alive || states.get(scope) !== state) return
        const loaded = await loadMessages(conversationId, scope)
        if (!alive || states.get(scope) !== state) return
        if (loaded) state.streamingText = ''
        else state.replyNotice = '回复已保存，但历史消息读取未完成，以下片段可复制保留：'
        if (props.conversation?.id === conversationId && scope !== conversationScope()) await loadMessages()
        if (!alive) return
        emit('changed')
      },
      onError: async (err) => {
        state.streaming = false
        state.replyNotice = '回复未完整结束，以下片段可复制保留：'
        if (!alive) return
        if (scope === conversationScope()) toast.error(err)
        await loadMessages(conversationId, scope)
      },
    })
  } catch (e) {
    state.streaming = false
    state.replyNotice = '回复未完整结束，以下片段可复制保留：'
    if (alive && scope === conversationScope()) toast.error((e as Error).message)
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (alive && listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

watch(
  conversationScope,
  (scope, previousScope) => {
    if (scope && previousScope && JSON.parse(scope)[0] === JSON.parse(previousScope)[0]) {
      const previous = states.get(previousScope)
      const next = stateFor(scope)
      if (previous?.input) {
        next.input = next.input && next.input !== previous.input ? previous.input + '\n' + next.input : previous.input
        previous.input = ''
      }
    }
    if (!currentState.value?.streaming) void loadMessages()
  },
  { immediate: true, flush: 'sync' },
)
watch(dirty, (value) => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(() => props.conversationIds, (ids) => {
  if (!ids) return
  const existing = new Set(ids)
  for (const scope of states.keys()) {
    if (!existing.has(JSON.parse(scope)[0])) states.delete(scope)
  }
}, { flush: 'sync' })
function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => {
  alive = false
  for (const state of states.values()) state.loadId += 1
  window.removeEventListener('beforeunload', beforeUnload)
  emit('dirty-change', false)
})
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
          :key="conversationScope() + ':' + m.id"
          :message="m"
          :novel-id="conversation.novel_id"
          @open-outline="emit('open-outline', $event)"
          @open-writing="(novelId: string, chapterId: string) => emit('open-writing', novelId, chapterId)"
        />
        <div v-if="streaming" class="streaming-assistant">
          <div class="streaming-bubble">{{ streamingText || '正在思考…' }}</div>
        </div>
      </div>

      <p v-if="!streaming && streamingText" class="incomplete-reply">{{ replyNotice }}{{ streamingText }}</p>

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
.incomplete-reply { margin: 0; padding: 8px 16px; color: #92400e; white-space: pre-wrap; max-height: 180px; overflow: auto; }
.empty {
  margin: auto;
}
</style>
