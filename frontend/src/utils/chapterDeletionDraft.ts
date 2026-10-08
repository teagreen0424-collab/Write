import { reactive } from 'vue'
import type { ChapterAuthorFields } from '../types'
import type { ManualChapterFact } from './manualChapterDraft'

export interface ChapterDeletionManualGuard {
  version: number
  revision: number
  submissionOperation: number | null
  author: ChapterAuthorFields
}
export interface ChapterDeletionSubmission {
  operation: number
  author: Readonly<ChapterAuthorFields>
  revision: number
  manual: ChapterDeletionManualGuard | null
  outcome: 'pending' | 'unknown' | 'confirmed'
  error: string
}
export interface ChapterDeletionDraft {
  key: string
  novelId: string
  chapterId: string
  fact: ManualChapterFact
  draft: ChapterAuthorFields
  baseline: ChapterAuthorFields
  revision: number
  version: number
  phase: 'waiting' | 'unknown' | 'missing' | 'released'
  pending: number | null
  checking: number | null
  exists: boolean
  observed: ManualChapterFact | null
  observedRead: ChapterDeletionRead | null
  submission: ChapterDeletionSubmission
  retained: ChapterDeletionSubmission[]
  error: string
}
export interface ChapterDeletionRead {
  version: number
  revision: number
  manual: ChapterDeletionManualGuard | null
}

const records = reactive(new Map<string, ChapterDeletionDraft>())
let sequence = 0
let unloadInstalled = false
const keyOf = (novelId: string, chapterId: string) => JSON.stringify([novelId, chapterId])
const authorCopy = (row: ChapterAuthorFields): ChapterAuthorFields => ({ title: row.title, directive: row.directive, content: row.content })
const sameAuthor = (a: ChapterAuthorFields, b: ChapterAuthorFields) => a.title === b.title && a.directive === b.directive && a.content === b.content
const guardCopy = (guard: ChapterDeletionManualGuard | null) => guard ? { ...guard, author: authorCopy(guard.author) } : null
export const sameChapterDeletionManualGuard = (a: ChapterDeletionManualGuard | null, b: ChapterDeletionManualGuard | null) => a === null ? b === null
  : b !== null && a.version === b.version && a.revision === b.revision && a.submissionOperation === b.submissionOperation && sameAuthor(a.author, b.author)
export const findChapterDeletionDraft = (novelId: string, chapterId: string) => records.get(keyOf(novelId, chapterId))
export const chapterDeletionDrafts = () => [...records.values()]
export const chapterDeletionWriteBlocked = (state: ChapterDeletionDraft | undefined | null) => !!state
  && (state.pending !== null || state.checking !== null || !state.exists || state.phase === 'unknown')
export const chapterDeletionDisposed = (state: ChapterDeletionDraft) => state.submission.outcome === 'confirmed'
  && state.revision === state.submission.revision && sameAuthor(state.draft, state.submission.author)
export const chapterDeletionNeedsWarning = (state: ChapterDeletionDraft) => state.pending !== null
  || state.submission.outcome === 'unknown' || state.retained.some(row => row.outcome === 'unknown')
  || !chapterDeletionDisposed(state)

function installUnload() {
  if (unloadInstalled || typeof window === 'undefined') return
  unloadInstalled = true
  window.addEventListener('beforeunload', event => {
    if (!chapterDeletionDrafts().some(chapterDeletionNeedsWarning)) return
    event.preventDefault()
    event.returnValue = ''
  })
}

export function beginChapterDeletion(novelId: string, chapterId: string, fact: ManualChapterFact,
  author: ChapterAuthorFields, baseline: ChapterAuthorFields, manual: ChapterDeletionManualGuard | null): ChapterDeletionDraft | null {
  const key = keyOf(novelId, chapterId), old = records.get(key)
  if (chapterDeletionWriteBlocked(old)) return null
  const operation = ++sequence
  const revision = old?.revision ?? 0
  const submission: ChapterDeletionSubmission = { operation, author: Object.freeze(authorCopy(author)), revision,
    manual: guardCopy(manual), outcome: 'pending', error: '' }
  if (old) {
    if (old.submission.outcome === 'unknown') old.retained.push({ ...old.submission, author: Object.freeze(authorCopy(old.submission.author)), manual: guardCopy(old.submission.manual) })
    old.submission = submission
    old.fact = { ...fact }
    old.draft = authorCopy(author)
    old.baseline = authorCopy(baseline)
    old.pending = operation
    old.phase = 'waiting'
    old.observed = null
    old.observedRead = null
    old.error = ''
    old.version += 1
  } else records.set(key, { key, novelId, chapterId, fact: { ...fact }, draft: authorCopy(author), baseline: authorCopy(baseline),
    revision, version: 1, phase: 'waiting', pending: operation, checking: null, exists: true, observed: null, observedRead: null,
    submission, retained: [], error: '' })
  installUnload()
  return records.get(key)!
}

export function editChapterDeletion(state: ChapterDeletionDraft, author: ChapterAuthorFields) {
  if (sameAuthor(state.draft, author)) return
  state.draft = authorCopy(author)
  state.revision += 1
}

export function synchronizeChapterDeletion(state: ChapterDeletionDraft, fact: ManualChapterFact, baseline: ChapterAuthorFields) {
  if (state.pending !== null || !state.exists || state.phase === 'unknown') return
  state.fact = { ...fact }
  state.baseline = authorCopy(baseline)
  state.version += 1
  state.observed = null
  state.observedRead = null
}

export function finishChapterDeletion(state: ChapterDeletionDraft, operation: number): boolean {
  if (state.pending !== operation || state.submission.operation !== operation) return false
  state.submission.outcome = 'confirmed'
  state.submission.error = ''
  state.pending = null
  state.exists = false
  state.phase = 'missing'
  state.observed = null
  state.observedRead = null
  state.error = ''
  state.version += 1
  return true
}

export function failChapterDeletion(state: ChapterDeletionDraft, operation: number, error: string, missing = false) {
  if (state.pending !== operation || state.submission.operation !== operation) return
  state.submission.outcome = 'unknown'
  state.submission.error = error
  state.pending = null
  if (missing) state.exists = false
  state.phase = state.exists ? 'unknown' : 'missing'
  state.observed = null
  state.observedRead = null
  state.error = error
  state.version += 1
}

export const chapterDeletionRead = (state: ChapterDeletionDraft, manual: ChapterDeletionManualGuard | null): ChapterDeletionRead =>
  ({ version: state.version, revision: state.revision, manual: guardCopy(manual) })
export const currentChapterDeletionRead = (state: ChapterDeletionDraft, read: ChapterDeletionRead, manual: ChapterDeletionManualGuard | null) =>
  read.version === state.version && read.revision === state.revision && sameChapterDeletionManualGuard(read.manual, manual)

export function beginChapterDeletionCheck(state: ChapterDeletionDraft, manual: ChapterDeletionManualGuard | null) {
  if (state.checking !== null) return null
  const operation = ++sequence
  state.checking = operation
  state.observed = null
  state.observedRead = null
  state.version += 1
  return { operation, read: chapterDeletionRead(state, manual) }
}

export function observeChapterDeletion(state: ChapterDeletionDraft, read: ChapterDeletionRead, manual: ChapterDeletionManualGuard | null, fact: ManualChapterFact): boolean {
  if (!currentChapterDeletionRead(state, read, manual) || !state.exists) return false
  state.observed = { ...fact }
  state.observedRead = chapterDeletionRead(state, manual)
  return true
}

export function missingChapterDeletion(state: ChapterDeletionDraft, read: ChapterDeletionRead, manual: ChapterDeletionManualGuard | null): boolean {
  if (!currentChapterDeletionRead(state, read, manual)) return false
  state.exists = false
  state.phase = 'missing'
  state.observed = null
  state.observedRead = null
  state.version += 1
  state.error = '原章节当前不存在，仅可复制页内稿。当前缺失不能证明原未知删除已成功。'
  return true
}

export function releaseChapterDeletionDraft(state: ChapterDeletionDraft, manual: ChapterDeletionManualGuard | null): boolean {
  if (state.pending !== null || state.checking !== null || !state.exists || !state.observed || !state.observedRead
    || state.submission.outcome !== 'unknown' || !currentChapterDeletionRead(state, state.observedRead, manual)) return false
  state.baseline = authorCopy(state.observed)
  state.fact = { ...state.observed }
  state.phase = 'released'
  state.version += 1
  state.error = ''
  return true
}
