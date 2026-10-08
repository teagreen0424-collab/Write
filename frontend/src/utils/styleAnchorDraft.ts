import { effectScope, reactive, watch } from 'vue'
import type { Novel } from '../types'

export interface StyleAnchorDraft {
  novelId: string
  title: string
  draft: string
  baseline: string
  revision: number
  version: number
  initialized: boolean
  original: string | null
  phase: 'idle' | 'pending' | 'saved' | 'unknown'
  pending: number | null
  checking: number | null
  saved: Novel | null
  observed: Novel | null
  exists: boolean
  error: string
}

// 仅在本页面内协调同作品的文风保存；不是数据库回执，也不跨独立页签。
const drafts = reactive(new Map<string, StyleAnchorDraft>())
let operation = 0
export const anchorOperation = () => ++operation

export function styleAnchorDraft(novelId: string): StyleAnchorDraft {
  let state = drafts.get(novelId)
  if (!state) {
    state = reactive<StyleAnchorDraft>({ novelId, title: novelId, draft: '', baseline: '', revision: 0,
      version: 0, initialized: false, original: null, phase: 'idle', pending: null,
      checking: null, saved: null, observed: null, exists: true, error: '' })
    const record = state
    effectScope(true).run(() => watch(() => record.draft, () => { record.revision += 1 }, { flush: 'sync' }))
    drafts.set(novelId, state)
  }
  return state
}

export const anchorDirty = (state: StyleAnchorDraft) => state.pending !== null ||
  state.phase === 'unknown' || state.draft !== state.baseline

export const styleAnchorDrafts = () => [...drafts.values()].filter(state => state.original !== null || anchorDirty(state))

// 设定面板卸载后，其他作品的等待或未保存文风仍可能留在本页面。
if (typeof window !== 'undefined') window.addEventListener('beforeunload', (event) => {
  if (![...drafts.values()].some(anchorDirty)) return
  event.preventDefault()
  event.returnValue = ''
})

export function isAnchorNovel(value: unknown): value is Novel {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, unknown>
  return typeof row.id === 'string' && !!row.id &&
    ['title', 'genre', 'summary', 'style_anchor', 'created_at', 'updated_at'].every(key => typeof row[key] === 'string')
}
