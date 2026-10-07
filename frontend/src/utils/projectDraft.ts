import { effectScope, reactive, ref, watch } from 'vue'
import type { Novel } from '../types'

export interface ProjectFields { title: string; genre: string; summary: string }
export interface ProjectDraft {
  form: ProjectFields
  revision: number
  original: ProjectFields | null
  baseline: ProjectFields | null
  savedNovel: Novel | null
  phase: 'idle' | 'pending' | 'saved' | 'unknown' | 'rejected'
  pending: number | null
  checking: number | null
  error: string
  directory: Novel[] | null
  bindingId: string | null
  sourceExists: boolean | null
}

// 仅保留本页面运行期稿件及等待锁；完整刷新前需要作者复制。
let creation: ProjectDraft | null = null
const promotions = new Map<string, ProjectDraft>()
const version = ref(0)
let sequence = 0

export const projectFields = (value: ProjectFields): ProjectFields => ({
  title: value.title, genre: value.genre, summary: value.summary,
})

function draft(title = '') {
  const state = reactive<ProjectDraft>({
    form: { title, genre: '', summary: '' }, revision: 0, original: null,
    baseline: null, savedNovel: null, phase: 'idle', pending: null,
    checking: null, error: '', directory: null, bindingId: null, sourceExists: null,
  })
  // 独立于组件挂载：改回原值也算作者的新一次编辑。
  effectScope(true).run(() => watch(() => state.form, () => { state.revision += 1 }, { deep: true, flush: 'sync' }))
  return state
}

export function creationDraft() {
  if (!creation) { creation = draft(); version.value += 1 }
  return creation
}

export function freshCreationDraft() {
  creation = draft()
  version.value += 1
  return creation
}

export function promotionDraft(conversationId: string, title: string) {
  let state = promotions.get(conversationId)
  if (!state) { state = draft(title); promotions.set(conversationId, state); version.value += 1 }
  return state
}

export function projectOperation() { return ++sequence }

export function projectChanges(state: ProjectDraft): Partial<ProjectFields> {
  const baseline = state.baseline
  if (!baseline) return projectFields(state.form)
  const changed: Partial<ProjectFields> = {}
  for (const key of ['title', 'genre', 'summary'] as const) {
    if (state.form[key] !== baseline[key]) changed[key] = state.form[key]
  }
  return changed
}

export function projectDirty(state: ProjectDraft) {
  return state.pending !== null || state.phase === 'unknown' || (state.baseline
    ? Object.keys(projectChanges(state)).length > 0
    : Object.values(state.form).some(value => value.length > 0))
}

export function projectDraftsDirty() {
  version.value
  return (creation && projectDirty(creation)) || [...promotions.values()].some(projectDirty) || false
}

export function existingProjectDrafts() {
  version.value
  const rows: { kind: 'create' | 'promote'; conversationId: string; state: ProjectDraft }[] = []
  if (creation && (creation.original || projectDirty(creation))) rows.push({ kind: 'create', conversationId: '', state: creation })
  for (const [conversationId, state] of promotions) {
    if (state.original || projectDirty(state)) rows.push({ kind: 'promote', conversationId, state })
  }
  return rows
}

export function isSavedProject(value: unknown): value is Novel {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, unknown>
  return typeof row.id === 'string' && row.id.length > 0 &&
    ['title', 'genre', 'summary', 'style_anchor', 'created_at', 'updated_at'].every(key => typeof row[key] === 'string')
}

export function projectText(fields: ProjectFields) {
  return `书名：${fields.title}\n题材：${fields.genre}\n梗概：\n${fields.summary}`
}
