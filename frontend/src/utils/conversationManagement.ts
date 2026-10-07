import { effectScope, reactive, watch } from 'vue'
import type { Conversation } from '../types'

export interface RenameDraft {
  id: string
  draft: string
  baseline: string
  revision: number
  original: string | null
  source: Conversation
  saved: Conversation | null
  observed: Conversation | null
  phase: 'idle' | 'pending' | 'saved' | 'unknown'
  pending: number | null
  checking: number | null
  exists: boolean
  error: string
}
export interface ConversationCreation {
  key: string
  novelId: string | null
  phase: 'pending' | 'saved' | 'unknown' | 'idle'
  pending: number | null
  checking: number | null
  saved: Conversation | null
  exists: boolean
  directory: Conversation[] | null
  error: string
}

// 当前页面的两个具体操作记录。完整刷新前复制；不作持久回执或自动重发。
export const renameDrafts = reactive(new Map<string, RenameDraft>())
export const conversationCreations = reactive(new Map<string, ConversationCreation>())
let sequence = 0
export const managementOperation = () => ++sequence
export const conversationEntry = (novelId: string | null) => novelId === null ? 'global' : `novel:${novelId}`

export function isConversation(value: unknown): value is Conversation {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, unknown>
  return typeof row.id === 'string' && !!row.id &&
    (row.novel_id === null || (typeof row.novel_id === 'string' && !!row.novel_id)) &&
    ['title', 'created_at', 'updated_at'].every(key => typeof row[key] === 'string')
}

export function renameDraft(source: Conversation) {
  let state = renameDrafts.get(source.id)
  if (!state) {
    state = reactive<RenameDraft>({ id: source.id, draft: source.title, baseline: source.title,
      revision: 0, original: null, source: { ...source }, saved: null, observed: null,
      phase: 'idle', pending: null, checking: null, exists: true, error: '' })
    const record = state
    effectScope(true).run(() => watch(() => record.draft, () => { record.revision += 1 }, { flush: 'sync' }))
    renameDrafts.set(source.id, state)
  }
  return state
}

export function creationEntry(novelId: string | null) {
  const key = conversationEntry(novelId)
  let state = conversationCreations.get(key)
  if (!state) {
    state = reactive<ConversationCreation>({ key, novelId, phase: 'idle', pending: null,
      checking: null, saved: null, exists: true, directory: null, error: '' })
    conversationCreations.set(key, state)
  }
  return state
}

export const renameDirty = (state: RenameDraft) => state.pending !== null || state.phase === 'unknown' || state.draft !== state.baseline
export const managementDirty = () => [...renameDrafts.values()].some(renameDirty) ||
  [...conversationCreations.values()].some(state => state.pending !== null || state.phase === 'unknown')
export const existingRenameDrafts = () => [...renameDrafts.values()].filter(state => state.original !== null || renameDirty(state))

export function observeConversations(items: Conversation[]) {
  const ids = new Set(items.map(item => item.id))
  for (const state of renameDrafts.values()) state.exists = ids.has(state.id)
  for (const state of conversationCreations.values()) if (state.saved) state.exists = ids.has(state.saved.id)
}

export function deletedConversations(ids: string[]) {
  const removed = new Set(ids)
  for (const state of renameDrafts.values()) if (removed.has(state.id)) state.exists = false
  for (const state of conversationCreations.values()) if (state.saved && removed.has(state.saved.id)) state.exists = false
}
