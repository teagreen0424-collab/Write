import { reactive } from 'vue'
import type { ChatAction, ChatActionBatch, ChatMessage, ChatMessageWindow, ChatSubmissionReceipt } from '../types'

export interface ChatSubmission {
  requestId: string
  conversationId: string
  raw: string
  hash: string
  token: number
  status: 'pending' | 'unknown' | 'saved' | 'not_found' | 'blocked'
  busy: boolean
}
export interface ChatSubmissionState {
  messages: ChatMessage[]
  input: string
  revision: number
  streaming: boolean
  streamingText: string
  replyNotice: string
  loadId: number
  historyLoaded: boolean
  historyLoading: boolean
  hasEarlier: boolean
  beforeMessageId: string | null
  olderLoadId: number
  olderLoading: boolean
  actions: Record<string, { status: 'pending' | 'ready' | 'failed'; items: ChatAction[]; token: number }>
  intent: ChatSubmission | null
}

// 只在当前页面运行期共享；卸载 UI 不代表发送已结束，不使用浏览器持久缓存。
export const chatSubmissionStates = reactive(new Map<string, ChatSubmissionState>())
let operationSequence = 0
export function newChatActionsToken(): number { return ++operationSequence }
export function chatSubmissionState(scope: string): ChatSubmissionState {
  if (!chatSubmissionStates.has(scope)) chatSubmissionStates.set(scope, {
    messages: [], input: '', revision: 0, streaming: false, streamingText: '', replyNotice: '', loadId: 0, intent: null,
    historyLoaded: false, historyLoading: false, hasEarlier: false, beforeMessageId: null,
    olderLoadId: 0, olderLoading: false, actions: {},
  })
  return chatSubmissionStates.get(scope)!
}
export function newChatSubmission(conversationId: string, raw: string): ChatSubmission {
  return { requestId: crypto.randomUUID(), conversationId, raw, hash: '', token: ++operationSequence, status: 'pending', busy: true }
}
export function removeChatSubmissions(conversationIds: string[], novelId?: string) {
  const ids = new Set(conversationIds)
  for (const scope of chatSubmissionStates.keys()) {
    const [id, boundNovel] = JSON.parse(scope)
    if (ids.has(id) || (novelId && boundNovel === novelId)) chatSubmissionStates.delete(scope)
  }
}
export async function hashChatSubmission(raw: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw))
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}
export function isChatSubmissionReceipt(value: unknown, intent: ChatSubmission): value is ChatSubmissionReceipt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return row.request_id === intent.requestId && row.user_message_id === intent.requestId
    && row.conversation_id === intent.conversationId && row.content === intent.raw && row.content_hash === intent.hash
    && /^[0-9a-f]{64}$/.test(intent.hash) && row.accepted === true && typeof row.receipt_replayed === 'boolean'
}
export function isChatReplyDone(value: unknown, intent: ChatSubmission): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return row.type === 'done' && row.request_id === intent.requestId && row.conversation_id === intent.conversationId
    && row.user_message_id === intent.requestId && typeof row.title === 'string'
    && typeof row.assistant_message_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(row.assistant_message_id)
    && row.assistant_message_id !== intent.requestId
}

export function isChatMessageWindow(value: unknown, conversationId: string, novelId: string | null): value is ChatMessageWindow {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as ChatMessageWindow
  if (row.conversation_id !== conversationId || row.novel_id !== novelId || !Array.isArray(row.items)
    || row.items.length > 100 || typeof row.has_more !== 'boolean') return false
  const ids = new Set<string>()
  for (const item of row.items) {
    if (!item || typeof item !== 'object' || typeof item.id !== 'string' || !item.id || item.id.startsWith('local-')
      || ids.has(item.id) || item.conversation_id !== conversationId || !['user', 'assistant'].includes(item.role)
      || typeof item.content !== 'string' || typeof item.created_at !== 'string'
      || (item.meta !== null && (!item.meta || typeof item.meta !== 'object' || Array.isArray(item.meta)))) return false
    ids.add(item.id)
  }
  return row.has_more ? row.items.length > 0 && row.next_before_message_id === row.items[0].id
    : row.next_before_message_id === null
}

export function isChatActionBatch(value: unknown, conversationId: string, novelId: string | null, ids: string[]): value is ChatActionBatch {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as ChatActionBatch
  if (row.conversation_id !== conversationId || row.novel_id !== novelId || !row.actions_by_message_id
    || typeof row.actions_by_message_id !== 'object' || Array.isArray(row.actions_by_message_id)) return false
  if (Object.keys(row.actions_by_message_id).length !== ids.length) return false
  return ids.every(id => Array.isArray(row.actions_by_message_id[id]) && row.actions_by_message_id[id].every(action =>
    !!action && typeof action === 'object' && !Array.isArray(action)
    && typeof action.action_type === 'string' && typeof action.status === 'string' && typeof action.result_id === 'string'
    && Object.prototype.hasOwnProperty.call(action, 'result_summary') && action.result_summary !== undefined
    && typeof action.target_deleted === 'boolean',
  ))
}

// 使用服务端已确定的顺序；不能用UUID重排同时间戳消息。
export function mergeChatMessages(existing: ChatMessage[], incoming: ChatMessage[], beforeId?: string): ChatMessage[] {
  const ids = new Set(incoming.map(item => item.id))
  const kept = existing.filter(item => !ids.has(item.id) && !ids.has(item.id.replace(/^local-/, '')))
  if (beforeId) {
    const anchor = kept.findIndex(item => item.id === beforeId)
    if (anchor < 0) return existing
    return [...kept.slice(0, anchor), ...incoming, ...kept.slice(anchor)]
  }
  return [...kept.filter(item => !item.id.startsWith('local-')), ...incoming, ...kept.filter(item => item.id.startsWith('local-'))]
}
