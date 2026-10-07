<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NEmpty, NInput, NModal, useMessage } from 'naive-ui'
import { api } from '../api'
import type { Conversation, Novel } from '../types'
import { chatSubmissionStates as states, chatSubmissionState as stateFor, newChatSubmission, hashChatSubmission, isChatSubmissionReceipt, isChatReplyDone } from '../utils/chatSubmission'
import type { ChatSubmission, ChatSubmissionState } from '../utils/chatSubmission'
import MessageBubble from './MessageBubble.vue'

const props = defineProps<{ conversation: Conversation | null; novels: Novel[]; conversationIds?: string[] }>()
const emit = defineEmits<{
  (e: 'changed'): void; (e: 'promote'): void; (e: 'move'): void
  (e: 'open-outline', novelId: string): void
  (e: 'open-writing', novelId: string, chapterId: string): void
  (e: 'dirty-change', dirty: boolean): void
}>()

const toast = useMessage()
let alive = true
let pageEpoch = 0
const conversationScope = () => props.conversation
  ? JSON.stringify([props.conversation.id, props.conversation.novel_id]) : ''
const currentState = computed(() => conversationScope() && (!props.conversationIds || props.conversationIds.includes(props.conversation!.id))
  ? stateFor(conversationScope()) : null)
const messages = computed(() => currentState.value?.messages ?? [])
const input = computed({ get: () => currentState.value?.input ?? '', set: (text: string) => {
  if (currentState.value && currentState.value.input !== text) { currentState.value.input = text; currentState.value.revision += 1 }
} })
const streaming = computed(() => !!props.conversation && [...states.entries()].some(([scope, state]) =>
  JSON.parse(scope)[0] === props.conversation!.id && state.streaming,
))
const streamingText = computed(() => currentState.value?.streamingText ?? '')
const replyNotice = computed(() => currentState.value?.replyNotice ?? '')
const submissionEntry = computed(() => {
  const scope = conversationScope(), id = props.conversation?.id
  const entries = [...states.entries()].filter(([key, state]) => JSON.parse(key)[0] === id && state.intent)
  return entries.find(([, state]) => state.intent?.status !== 'saved') ?? entries.find(([key]) => key === scope) ?? null
})
const submission = computed(() => submissionEntry.value?.[1].intent ?? null)
const submissionUnresolved = computed(() => !!submission.value && submission.value.status !== 'saved')
const dirty = computed(() => [...states.values()].some((state) => state.input.length > 0 || state.streaming
  || (!!state.intent && state.intent.status !== 'saved') || (!!state.streamingText && !!state.replyNotice)))
const showRestore = ref(false)
const restoreContext = ref<{ scope: string; epoch: number; revision: number; intent: ChatSubmission; discard: boolean } | null>(null)
const listEl = ref<HTMLElement | null>(null)

const isGlobal = computed(() => !!props.conversation && !props.conversation.novel_id)
const novelLabel = computed(() => {
  if (!props.conversation?.novel_id) return '全局'
  return props.novels.find((n) => n.id === props.conversation!.novel_id)?.title ?? '作品'
})

async function loadMessages(conversationId = props.conversation?.id, scope = conversationScope()) {
  if (!conversationId || !scope || !alive || (props.conversationIds && !props.conversationIds.includes(conversationId))) return false
  const state = stateFor(scope)
  const requestId = ++state.loadId
  try {
    const loaded = await api.listMessages(conversationId)
    if (!alive || states.get(scope) !== state || requestId !== state.loadId) return false
    state.messages = loaded.filter((item) => item.conversation_id === conversationId)
    if (scope === conversationScope()) scrollToBottom()
    return true
  } catch (e) {
    if (alive && states.get(scope) === state && requestId === state.loadId && scope === conversationScope()) toast.error((e as Error).message)
    return false
  }
}

async function send() {
  const text = input.value
  if (!text.trim() || streaming.value || submissionUnresolved.value || !props.conversation) return
  const conversationId = props.conversation.id
  const scope = conversationScope()
  const state = stateFor(scope)
  let intent: ChatSubmission
  try { intent = newChatSubmission(conversationId, text) }
  catch { toast.error('无法建立本次发送身份，请先复制原稿；本次未发送'); return }
  state.intent = intent
  intent = state.intent!
  state.input = ''
  state.revision += 1
  await submit(scope, state, intent)
}

async function submit(scope: string, state: ChatSubmissionState, intent: ChatSubmission) {
  if (states.get(scope) !== state || state.intent !== intent) return
  const epoch = pageEpoch
  const operation = ++intent.token
  const sameIntent = () => states.get(scope) === state && state.intent === intent && intent.token === operation
  const saved = () => intent.status === 'saved'
  const current = () => alive && epoch === pageEpoch && conversationScope() === scope && sameIntent()
  const conversationId = intent.conversationId
  state.loadId += 1
  state.streaming = true
  intent.status = 'pending'
  intent.busy = true
  state.streamingText = ''
  state.replyNotice = ''
  try {
    if (!intent.hash) intent.hash = await hashChatSubmission(intent.raw)
    if (!sameIntent()) return
    if (!alive) { intent.status = 'unknown'; return }
    if (!state.messages.some(row => row.id === 'local-' + intent.requestId || row.id === intent.requestId)) state.messages.push({
      id: 'local-' + intent.requestId, conversation_id: conversationId, role: 'user', content: intent.raw, meta: null, created_at: '',
    })
    if (current()) scrollToBottom()
    const accept = (value: unknown, kind: 'user_saved' | 'receipt') => {
      if (!sameIntent()) return false
      const row = value as Record<string, unknown> | null
      if (!isChatSubmissionReceipt(value, intent) || row?.type !== kind
        || (kind === 'user_saved' ? row.receipt_replayed !== false : row.receipt_replayed !== true || row.reply_restarted !== false)) {
        if (intent.status !== 'saved') intent.status = 'unknown'
        state.replyNotice = '保存回执未得到完整核对，原稿仍保留，请核对原发送。'
        return false
      }
      intent.status = 'saved'
      return true
    }
    await api.sendMessage(conversationId, intent.raw, {
      onToken: (t) => {
        if (!alive || !sameIntent()) return
        state.streamingText += t
        if (current()) scrollToBottom()
      },
      onUserSaved: value => { accept(value, 'user_saved') },
      onReceipt: async value => {
        if (!sameIntent()) return
        state.streaming = false
        const accepted = accept(value, 'receipt')
        if (accepted) state.replyNotice = '原用户消息已保存；本次核对没有重新生成回复，请核对历史回复。'
        if (!alive) return
        if (current() && accepted) toast.success('原用户消息已保存，本次未重新生成回复')
        await loadMessages(conversationId, scope)
      },
      onDone: async value => {
        if (!sameIntent()) return
        state.streaming = false
        if (!isChatReplyDone(value, intent)) {
          if (intent.status !== 'saved') intent.status = 'unknown'
          state.replyNotice = '回复完成回应未得到完整核对，原稿和片段仍保留，请核对原发送与历史消息。'
          if (alive) await loadMessages(conversationId, scope)
          return
        }
        // 完整 done 证明同次用户与回复已落库；不依靠历史文本匹配。
        intent.status = 'saved'
        if (!alive) return
        const loaded = await loadMessages(conversationId, scope)
        if (!alive || !sameIntent()) return
        if (loaded) state.streamingText = ''
        else state.replyNotice = '回复已保存，但历史消息读取未完成，以下片段可复制保留：'
        if (props.conversation?.id === conversationId && scope !== conversationScope()) await loadMessages()
        if (!alive || !sameIntent()) return
        emit('changed')
      },
      onError: async (err, code) => {
        if (!sameIntent()) return
        state.streaming = false
        if (intent.status !== 'saved') intent.status = code === 'chat_message_request_conflict' || code === 'chat_conversation_not_found' ? 'blocked' : 'unknown'
        state.replyNotice = '回复未完整结束，以下片段可复制保留：'
        if (!alive) return
        if (current()) toast.error(err)
        await loadMessages(conversationId, scope)
      },
    }, intent.requestId)
  } catch (e) {
    if (sameIntent()) {
      if (!saved()) intent.status = 'unknown'
      state.replyNotice = '发送未完整结束，原稿仍保留，请核对原发送。'
      if (current()) toast.error((e as Error).message)
    }
  } finally {
    if (sameIntent()) { state.streaming = false; intent.busy = false }
  }
}

async function checkSubmission() {
  const entry = submissionEntry.value, scope = entry?.[0], state = entry?.[1], intent = state?.intent, epoch = pageEpoch, visibleScope = conversationScope()
  if (!scope || !state || !intent || intent.busy || streaming.value || intent.status === 'saved') return
  const operation = ++intent.token
  const same = () => states.get(scope) === state && state.intent === intent && intent.token === operation
  const current = () => alive && epoch === pageEpoch && conversationScope() === visibleScope && same()
  intent.busy = true
  try {
    const receipt = await api.getChatSubmission(intent.conversationId, intent.requestId)
    if (!same()) return
    if (!isChatSubmissionReceipt(receipt, intent) || receipt.receipt_replayed !== true) {
      intent.status = 'unknown'
      if (current()) toast.warning('保存回执未得到完整核对，原稿仍保留'); return
    }
    intent.status = 'saved'
    if (current()) toast.success('原用户消息已保存；不能据此确认回复已完整生成')
    if (alive) await loadMessages(intent.conversationId, scope)
  } catch (e) {
    if (!same()) return
    const error = e as Error & { status?: number; code?: string }
    if (error.status === 404 && error.code === 'chat_message_request_not_found') {
      intent.status = 'not_found'
      if (current()) toast.warning('本次核对未找到原发送，可明确使用原稿重试；不会自动发送')
    } else {
      intent.status = error.code === 'chat_message_request_conflict' || error.code === 'chat_conversation_not_found' ? 'blocked' : 'unknown'
      if (current()) toast.error(error.message)
    }
  } finally { if (same()) intent.busy = false }
}

async function retrySubmission() {
  const entry = submissionEntry.value, scope = entry?.[0], state = entry?.[1], intent = state?.intent
  if (!scope || !state || !intent || intent.busy || streaming.value || intent.status !== 'not_found') return
  await submit(scope, state, intent)
}

function restoreSubmission() {
  const intent = submission.value, state = currentState.value
  if (!intent || !state || intent.busy || streaming.value) return
  restoreContext.value = { scope: conversationScope(), epoch: pageEpoch, revision: state.revision, intent, discard: false }
  if (input.value && input.value !== intent.raw) showRestore.value = true
  else confirmRestoreSubmission()
}
function handleRestoreVisibility(visible: boolean) {
  showRestore.value = visible
  if (!visible) restoreContext.value = null
}
function confirmRestoreSubmission() {
  const context = restoreContext.value, state = currentState.value
  showRestore.value = false
  restoreContext.value = null
  if (!context || !alive || context.epoch !== pageEpoch || context.scope !== conversationScope() || !state
    || submission.value !== context.intent || state.revision !== context.revision || context.intent.busy || streaming.value) return
  if (context.discard) {
    const original = submissionEntry.value?.[1]
    if (original?.intent === context.intent) original.intent = null
  } else input.value = context.intent.raw
}
function removeSubmission() {
  const intent = submission.value, state = currentState.value
  if (!intent || !state || intent.busy || streaming.value) return
  restoreContext.value = { scope: conversationScope(), epoch: pageEpoch, revision: state.revision, intent, discard: true }
  showRestore.value = true
}
async function copySubmission() {
  const intent = submission.value, scope = conversationScope(), epoch = pageEpoch
  if (!intent) return
  const current = () => alive && epoch === pageEpoch && scope === conversationScope() && intent === submission.value
  try { await navigator.clipboard.writeText(intent.raw); if (current()) toast.success('已复制原发送稿') }
  catch { if (current()) toast.warning('自动复制失败，请在只读原发送稿中手动选中复制') }
}

function scrollToBottom() {
  nextTick(() => {
    if (alive && listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

watch(
  conversationScope,
  (scope, previousScope) => {
    pageEpoch += 1
    showRestore.value = false
    restoreContext.value = null
    if (scope && previousScope && JSON.parse(scope)[0] === JSON.parse(previousScope)[0]) {
      const previous = states.get(previousScope)
      const next = stateFor(scope)
      if (previous?.input) {
        next.input = next.input && next.input !== previous.input ? previous.input + '\n' + next.input : previous.input
        previous.input = ''
        next.revision += 1
        previous.revision += 1
      }
    }
    if (!currentState.value?.streaming) void loadMessages()
  },
  { immediate: true, flush: 'sync' },
)
watch(dirty, (value) => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(() => props.conversationIds, (ids, previousIds) => {
  if (ids) {
    const existing = new Set(ids)
    for (const scope of states.keys()) {
      if (!existing.has(JSON.parse(scope)[0])) states.delete(scope)
    }
  }
  // 会话先恢复、目录后到时，scope没有变化；补上原先因资格未就绪而跳过的只读历史。
  const id = props.conversation?.id
  if (id && previousIds && !previousIds.includes(id) && (!ids || ids.includes(id)) && !streaming.value) void loadMessages()
}, { flush: 'sync' })
function beforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => {
  alive = false
  pageEpoch += 1
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

      <section v-if="submission" class="submission-panel" aria-label="原发送稿核对">
        <p v-if="submission.status === 'saved'" role="status">原用户消息已保存；这不代表回复已完整生成。</p>
        <p v-else role="status">{{ submission.busy ? '正在处理原发送，请等待回应。' : submission.status === 'not_found' ? '核对未找到原发送，可明确按原稿重试。' : '发送结果待核对，原稿仍保留。' }}</p>
        <p class="submission-hint">原稿仅在当前页面内暂存，完整刷新或关闭前请复制。核对不自动发送，也不重新生成已记录请求的回复。</p>
        <textarea readonly :value="submission.raw" aria-label="原发送稿" class="submission-original" />
        <div class="submission-actions">
          <n-button size="small" @click="copySubmission">复制原发送稿</n-button>
          <n-button size="small" :disabled="submission.busy || streaming" @click="restoreSubmission">恢复原发送稿</n-button>
          <n-button v-if="submission.status !== 'saved'" size="small" :loading="submission.busy" :disabled="streaming" @click="checkSubmission">核对原发送</n-button>
          <n-button v-if="submission.status === 'not_found'" size="small" :disabled="submission.busy || streaming" @click="retrySubmission">原发送重试</n-button>
          <n-button size="small" :disabled="submission.busy || streaming" @click="removeSubmission">移除本地原稿</n-button>
        </div>
      </section>

      <div class="input-bar">
        <n-input
          v-model:value="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="输入构思、问题或想法，Enter 发送"
          :disabled="streaming"
          @keydown.enter.exact.prevent="send"
        />
        <n-button type="primary" :disabled="streaming || submissionUnresolved || !input.trim()" @click="send">
          发送
        </n-button>
      </div>
    </template>

    <n-empty v-else description="选择或新建一个会话开始创作讨论" class="empty" />
    <n-modal :show="showRestore" @update:show="handleRestoreVisibility" preset="card" :title="restoreContext?.discard ? '移除本地原发送稿' : '恢复原发送稿'" style="width: 500px">
      <p v-if="restoreContext?.discard">原发送可能已经保存在会话中。这里只移除本地保留的原稿，不删除已保存消息；若要保留内容，请先复制。</p>
      <p v-else>恢复会替换当前输入，请先复制新写的稿件。确认期间再次编辑或切换会话会取消这次恢复。</p>
      <template #footer><div class="submission-actions">
        <n-button @click="showRestore = false; restoreContext = null">取消</n-button>
        <n-button type="primary" @click="confirmRestoreSubmission">{{ restoreContext?.discard ? '移除本地原稿' : '替换当前输入' }}</n-button>
      </div></template>
    </n-modal>
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
.submission-panel { margin: 8px 16px; padding: 10px; border: 1px solid #e5e5e5; border-radius: 6px; max-height: 250px; overflow: auto; }
.submission-panel p { margin: 0 0 6px; font-size: 13px; }
.submission-hint { color: #666; }
.submission-original { box-sizing: border-box; width: 100%; height: 70px; resize: vertical; white-space: pre-wrap; }
.submission-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.empty {
  margin: auto;
}
</style>
