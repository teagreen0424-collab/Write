import type { AutopilotInjectionReceipt } from '../types'

export interface AutopilotInjectionIntent {
  version: 1
  novelId: string
  taskId: string
  requestId: string
  text: string
  hash: string
  currentDraft: string
  currentDraftEdited: boolean
  createdAt: number
}

const PREFIX = 'lingmo:autopilot-injection:v1:'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const HASH = /^[0-9a-f]{64}$/
const keyFor = (novelId: string, requestId: string) => `${PREFIX}${encodeURIComponent(novelId)}:${requestId}`

export async function hashAutopilotDirective(text: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, '0')).join('')
}

export function isAutopilotInjectionReceipt(value: unknown, intent: AutopilotInjectionIntent): value is AutopilotInjectionReceipt {
  if (!value || typeof value !== 'object') return false
  const row = value as Record<string, unknown>
  return row.type === 'control' && row.action === 'inject' && row.novel_id === intent.novelId
    && row.task_id === intent.taskId && row.request_id === intent.requestId
    && row.directive === intent.text && row.directive_hash === intent.hash && HASH.test(intent.hash)
    && row.accepted === true && typeof row.receipt_replayed === 'boolean'
    && Number.isSafeInteger(row.event_id) && (row.event_id as number) > 0
    && typeof row.paused === 'boolean' && typeof row.pause_requested === 'boolean'
    && typeof row.message === 'string' && row.message.trim().length > 0
}

export function storeAutopilotInjection(intent: AutopilotInjectionIntent): boolean {
  try { localStorage.setItem(keyFor(intent.novelId, intent.requestId), JSON.stringify(intent)); return true }
  catch { return false }
}

export function removeAutopilotInjection(novelId: string, requestId: string): boolean {
  try { localStorage.removeItem(keyFor(novelId, requestId)); return true }
  catch { return false }
}

export function listAutopilotInjections(novelId: string): { records: AutopilotInjectionIntent[]; unavailable: boolean; invalid: number } {
  const records: AutopilotInjectionIntent[] = []
  let invalid = 0
  try {
    const prefix = `${PREFIX}${encodeURIComponent(novelId)}:`
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index)
      if (!key?.startsWith(prefix)) continue
      try {
        const row = JSON.parse(localStorage.getItem(key) ?? 'null') as AutopilotInjectionIntent | null
        if (!row || row.version !== 1 || row.novelId !== novelId || typeof row.taskId !== 'string' || !row.taskId
          || typeof row.requestId !== 'string' || !UUID.test(row.requestId) || key !== keyFor(novelId, row.requestId)
          || typeof row.text !== 'string' || !row.text.trim() || typeof row.hash !== 'string' || !HASH.test(row.hash)
          || typeof row.currentDraft !== 'string' || typeof row.currentDraftEdited !== 'boolean'
          || (!row.currentDraftEdited && row.currentDraft !== row.text)
          || !Number.isFinite(row.createdAt) || row.createdAt < 0) { invalid += 1; continue }
        records.push(row)
      } catch { invalid += 1 }
    }
    return { records: records.sort((a, b) => b.createdAt - a.createdAt), unavailable: false, invalid }
  } catch { return { records, unavailable: true, invalid } }
}
