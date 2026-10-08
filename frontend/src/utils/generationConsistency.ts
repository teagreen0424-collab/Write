import type { Conflict, GeneratedRuleCheck } from '../types'

export interface GenerationConsistencySource {
  novelId: string
  chapterId: string
  subject: 'saved_chapter' | 'preview_candidate'
  content: string
  allowExistingAction?: boolean
}
export interface GenerationConsistencyObservation {
  status: GeneratedRuleCheck['status'] | 'verifying' | 'unconfirmed'
  novelId: string
  chapterId: string
  subject: GeneratedRuleCheck['subject']
  content: string
  conflicts: Conflict[]
  warnings: string[]
}

export function createGenerationObservation(source: GenerationConsistencySource): GenerationConsistencyObservation {
  return { status: 'verifying', novelId: source.novelId, chapterId: source.chapterId,
    subject: source.subject, content: source.content, conflicts: [], warnings: [] }
}

// 后端范围是Unicode码点；仅展示时转换，API和改写选区仍保留码点合同。
export function codePointRange(text: string, start: unknown, end: unknown): { start: number; end: number } | null {
  if (typeof start !== 'number' || typeof end !== 'number' || !Number.isSafeInteger(start)
    || !Number.isSafeInteger(end) || start < 0 || end <= start) return null
  const points = Array.from(text)
  if (end > points.length) return null
  return { start: points.slice(0, start).join('').length, end: points.slice(0, end).join('').length }
}

export async function verifyGenerationConsistency(raw: unknown, source: GenerationConsistencySource): Promise<GenerationConsistencyObservation> {
  const result = { ...createGenerationObservation(source), status: 'unconfirmed' as GenerationConsistencyObservation['status'] }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return result
  const check = raw as GeneratedRuleCheck
  const existing = source.allowExistingAction && check.subject === 'existing_action' && check.status === 'skipped'
  if (!['completed', 'failed', 'skipped'].includes(check.status)
    || check.novel_id !== source.novelId || check.chapter_id !== source.chapterId
    || (check.subject !== source.subject && !existing) || (check.status === 'skipped' && !existing)
    || check.offset_unit !== 'unicode_codepoint' || typeof check.content_sha256 !== 'string'
    || !/^[0-9a-f]{64}$/.test(check.content_sha256)
    || !Array.isArray(check.conflicts) || !Array.isArray(check.warnings)
    || !check.warnings.every(value => typeof value === 'string')
    || (check.status !== 'completed' && check.conflicts.length !== 0)) return result
  if (!check.conflicts.every(value => value && typeof value === 'object'
    && ['foreshadowing', 'known_info', 'timeline'].includes(value.rule) && typeof value.message === 'string' && !!value.message
    && typeof value.entity_id === 'string' && !!value.entity_id && codePointRange(source.content, value.start, value.end))) return result
  // 完整复制实际检查范围，异步核验不能继续读取后来变化的metadata对象。
  const verified = { ...result, status: check.status, subject: check.subject,
    conflicts: check.conflicts.map(value => ({ rule: value.rule, message: value.message,
      start: value.start, end: value.end, entity_id: value.entity_id })), warnings: [...check.warnings] }
  const expectedHash = check.content_sha256
  try {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source.content))
    const actualHash = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
    return actualHash === expectedHash ? verified : result
  } catch { return result }
}

export function generationConsistencyMessage(check: GenerationConsistencyObservation): string {
  if (check.status === 'completed') return check.conflicts.length
    ? `规则检查完成，发现 ${check.conflicts.length} 处提示；仍需作者校对。`
    : '规则检查完成，未发现已列规则冲突；仍需作者校对。'
  if (check.status === 'skipped') return '这是已有章节动作，本次未重新进行规则检查；仍需作者校对。'
  if (check.status === 'verifying') return '正在核对规则检查范围；仍需作者校对。'
  return check.status === 'failed' ? '规则检查未完成；仍需作者校对，可按需手动检查。'
    : '规则检查结果与范围尚未确认；仍需作者校对，可按需手动检查。'
}
