import type { Chapter } from '../types'

export function countCharacters(text: string): number {
  return text.replace(/\s/g, '').length
}

export function chapterLabel(chapter: Chapter): string {
  const sequenceLabel = `第${chapter.seq}章`
  const title = chapter.title
    .trim()
    .replace(/^第\s*[0-9零〇一二三四五六七八九十百千万两]+\s*章\s*[:：—-]?\s*/, '')
    .trim()
  return title ? `${sequenceLabel} ${title}` : sequenceLabel
}
