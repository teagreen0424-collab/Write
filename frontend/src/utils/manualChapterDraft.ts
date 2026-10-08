import { reactive } from 'vue'
import type { Chapter, ChapterAuthorFields } from '../types'

export type ManualChapterFact = Omit<Chapter, 'status'> & { status: string }
export interface ManualChapterSubmission {
  operation: number
  author: Readonly<ChapterAuthorFields>
  baseline: Readonly<ChapterAuthorFields>
  payloadJson: string
  revision: number
  outcome: 'pending' | 'known' | 'unknown' | 'failed'
  error: string
}
export interface ManualChapterDraft {
  key: string
  novelId: string
  chapterId: string
  draft: ChapterAuthorFields
  baseline: ChapterAuthorFields
  revision: number
  version: number
  phase: 'ready' | 'pending' | 'known' | 'unknown' | 'missing'
  pending: number | null
  checking: number | null
  exists: boolean
  observed: ManualChapterFact | null
  saved: ManualChapterFact | null
  submission: ManualChapterSubmission | null
  retained: ManualChapterSubmission[]
  error: string
  deletionDisposition?: { operation: number; revision: number; submissionOperation: number | null; author: ChapterAuthorFields }
}
export interface ManualChapterRead {
  key: string
  revision: number
  version: number
  clean: boolean
  quiet: boolean
}

const records = reactive(new Map<string, ManualChapterDraft>())
let sequence = 0
let unloadInstalled = false
const keyOf = (novelId: string, chapterId: string) => JSON.stringify([novelId, chapterId])
export const manualChapterAuthor = (value: ChapterAuthorFields): ChapterAuthorFields => ({ title: value.title, directive: value.directive, content: value.content })
const sameAuthor = (a: ChapterAuthorFields, b: ChapterAuthorFields) => a.title === b.title && a.directive === b.directive && a.content === b.content
export const manualChapterDirty = (state: ManualChapterDraft) => !sameAuthor(state.draft, state.baseline)
export const manualChapterWriteBlocked = (state: ManualChapterDraft | undefined | null) => !!state && (state.pending !== null || state.checking !== null || state.phase === 'unknown' || !state.exists)
export const findManualChapterDraft = (novelId: string, chapterId: string) => records.get(keyOf(novelId, chapterId))
export const manualChapterDrafts = () => [...records.values()]
export const manualChapterNeedsWarning = (state: ManualChapterDraft) => state.pending !== null || state.submission?.outcome === 'unknown'
  || state.retained.some(submission => submission.outcome === 'unknown')
  || ((!state.exists || manualChapterDirty(state)) && !(state.deletionDisposition
    && state.deletionDisposition.revision === state.revision && state.deletionDisposition.submissionOperation === (state.submission?.operation ?? null)
    && sameAuthor(state.deletionDisposition.author, state.draft)))

export function markManualChapterDeleted(novelId: string, chapterId: string,
  disposition?: { operation: number; revision: number; submissionOperation: number | null; author: ChapterAuthorFields }) {
  const state = findManualChapterDraft(novelId, chapterId)
  if (!state) return
  state.exists = false
  state.phase = 'missing'
  state.observed = null
  if (disposition) state.deletionDisposition = { ...disposition, author: manualChapterAuthor(disposition.author) }
  state.version += 1
  state.error = '原章节当前不存在，页内原提交和后来稿仅可复制，不会重新创建。'
}

export function completeManualChapter(value: unknown, novelId: string, chapterId: string): value is ManualChapterFact {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return row.id === chapterId && row.novel_id === novelId && typeof row.id === 'string' && typeof row.novel_id === 'string'
    && ['outline_node_id', 'title', 'content', 'status', 'summary', 'directive', 'created_at', 'updated_at'].every(name => typeof row[name] === 'string')
    && typeof row.seq === 'number' && Number.isInteger(row.seq)
    && ['tension_score', 'drift_score'].every(name => row[name] === null || (typeof row[name] === 'number' && Number.isFinite(row[name])))
}

const factCopy = (row: ManualChapterFact): ManualChapterFact => ({ id: row.id, novel_id: row.novel_id, outline_node_id: row.outline_node_id,
  seq: row.seq, title: row.title, content: row.content, status: row.status, summary: row.summary, tension_score: row.tension_score,
  drift_score: row.drift_score, directive: row.directive, created_at: row.created_at, updated_at: row.updated_at })

function installUnload() {
  if (unloadInstalled || typeof window === 'undefined') return
  unloadInstalled = true
  window.addEventListener('beforeunload', event => {
    if (!manualChapterDrafts().some(manualChapterNeedsWarning)) return
    event.preventDefault()
    event.returnValue = ''
  })
}

export function manualChapterDraft(novelId: string, chapterId: string, fact: ManualChapterFact,
  draft: ChapterAuthorFields, baseline: ChapterAuthorFields): ManualChapterDraft {
  const key = keyOf(novelId, chapterId)
  if (!records.has(key)) records.set(key, { key, novelId, chapterId, draft: manualChapterAuthor(draft), baseline: manualChapterAuthor(baseline),
    revision: 0, version: 0, phase: 'ready', pending: null, checking: null, exists: true,
    observed: factCopy(fact), saved: factCopy(fact), submission: null, retained: [], error: '' })
  installUnload()
  return records.get(key)!
}

export function editManualChapter(state: ManualChapterDraft, author: ChapterAuthorFields) {
  if (sameAuthor(state.draft, author)) return
  state.draft = manualChapterAuthor(author)
  state.revision += 1
}

export function beginManualChapterSave(state: ManualChapterDraft): ManualChapterSubmission | null {
  if (manualChapterWriteBlocked(state) || !manualChapterDirty(state)) return null
  if (state.submission?.outcome === 'unknown') state.retained.push({ ...state.submission, author: Object.freeze(manualChapterAuthor(state.submission.author)), baseline: Object.freeze(manualChapterAuthor(state.submission.baseline)) })
  const operation = ++sequence
  state.submission = { operation, author: Object.freeze(manualChapterAuthor(state.draft)), baseline: Object.freeze(manualChapterAuthor(state.baseline)),
    payloadJson: JSON.stringify({ ...manualChapterAuthor(state.draft), revision_source: 'manual_save' }), revision: state.revision, outcome: 'pending', error: '' }
  state.pending = operation
  state.phase = 'pending'
  state.error = ''
  state.observed = null
  state.version += 1
  return state.submission
}

export function finishManualChapterSave(state: ManualChapterDraft, operation: number, value: unknown): boolean {
  const submitted = state.submission
  if (state.pending !== operation || submitted?.operation !== operation) return false
  if (!completeManualChapter(value, state.novelId, state.chapterId) || !sameAuthor(value, submitted.author)) {
    failManualChapterSave(state, operation, '未收到对应原提交的完整章节确认，请保留稿件并核对当前已保存章节。')
    return false
  }
  submitted.outcome = 'known'
  submitted.error = ''
  state.saved = factCopy(value)
  state.baseline = manualChapterAuthor(submitted.author)
  if (state.exists) state.observed = factCopy(value)
  state.phase = state.exists ? 'known' : 'missing'
  state.pending = null
  state.version += 1
  state.error = ''
  return true
}

export function failManualChapterSave(state: ManualChapterDraft, operation: number, error: string, missing = false) {
  if (state.pending !== operation || state.submission?.operation !== operation) return
  state.submission.outcome = missing ? 'failed' : 'unknown'
  state.submission.error = error
  if (missing) state.exists = false
  state.phase = state.exists ? 'unknown' : 'missing'
  state.pending = null
  state.observed = null
  state.version += 1
  state.error = error
}

export const manualChapterRead = (state: ManualChapterDraft): ManualChapterRead => ({ key: state.key, revision: state.revision, version: state.version,
  clean: !manualChapterDirty(state), quiet: state.pending === null && state.checking === null && state.phase !== 'unknown' && state.exists })
const currentRead = (state: ManualChapterDraft, read: ManualChapterRead) => state.key === read.key && state.version === read.version && state.revision === read.revision

export function observeManualChapter(state: ManualChapterDraft, read: ManualChapterRead, value: unknown, refresh = false): boolean {
  if (!currentRead(state, read) || !completeManualChapter(value, state.novelId, state.chapterId)) return false
  state.observed = factCopy(value)
  if (refresh && read.clean && read.quiet && !manualChapterDirty(state) && !manualChapterWriteBlocked(state)) {
    state.draft = manualChapterAuthor(value)
    state.baseline = manualChapterAuthor(value)
    state.saved = factCopy(value)
    state.phase = 'known'
    state.version += 1
  }
  return true
}

export function missingManualChapter(state: ManualChapterDraft, read: ManualChapterRead) {
  if (!currentRead(state, read)) return
  state.exists = false
  state.phase = 'missing'
  state.observed = null
  state.version += 1
  state.error = '原章节当前不存在，页内原提交和后来稿仅可复制，不会重新创建。'
}

export function beginManualChapterCheck(state: ManualChapterDraft) {
  if (state.checking !== null) return null
  const operation = ++sequence
  state.checking = operation
  state.observed = null
  state.version += 1
  return { operation, read: manualChapterRead(state) }
}

export function adoptManualChapter(state: ManualChapterDraft): boolean {
  if (state.pending !== null || state.checking !== null || !state.exists || !state.observed) return false
  state.baseline = manualChapterAuthor(state.observed)
  state.saved = factCopy(state.observed)
  state.phase = 'ready'
  state.error = ''
  state.version += 1
  return true
}

export function synchronizeManualChapter(state: ManualChapterDraft, chapter: unknown, author: ChapterAuthorFields, baseline: ChapterAuthorFields) {
  if (manualChapterWriteBlocked(state) || !completeManualChapter(chapter, state.novelId, state.chapterId)) return
  state.draft = manualChapterAuthor(author)
  state.baseline = manualChapterAuthor(baseline)
  state.saved = factCopy(chapter)
  state.observed = factCopy(chapter)
  state.version += 1
}
