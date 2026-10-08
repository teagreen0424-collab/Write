import { effectScope, reactive, watch } from 'vue'
import { getEntityConfig, type EntityRecord, type EntityType, type FieldConfig } from '../types'

export interface EntityDraft {
  key: string
  novelId: string
  type: EntityType
  recordId: string | null
  draft: Record<string, any>
  baseline: Record<string, any>
  revision: number
  version: number
  original: Record<string, any> | null
  payload: Record<string, unknown> | null
  phase: 'idle' | 'pending' | 'saved' | 'unknown'
  pending: number | null
  checking: number | null
  saved: EntityRecord | null
  observed: EntityRecord | null
  exists: boolean
  error: string
}

// 单个页面内协调实际目标或一次明确创建意图，不按名称或内容推断创建身份。
const records = reactive(new Map<string, EntityDraft>())
const creations = new Map<string, EntityDraft>()
const facts = new Map<string, number>()
let sequence = 0
export const entityOperation = () => ++sequence
export const entityFactVersion = (novelId: string) => facts.get(novelId) ?? 0
export const advanceEntityFacts = (novelId: string) => facts.set(novelId, entityFactVersion(novelId) + 1)
const recordKey = (novelId: string, type: EntityType, id: string) => JSON.stringify([novelId, type, id])
export const cloneEntityForm = (form: Record<string, any>) => JSON.parse(JSON.stringify(form)) as Record<string, any>

export function emptyEntityForm(type: EntityType): Record<string, any> {
  return Object.fromEntries(getEntityConfig(type).fields.map(field => [field.key,
    field.type === 'select' ? field.options?.[0]?.value ?? '' : '']))
}

export function entityForm(type: EntityType, item: EntityRecord): Record<string, any> {
  return Object.fromEntries(getEntityConfig(type).fields.map(field => [field.key,
    field.type === 'json' ? (item[field.key] == null ? '' : JSON.stringify(item[field.key], null, 2)) : item[field.key] ?? '']))
}

export function fieldValue(field: FieldConfig, raw: unknown): unknown {
  if (field.type !== 'json') return raw ?? ''
  if (raw == null || String(raw).trim() === '') return field.key === 'extras' ? {} : []
  return JSON.parse(String(raw))
}

export function semanticJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(semanticJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const row = value as Record<string, unknown>
    return `{${Object.keys(row).sort().map(key => `${JSON.stringify(key)}:${semanticJson(row[key])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'undefined'
}

export function sameFieldValue(field: FieldConfig, current: unknown, baseline: unknown): boolean {
  try { return semanticJson(fieldValue(field, current)) === semanticJson(fieldValue(field, baseline)) }
  catch { return false }
}

export const entityFormDirty = (state: EntityDraft) => getEntityConfig(state.type).fields
  .some(field => !sameFieldValue(field, state.draft[field.key], state.baseline[field.key]))
export const entityDraftDirty = (state: EntityDraft) => state.pending !== null || state.phase === 'unknown' || entityFormDirty(state)
export const entityDrafts = () => [...new Set(records.values())].filter(state => state.original !== null || entityDraftDirty(state))
export const entityReadVersions = (novelId: string) => new Map([...new Set(records.values())]
  .filter(state => state.novelId === novelId).map(state => [state.key,
    { revision: state.revision, version: state.version, clean: !entityFormDirty(state) }]))

function makeDraft(novelId: string, type: EntityType, recordId: string | null, key: string, form: Record<string, any>) {
  const state = reactive<EntityDraft>({ key, novelId, type, recordId, draft: cloneEntityForm(form), baseline: cloneEntityForm(form),
    revision: 0, version: 0, original: null, payload: null, phase: 'idle', pending: null, checking: null,
    saved: null, observed: null, exists: true, error: '' })
  effectScope(true).run(() => watch(() => state.draft, () => { state.revision += 1 }, { deep: true, flush: 'sync' }))
  records.set(key, state)
  return state
}

export function entityCreation(novelId: string, type: EntityType, fresh = false): EntityDraft {
  const key = JSON.stringify([novelId, type])
  let state = creations.get(key)
  if (!state || fresh) {
    state = makeDraft(novelId, type, null, recordKey(novelId, type, `new:${entityOperation()}`), emptyEntityForm(type))
    creations.set(key, state)
  }
  return state
}

export function entityRecord(novelId: string, type: EntityType, item: EntityRecord): EntityDraft {
  const key = recordKey(novelId, type, item.id)
  return records.get(key) ?? makeDraft(novelId, type, item.id, key, entityForm(type, item))
}

export function bindEntityId(state: EntityDraft, id: string) {
  state.recordId = id
  records.set(recordKey(state.novelId, state.type, id), state)
}

export function isEntityRead(value: unknown, novelId: string, type: EntityType, id?: string | null): value is EntityRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  if (typeof row.id !== 'string' || !row.id || row.novel_id !== novelId || (id && row.id !== id)
    || typeof row.created_at !== 'string' || typeof row.updated_at !== 'string') return false
  return getEntityConfig(type).fields.every(field => {
    if (!Object.prototype.hasOwnProperty.call(row, field.key)) return false
    const item = row[field.key]
    if (field.type !== 'json') return typeof item === 'string'
    return field.key === 'extras' ? item !== null && typeof item === 'object' && !Array.isArray(item) : Array.isArray(item)
  })
}

export const matchesEntityPayload = (row: EntityRecord, payload: Record<string, unknown>) =>
  Object.entries(payload).every(([key, value]) => semanticJson(row[key]) === semanticJson(value))

// 只有当前读取版本的干净稿跟随新事实。旧读取、等待、未知、删除副本均不能隐式确认。
export function refreshEntityDrafts(novelId: string, lists: Record<EntityType, EntityRecord[]>,
  versions: ReturnType<typeof entityReadVersions>) {
  for (const state of new Set(records.values())) {
    if (state.novelId !== novelId || !state.recordId || !state.exists || state.pending !== null
      || state.checking !== null || state.phase === 'unknown') continue
    const expected = versions.get(state.key)
    if (!expected || expected.version !== state.version) continue
    const row = lists[state.type].find(item => item.id === state.recordId)
    if (!row) { state.exists = false; state.version += 1; state.error = '当前列表已无原记录；原稿和当前稿仅供复制。'; continue }
    if (!isEntityRead(row, novelId, state.type, state.recordId) || !expected.clean
      || expected.revision !== state.revision || entityFormDirty(state)) continue
    const form = entityForm(state.type, row)
    state.draft = cloneEntityForm(form)
    state.baseline = form
    state.version += 1
  }
}

if (typeof window !== 'undefined') window.addEventListener('beforeunload', event => {
  if (![...new Set(records.values())].some(entityDraftDirty)) return
  event.preventDefault()
  event.returnValue = ''
})
