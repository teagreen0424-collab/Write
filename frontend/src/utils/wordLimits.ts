import type { WordLimits } from '../types'

/** 目标范围仅用于本次检查或任务，不替代作品设定。 */
export function wordLimitError(minWords: number | null, maxWords: number | null): string {
  if ([minWords, maxWords].some((value) => value !== null && (!Number.isSafeInteger(value) || value < 1 || value > 1_000_000))) {
    return '字数目标必须是 1 至 1000000 的整数'
  }
  if (minWords !== null && maxWords !== null && minWords > maxWords) return '最低字数不能高于最高字数'
  return ''
}

export function wordLimits(minWords: number | null, maxWords: number | null): WordLimits | null {
  return minWords === null && maxWords === null ? null : { min_words: minWords, max_words: maxWords }
}

export function wordLimitLabel(limits: WordLimits | null): string {
  if (!limits) return '未设置字数范围'
  if (limits.min_words !== null && limits.max_words !== null) return `${limits.min_words}–${limits.max_words} 字`
  if (limits.min_words !== null) return `至少 ${limits.min_words} 字`
  return `至多 ${limits.max_words} 字`
}
