import type { ChapterAuthorFields, ChapterCreationResult } from '../types'

export interface ChapterCreationIntent {
  version: 1
  novelId: string
  requestId: string
  payloadJson: string
  draft: ChapterAuthorFields
  edited: boolean
  createdAt: number
  blockedCode?: string
}

const PREFIX = 'lingmo:chapter-creation:v1:'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const keyFor = (novelId: string, requestId: string) => `${PREFIX}${encodeURIComponent(novelId)}:${requestId}`
const authorFields = (value: unknown): value is ChapterAuthorFields => !!value && typeof value === 'object'
  && ['title', 'content', 'directive'].every((field) => typeof (value as Record<string, unknown>)[field] === 'string')

export function newChapterCreation(novelId: string, draft: ChapterAuthorFields): ChapterCreationIntent {
  const requestId = crypto.randomUUID()
  return { version: 1, novelId, requestId,
    payloadJson: JSON.stringify({ ...draft, status: 'draft', request_id: requestId }),
    draft: { ...draft }, edited: false, createdAt: Date.now() }
}

export function isChapterCreationResult(value: unknown): value is ChapterCreationResult {
  if (!authorFields(value)) return false
  const row = value as unknown as Record<string, unknown>
  return typeof row.id === 'string' && row.id.length > 0 && typeof row.novel_id === 'string'
    && ['outline_node_id', 'summary', 'created_at', 'updated_at'].every((field) => typeof row[field] === 'string')
    && Number.isSafeInteger(row.seq) && (row.seq as number) > 0
    && (row.status === 'draft' || row.status === 'finalized')
    && ['tension_score', 'drift_score'].every((field) => row[field] === null || (typeof row[field] === 'number' && Number.isFinite(row[field])))
    && typeof row.creation_replayed === 'boolean' && typeof row.author_changed_since_creation === 'boolean'
}

function parseIntent(raw: string | null, novelId: string, key: string): ChapterCreationIntent | null {
  if (!raw) return null
  const value = JSON.parse(raw) as ChapterCreationIntent
  if (value.version !== 1 || value.novelId !== novelId || typeof value.requestId !== 'string' || !UUID.test(value.requestId)
    || key !== keyFor(novelId, value.requestId) || typeof value.payloadJson !== 'string' || !authorFields(value.draft)
    || typeof value.edited !== 'boolean' || !Number.isFinite(value.createdAt)
    || (value.blockedCode !== undefined && typeof value.blockedCode !== 'string')) return null
  const payload = JSON.parse(value.payloadJson)
  const extra = payload as ChapterAuthorFields & { request_id?: unknown; status?: unknown }
  if (!authorFields(payload) || extra.request_id !== value.requestId || extra.status !== 'draft'
    || Object.keys(payload).some((field) => !['title', 'content', 'directive', 'status', 'request_id'].includes(field))
    || (!value.edited && ['title', 'content', 'directive'].some((field) => value.draft[field as keyof ChapterAuthorFields] !== payload[field as keyof ChapterAuthorFields]))) return null
  return value
}

export function storeChapterCreation(intent: ChapterCreationIntent): boolean {
  try { localStorage.setItem(keyFor(intent.novelId, intent.requestId), JSON.stringify(intent)); return true }
  catch { return false }
}

export function listChapterCreations(novelId: string): { records: ChapterCreationIntent[]; unavailable: boolean; invalid: number } {
  const records: ChapterCreationIntent[] = []
  let invalid = 0
  try {
    const prefix = `${PREFIX}${encodeURIComponent(novelId)}:`
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index)
      if (!key?.startsWith(prefix)) continue
      try {
        const record = parseIntent(localStorage.getItem(key), novelId, key)
        if (record) records.push(record)
        else invalid += 1
      } catch { invalid += 1 }
    }
    return { records: records.sort((a, b) => b.createdAt - a.createdAt), unavailable: false, invalid }
  } catch { return { records, unavailable: true, invalid } }
}

export function removeChapterCreation(novelId: string, requestId: string): boolean {
  try { localStorage.removeItem(keyFor(novelId, requestId)); return true }
  catch { return false }
}

export function clearNovelChapterCreations(novelId: string): boolean {
  try {
    const prefix = `${PREFIX}${encodeURIComponent(novelId)}:`
    const keys: string[] = []
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index)
      if (key?.startsWith(prefix)) keys.push(key)
    }
    let cleared = true
    for (const key of keys) {
      try { localStorage.removeItem(key) } catch { cleared = false }
    }
    return cleared
  } catch { return false }
}
