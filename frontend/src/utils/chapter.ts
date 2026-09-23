import type { Chapter } from '../types'

export function countCharacters(text: string): number {
  // Python 后端用 str.split() 去空白、len() 数 Unicode 字符；JS 的 \s 和 length
  // 分别会误判部分空白字符、把 emoji/扩展汉字计为 2，需显式对齐。
  return Array.from(text.replace(/[\p{White_Space}\u001c-\u001f]/gu, '')).length
}

export function chapterLabel(chapter: Chapter): string {
  const sequenceLabel = `第${chapter.seq}章`
  const title = chapter.title
    .trim()
    .replace(/^第\s*[0-9零〇一二三四五六七八九十百千万两]+\s*章\s*[:：—-]?\s*/, '')
    .trim()
  return title ? `${sequenceLabel} ${title}` : sequenceLabel
}
