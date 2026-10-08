import { reactive, ref } from 'vue'
import type { Chapter } from '../types'
import type { GenerationConsistencyObservation } from './generationConsistency'

export interface ChapterGenerationState {
  title: string
  directive: string
  outlineNodeId: string
  previousChapterId: string
  latestTitle: string
  outlineOptions: { label: string; value: string }[]
  sourceLoaded: boolean
  needsSourceRefresh: boolean
  revision: number
  outlineRevision: number
  candidate: string
  outcome: 'idle' | 'generating' | 'failed' | 'unknown' | 'saved'
  error: string
  savedChapter: Chapter | null
  unsavedEdits: boolean
  pending: number | null
  ruleCheck: GenerationConsistencyObservation | null
}

// 本页面内保留卡片稿件和写入锁；完整重载前仍需作者复制。
const drafts = new Map<string, ChapterGenerationState>()
const registryVersion = ref(0)
let operationSequence = 0

export function chapterGenerationState(novelId: string, conversationId: string, messageId: string, sourceText: string) {
  const key = JSON.stringify([novelId, conversationId, messageId])
  let state = drafts.get(key)
  if (!state) {
    state = reactive<ChapterGenerationState>({
      title: '', directive: sourceText, outlineNodeId: '', previousChapterId: '',
      latestTitle: '暂无章节，从第一章开始', outlineOptions: [], sourceLoaded: false,
      needsSourceRefresh: false, revision: 0, outlineRevision: 0, candidate: '', outcome: 'idle', error: '',
      savedChapter: null, unsavedEdits: false, pending: null, ruleCheck: null,
    })
    drafts.set(key, state)
    registryVersion.value += 1
  }
  return state
}

export function findChapterGenerationState(novelId: string, conversationId: string, messageId: string) {
  // 仅读取已存在的记录；消息列表渲染不会为每条回复创建空稿件。
  registryVersion.value
  return drafts.get(JSON.stringify([novelId, conversationId, messageId]))
}

export function chapterGenerationOperation() { return ++operationSequence }
