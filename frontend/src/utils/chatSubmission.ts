import { reactive } from 'vue'
import type { ChatMessage, ChatSubmissionReceipt } from '../types'

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
  intent: ChatSubmission | null
}

// 只在当前页面运行期共享；卸载 UI 不代表发送已结束，不使用浏览器持久缓存。
export const chatSubmissionStates = reactive(new Map<string, ChatSubmissionState>())
let operationSequence = 0
export function chatSubmissionState(scope: string): ChatSubmissionState {
  if (!chatSubmissionStates.has(scope)) chatSubmissionStates.set(scope, {
    messages: [], input: '', revision: 0, streaming: false, streamingText: '', replyNotice: '', loadId: 0, intent: null,
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
