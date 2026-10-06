<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NCheckbox,
  NEmpty,
  NInput,
  NInputNumber,
  NList,
  NListItem,
  NModal,
  NSelect,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, streamSSE } from '../api'
import { chapterLabel, countCharacters } from '../utils/chapter'
import { wordLimitError, wordLimitLabel, wordLimits } from '../utils/wordLimits'
import ChapterRevisionPanel from './ChapterRevisionPanel.vue'
import type {
  Chapter,
  ChapterRepairPreview,
  ClicheHit,
  ClicheResult,
  Conflict,
  DeepCheckConflict,
  OutlineCheckResult,
  OutlineNode,
  Segment,
  StoryCheckResult,
  StoryCheckIssue,
  Suggestion,
  WordLimits,
} from '../types'

const props = defineProps<{ novelId: string; initialChapterId?: string | null }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const chapters = ref<Chapter[]>([])
const selectedId = ref<string | null>(null)
const title = ref('')
const directive = ref('')
const content = ref('')
const contentRef = ref<HTMLTextAreaElement | null>(null)
const savedSnapshot = ref({ title: '', directive: '', content: '' })

const streaming = ref(false)
const streamText = ref('')
const streamError = ref('')
const streamCompleted = ref(false)
const streamUnconfirmed = ref(false)
const incompleteCollapsed = ref(false)
const copyFallback = ref(false)
const incompleteTextRef = ref<HTMLTextAreaElement | null>(null)
let streamRunId = 0
let unmounted = false
const conflicts = ref<Conflict[]>([])
const rewriteIntent = ref('去AI味')

// 去 AI 味扫描与语义深度检查
const clicheHits = ref<ClicheHit[]>([])
const cliche = ref<ClicheResult | null>(null)
const deepConflicts = ref<DeepCheckConflict[]>([])
const deepMeta = ref<{
  chunks_checked: number
  chunks_skipped: number
  truncated: boolean
} | null>(null)
const deepLoading = ref(false)
const outlineCheckResult = ref<OutlineCheckResult | null>(null)
const outlineCheckLoading = ref(false)
const storyCheckResult = ref<StoryCheckResult | null>(null)
const storyCheckLoading = ref(false)
const storyMinWords = ref<number | null>(null)
const storyMaxWords = ref<number | null>(null)
const storyWordLimitError = computed(() => wordLimitError(storyMinWords.value, storyMaxWords.value))
let storyCheckRequestId = 0

// 改写预览（不落库，确认后才应用）
const showPreview = ref(false)
const previewPassage = ref('')
const previewNew = ref('')
const previewFullContent = ref('')

// 整章修订与选段改写各自保留预览；仅明确确认才覆盖正文。
interface RepairContext {
  novelId: string
  chapterId: string
  epoch: number
  title: string
  directive: string
  content: string
  wordLimits: WordLimits | null
  issues: StoryCheckIssue[]
  unreviewedIds: string[]
}
const showRepair = ref(false)
const repairInstructions = ref('')
const repairContext = ref<RepairContext | null>(null)
const repairPreview = ref<ChapterRepairPreview | null>(null)
const repairLoading = ref(false)
const repairApplying = ref(false)
const repairError = ref('')
const repairRefreshNeeded = ref(false)
let repairEpoch = 0
let repairGenerationId = 0
let repairApplyRequestId = 0
let repairInternalUpdate = false

const showSettle = ref(false)
const showSummary = ref(false)
const summaryDraft = ref('')
const savedSummary = ref('')
const summarySaving = ref(false)
const showDiscard = ref(false)
const discardPrompt = ref('')
const discardConfirmText = ref('放弃修改')
let resolveDiscard: ((confirmed: boolean) => void) | null = null
const showDelete = ref(false)
const showWriteNext = ref(false)
const showRevisions = ref(false)
const deleting = ref(false)
const nextTitle = ref('')
const nextDirective = ref('')
const nextOutlineNodes = ref<OutlineNode[]>([])
const nextOutlineNodeId = ref<string | null>(null)
const settleSummary = ref('')
const suggestions = ref<Suggestion[]>([])
const checkedSuggestion = ref<boolean[]>([])

const REWRITE_INTENT_OPTIONS = [
  { label: '更细腻', value: '更细腻' },
  { label: '更爽', value: '更爽' },
  { label: '去 AI 味', value: '去AI味' },
  { label: '压缩', value: '压缩' },
]

const OUTLINE_ISSUE_LABEL = {
  missing: '遗漏',
  changed: '替换',
  contradiction: '矛盾',
}

const STORY_ISSUE_LABEL = {
  directive: '章前指令',
  continuity: '前章承接',
  setting: '设定',
  outline: '大纲',
}
const STORY_SEVERITY_LABEL = { high: '严重', medium: '中等', low: '轻微' }
const STORY_REQUIREMENT_STATUS = { violation: '偏差', satisfied: '满足', uncertain: '待核对' }
const storyRequirementRows = computed(() => (storyCheckResult.value?.requirements || []).map((requirement) => {
  const matches = (storyCheckResult.value?.requirement_checks || []).filter((check) => check.requirement_id === requirement.id)
  const uniqueRequirement = storyCheckResult.value?.requirements.filter((item) => item.id === requirement.id).length === 1
  const check = uniqueRequirement && matches.length === 1 && !storyCheckResult.value?.unreviewed_requirement_ids?.includes(requirement.id) ? matches[0] : undefined
  return { ...requirement, status: check?.status || 'uncertain', excerpt: check?.excerpt || '', reason: check?.reason || '本项未得到完整核对，请重试或人工核对。' }
}))
const storyReviewIncomplete = computed(() => !storyCheckResult.value?.review_complete
  || !Array.isArray(storyCheckResult.value?.requirements)
  || !Array.isArray(storyCheckResult.value?.requirement_checks)
  || !Array.isArray(storyCheckResult.value?.unreviewed_requirement_ids)
  || !!storyCheckResult.value?.unreviewed_requirement_ids?.length
  || (storyCheckResult.value?.requirement_checks || []).some((check) => !storyCheckResult.value?.requirements?.some((requirement) => requirement.id === check.requirement_id))
  || storyRequirementRows.value.some((row) => row.status === 'uncertain'))
const storyCheckEmptyText = computed(() => {
  if (storyReviewIncomplete.value) return '剧情检查未完成，不能判断是否存在偏差；请重试并人工核对'
  if (storyCheckResult.value?.truncated) return '已检查范围内未发现明显问题，正文或依据有截断；仍需人工校对'
  if (storyRequirementRows.value.some((row) => row.status === 'violation')) return '构思逐项核对发现偏差，请核对正文'
  return '未发现明显剧情问题，仍需人工校对'
})

const selectedChapter = computed(() => chapters.value.find((c) => c.id === selectedId.value) ?? null)
const isLatestChapter = computed(
  () => !!selectedId.value && chapters.value[chapters.value.length - 1]?.id === selectedId.value,
)
const nextOutlineOptions = computed(() => nextOutlineNodes.value.map((node) => ({
  label: node.title,
  value: node.id,
})))
const selectedNextOutline = computed(
  () => nextOutlineNodes.value.find((node) => node.id === nextOutlineNodeId.value),
)
const chapterWords = computed(() => countCharacters(content.value))
const totalWords = computed(() =>
  chapters.value.reduce((total, chapter) => {
    const chapterContent = chapter.id === selectedId.value ? content.value : chapter.content
    return total + countCharacters(chapterContent)
  }, 0),
)
const isDirty = computed(
  () =>
    !!selectedId.value &&
    (title.value !== savedSnapshot.value.title ||
      directive.value !== savedSnapshot.value.directive ||
      content.value !== savedSnapshot.value.content),
)
const isSummaryDirty = computed(() => showSummary.value && summaryDraft.value !== savedSummary.value)
const hasIncompleteOutput = computed(() => !!streamError.value)
const hasUnsavedChanges = computed(
  () => isDirty.value || isSummaryDirty.value || streaming.value || hasIncompleteOutput.value,
)

function rememberSaved() {
  savedSnapshot.value = {
    title: title.value,
    directive: directive.value,
    content: content.value,
  }
}

function askDiscard(prompt: string, positiveText = '放弃修改'): Promise<boolean> {
  if (resolveDiscard) return Promise.resolve(false)
  discardPrompt.value = prompt
  discardConfirmText.value = positiveText
  showDiscard.value = true
  return new Promise((resolve) => { resolveDiscard = resolve })
}

function finishDiscard(confirmed: boolean) {
  showDiscard.value = false
  const resolve = resolveDiscard
  resolveDiscard = null
  resolve?.(confirmed)
}

async function confirmDiscard(): Promise<boolean> {
  return !hasUnsavedChanges.value || await askDiscard('当前章节有未保存修改或未完成输出，确定放弃并离开吗？')
}

function clearStreamOutput() {
  streamText.value = ''
  streamError.value = ''
  streamCompleted.value = false
  streamUnconfirmed.value = false
  incompleteCollapsed.value = false
  copyFallback.value = false
}

function abandonStreamOutput() {
  streamRunId += 1
  streaming.value = false
  clearStreamOutput()
}

async function beginStream(): Promise<number | null> {
  if (streaming.value || repairApplying.value) return null
  if (hasIncompleteOutput.value && !await askDiscard('上次生成的输出尚未处理，新一轮生成会放弃这些输出，确定继续吗？', '放弃旧稿并生成')) return null
  clearStreamOutput()
  showPreview.value = false
  previewNew.value = ''
  previewFullContent.value = ''
  streaming.value = true
  return ++streamRunId
}

function isCurrentStream(runId: number) {
  return !unmounted && runId === streamRunId
}

function failStream(runId: number, error: string, code?: string) {
  if (!isCurrentStream(runId)) return
  streaming.value = false
  streamError.value = error || '生成中断，未收到完成确认'
  streamCompleted.value = code === 'CLIENT_UPDATE_FAILED'
  streamUnconfirmed.value = code === 'STREAM_INCOMPLETE'
  incompleteCollapsed.value = false
  showPreview.value = false
  previewNew.value = ''
  previewFullContent.value = ''
  message.error(streamCompleted.value ? '生成已完成，但页面同步失败，请刷新后核对'
    : streamUnconfirmed.value ? '未收到完成确认，请保留输出并核对保存结果' : '生成未完成，已收到的输出尚未保存')
}

async function copyIncompleteOutput() {
  if (!streamText.value) return
  try {
    await navigator.clipboard.writeText(streamText.value)
    message.success('已复制未完成输出')
  } catch {
    copyFallback.value = true
    incompleteCollapsed.value = false
    await nextTick()
    incompleteTextRef.value?.focus()
    incompleteTextRef.value?.select()
  }
}

async function collapseIncompleteOutput() {
  if (await askDiscard('这些输出尚未处理。收起后仍会保留，可再次展开，确定收起吗？', '确认收起')) {
    incompleteCollapsed.value = true
  }
}

async function discardIncompleteOutput() {
  if (await askDiscard('确定放弃这些输出吗？放弃后无法恢复，请先复制需要保留的内容。', '放弃输出')) {
    abandonStreamOutput()
  }
}

function requireSaved(action: string): boolean {
  if (!isDirty.value) return true
  message.warning(`请先保存当前修改，再${action}`)
  return false
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!hasUnsavedChanges.value) return
  event.preventDefault()
  event.returnValue = ''
}

const segments = computed<Segment[]>(() => {
  const text = content.value
  if (!text) return [{ text, kind: 'plain' }]
  const marks: { start: number; end: number; kind: 'conflict' | 'cliche' }[] = []
  for (const c of conflicts.value) marks.push({ start: c.start, end: c.end, kind: 'conflict' })
  for (const h of clicheHits.value) marks.push({ start: h.start, end: h.end, kind: 'cliche' })
  if (!marks.length) return [{ text, kind: 'plain' }]
  marks.sort((a, b) => {
    if (a.start !== b.start) return a.start - b.start
    if (a.kind !== b.kind) return a.kind === 'conflict' ? -1 : 1
    return b.end - a.end
  })
  const out: Segment[] = []
  let pos = 0
  for (const m of marks) {
    if (m.start < pos) continue // 与上一标记重叠，冲突优先
    if (m.start > pos) out.push({ text: text.slice(pos, m.start), kind: 'plain' })
    out.push({ text: text.slice(m.start, m.end), kind: m.kind })
    pos = m.end
  }
  if (pos < text.length) out.push({ text: text.slice(pos), kind: 'plain' })
  return out
})

// 四类分布展示用（顺序固定）
const CLICHE_CATEGORIES = ['万能副词', '套路动作', '面部套话', '心理填充']

async function loadChapters() {
  if (!props.novelId) return
  const novelId = props.novelId
  const loaded = await api.listChapters(novelId)
  if (unmounted || props.novelId !== novelId) return
  chapters.value = loaded
  if (props.initialChapterId && !selectedId.value && !streaming.value) {
    const initial = chapters.value.find((chapter) => chapter.id === props.initialChapterId)
    if (initial) selectChapter(initial, true)
  }
}

function clearMarks() {
  conflicts.value = []
  clicheHits.value = []
  cliche.value = null
  deepConflicts.value = []
  deepMeta.value = null
  outlineCheckResult.value = null
  invalidateStoryCheck()
}

function invalidateStoryCheck() {
  storyCheckRequestId += 1
  storyCheckResult.value = null
  storyCheckLoading.value = false
}

async function selectChapter(c: Chapter, force = false) {
  if (!force && repairApplying.value) { message.warning('正在保存修订，请等待保存回应'); return }
  if (!force && c.id === selectedId.value) return
  if (!force && !await confirmDiscard()) return
  abandonStreamOutput()
  if (c.id !== selectedId.value) {
    showRevisions.value = false
    showSummary.value = false
  }
  selectedId.value = c.id
  title.value = c.title
  content.value = c.content
  directive.value = c.directive
  rememberSaved()
  clearMarks()
}

async function startNew(force = false) {
  if (!force && repairApplying.value) { message.warning('正在保存修订，请等待保存回应'); return }
  if (!force && !await confirmDiscard()) return
  abandonStreamOutput()
  showRevisions.value = false
  showSummary.value = false
  selectedId.value = null
  title.value = ''
  directive.value = ''
  content.value = ''
  rememberSaved()
  clearMarks()
}

async function reloadAndReselect(chapter?: Chapter, runId?: number) {
  await loadChapters()
  if (runId !== undefined && !isCurrentStream(runId)) return
  const c = chapter ?? chapters.value.find((x) => x.id === selectedId.value)
  if (c) {
    selectedId.value = c.id
    content.value = c.content
    title.value = c.title
    directive.value = c.directive
    // 恢复后清空一致性/套路标记，并把内容视为已保存
    rememberSaved()
    clearMarks()
  }
}

// 恢复历史版本：后端已把恢复前状态另存为快照，这里只需重载并清空标记
async function onRestored(chapter: Chapter) {
  await reloadAndReselect(chapter)
}

function openRevisionHistory() {
  if (!selectedId.value || !requireSaved('查看版本历史')) return
  showRevisions.value = true
}

async function generateChapter(
  previousChapterId = '',
  chapterTitle = title.value,
  chapterDirective = directive.value,
  outlineNodeId = '',
) {
  const runId = await beginStream()
  if (runId === null) return
  clearMarks()
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/write`,
    {
      title: chapterTitle,
      directive: chapterDirective,
      previous_chapter_id: previousChapterId,
      outline_node_id: outlineNodeId,
    },
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: async () => {
        if (!isCurrentStream(runId)) return
        await loadChapters()
        if (!isCurrentStream(runId)) return
        const last = chapters.value[chapters.value.length - 1]
        if (last) await selectChapter(last, true)
        clearStreamOutput()
        streaming.value = false
        message.success('已生成新章')
      },
      onError: (m, code) => failStream(runId, m, code),
    },
  )
}

async function doWrite() {
  await generateChapter()
}

async function doWriteNext() {
  if (!selectedId.value || streaming.value || !requireSaved('生成下一章')) return
  if (!isLatestChapter.value) {
    message.warning('只能从最后一章继续生成，请先选择章节列表中的最后一章')
    return
  }
  nextTitle.value = ''
  nextDirective.value = ''
  try {
    nextOutlineNodes.value = (await api.listOutline(props.novelId)).filter(
      (node) => node.level === 'chapter',
    )
  } catch (error) {
    message.error((error as Error).message)
    return
  }
  const currentIndex = nextOutlineNodes.value.findIndex(
    (node) => node.id === selectedChapter.value?.outline_node_id,
  )
  const nextIndex = currentIndex >= 0 ? currentIndex + 1 : chapters.value.length
  nextOutlineNodeId.value = nextOutlineNodes.value[nextIndex]?.id ?? null
  showWriteNext.value = true
}

async function confirmWriteNext() {
  if (!selectedId.value || streaming.value) return
  showWriteNext.value = false
  await generateChapter(
    selectedId.value,
    nextTitle.value,
    nextDirective.value,
    nextOutlineNodeId.value ?? '',
  )
}

async function doContinue() {
  if (!selectedId.value || streaming.value || !requireSaved('续写本章')) return
  const runId = await beginStream()
  if (runId === null) return
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/continue`,
    { directive: directive.value },
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: async () => {
        if (!isCurrentStream(runId)) return
        await reloadAndReselect(undefined, runId)
        if (!isCurrentStream(runId)) return
        clearStreamOutput()
        streaming.value = false
        message.success('已续写')
      },
      onError: (m, code) => failStream(runId, m, code),
    },
  )
}

function getSelection(): string {
  const el = contentRef.value
  if (!el) return ''
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  return end > start ? content.value.slice(start, end) : ''
}

async function doRewrite() {
  if (!selectedId.value || streaming.value || !requireSaved('改写选中内容')) return
  const passage = getSelection().trim()
  if (!passage) {
    message.error('请先在正文中选中要改写的段落')
    return
  }
  const runId = await beginStream()
  if (runId === null) return
  previewPassage.value = passage
  previewNew.value = ''
  previewFullContent.value = ''
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/rewrite`,
    { passage, intent: rewriteIntent.value, preview: true },
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: (obj) => {
        if (!isCurrentStream(runId)) return
        streaming.value = false
        // 流式内容即改写后的段落；done 事件带拼好的整章内容，供「应用」落库
        previewNew.value = streamText.value
        previewFullContent.value = obj.content ?? ''
        clearStreamOutput()
        showPreview.value = true
      },
      onError: (m, code) => failStream(runId, m, code),
    },
  )
}

// 应用改写：仅此时才落库，来源记为 rewrite_apply
async function applyPreview() {
  if (!selectedId.value || streaming.value || hasIncompleteOutput.value || !previewFullContent.value.trim()) return
  try {
    await api.updateChapter(props.novelId, selectedId.value, {
      content: previewFullContent.value,
      revision_source: 'rewrite_apply',
    })
    content.value = previewFullContent.value
    rememberSaved()
    showPreview.value = false
    await loadChapters()
    message.success('已应用改写')
  } catch (e) {
    message.error((e as Error).message)
  }
}

function discardPreview() {
  showPreview.value = false
  previewPassage.value = ''
  previewNew.value = ''
  previewFullContent.value = ''
  message.info('已放弃本次改写')
}

function invalidateRepair() {
  repairEpoch += 1
  repairGenerationId += 1
  repairLoading.value = false
  repairPreview.value = null
  repairContext.value = null
  repairError.value = ''
  repairRefreshNeeded.value = false
  showRepair.value = false
}

function isCurrentRepair(context: RepairContext) {
  return !unmounted && repairEpoch === context.epoch
    && props.novelId === context.novelId && selectedId.value === context.chapterId
    && title.value === context.title && directive.value === context.directive
    && content.value === context.content && !isDirty.value
}

function openRepair() {
  if (!selectedId.value || repairApplying.value || streaming.value || hasIncompleteOutput.value || storyCheckLoading.value
    || !content.value.trim() || !requireSaved('生成修订稿')) return
  if (storyWordLimitError.value) { message.error(storyWordLimitError.value); return }
  invalidateRepair()
  repairInstructions.value = ''
  const limits = wordLimits(storyMinWords.value, storyMaxWords.value)
  repairContext.value = {
    novelId: props.novelId,
    chapterId: selectedId.value,
    epoch: repairEpoch,
    title: title.value,
    directive: directive.value,
    content: content.value,
    wordLimits: limits ? { ...limits } : null,
    issues: (storyCheckResult.value?.issues || []).map((issue) => ({ ...issue })),
    unreviewedIds: [...new Set([
      ...(storyCheckResult.value?.unreviewed_requirement_ids || []),
      ...storyRequirementRows.value.filter((row) => row.status === 'uncertain').map((row) => row.id),
    ])],
  }
  showRepair.value = true
}

function handleRepairVisibility(show: boolean) {
  // 确认请求已经可能写入，等待回应期间保留窗口，避免误以为关闭等于取消保存。
  if (repairApplying.value) return
  if (!show) invalidateRepair()
}

function discardRepair() {
  if (repairApplying.value) return
  invalidateRepair()
  message.info('已放弃修订预览，正文未修改')
}

async function generateRepair() {
  const context = repairContext.value
  if (!context || !showRepair.value || repairLoading.value || repairApplying.value || repairRefreshNeeded.value || !isCurrentRepair(context)) return
  const requestId = ++repairGenerationId
  const instructions = repairInstructions.value
  const isCurrent = () => showRepair.value && isCurrentRepair(context)
    && requestId === repairGenerationId && repairInstructions.value === instructions
  repairLoading.value = true
  repairPreview.value = null
  repairError.value = ''
  try {
    const preview = await api.previewChapterRepair(context.novelId, context.chapterId, {
      instructions,
      word_limits: context.wordLimits,
      issues: context.issues,
      unreviewed_requirement_ids: context.unreviewedIds,
    })
    if (!isCurrent()) return
    if (preview.chapter_id !== context.chapterId || preview.original_content !== context.content
      || preview.title !== context.title || preview.directive !== context.directive || !preview.base_fingerprint
      || !preview.content?.trim()) {
      repairError.value = '修订依据与当前已保存章节不一致，请刷新章节后重新生成。正文未修改。'
      repairRefreshNeeded.value = true
      return
    }
    repairPreview.value = preview
  } catch (e) {
    if (isCurrent()) repairError.value = `修订稿生成未完成，正文未修改：${(e as Error).message}`
  } finally {
    if (isCurrent()) repairLoading.value = false
  }
}

async function refreshRepairChapter() {
  const context = repairContext.value
  if (!context || repairApplying.value || !isCurrentRepair(context)) return
  try {
    await loadChapters()
    if (!isCurrentRepair(context)) return
    const chapter = chapters.value.find((item) => item.id === context.chapterId)
    if (!chapter) { invalidateRepair(); message.warning('原章节已不存在，请核对章节列表'); return }
    await selectChapter(chapter, true)
    invalidateRepair()
    message.info('已刷新章节，请核对正文并重新检查、生成修订稿')
  } catch (e) {
    if (isCurrentRepair(context)) repairError.value = `章节刷新未完成：${(e as Error).message}`
  }
}

async function applyRepair() {
  const context = repairContext.value
  const preview = repairPreview.value
  if (!context || !preview || !showRepair.value || repairApplying.value || repairLoading.value
    || !preview.content.trim() || preview.content === preview.original_content || !isCurrentRepair(context)) return
  const requestId = ++repairApplyRequestId
  const generationId = repairGenerationId
  const isCurrent = () => isCurrentRepair(context) && requestId === repairApplyRequestId && generationId === repairGenerationId
  repairApplying.value = true
  repairError.value = ''
  let saved = false
  try {
    const updated = await api.applyChapterRepair(context.novelId, context.chapterId, preview.base_fingerprint, preview.content)
    saved = true
    if (!isCurrent()) return
    if (updated.id !== context.chapterId || updated.novel_id !== context.novelId) {
      repairError.value = '保存响应与原章节不一致，请刷新原章节核对保存结果。当前编辑稿未替换。'
      return
    }
    // 使用保存回应更新同一章，不调用可能跨章覆盖编辑稿的普通保存函数。
    repairInternalUpdate = true
    try {
      chapters.value = chapters.value.map((chapter) => chapter.id === context.chapterId ? updated : chapter)
      title.value = updated.title
      directive.value = updated.directive
      content.value = updated.content
      storyMinWords.value = context.wordLimits?.min_words ?? null
      storyMaxWords.value = context.wordLimits?.max_words ?? null
      rememberSaved()
      clearMarks()
    } finally { repairInternalUpdate = false }
    invalidateRepair()
    repairApplying.value = false
    message.success('修订稿已保存，旧版本已保留，正在复检')
    const checked = await doStoryCheck()
    if (!checked && !unmounted && props.novelId === context.novelId && selectedId.value === context.chapterId
      && content.value === updated.content && directive.value === updated.directive) {
      message.warning('修订稿已保存，旧版本已保留；剧情复检未完成，请重试或人工核对。')
    }
  } catch (e) {
    if (!isCurrent()) return
    if (saved) {
      repairError.value = `修订稿已保存，但页面同步或复检未完成，请刷新原章节核对：${(e as Error).message}`
    } else {
      const status = (e as Error & { status?: number }).status
      repairError.value = status === 409 || status === 410
        ? `章节或修订依据已改变，本次未保存修订稿。请刷新章节并重新检查、生成：${(e as Error).message}`
        : `修订保存未得到成功确认，请保留候选稿并核对原章节：${(e as Error).message}`
      if (status === 409 || status === 410) {
        repairPreview.value = null
        repairRefreshNeeded.value = true
        await loadChapters().catch(() => {})
      }
    }
  } finally {
    if (requestId === repairApplyRequestId) repairApplying.value = false
  }
}

// 扫描套路表达，命中与冲突共用高亮
async function doScanCliche() {
  if (!content.value.trim()) return
  try {
    const res = await api.scanCliche(props.novelId, content.value)
    cliche.value = res
    clicheHits.value = res.hits
    if (res.total) message.warning(`命中 ${res.total} 处套路表达，密度 ${res.density}/千字`)
    else message.success('未命中套路表达')
  } catch (e) {
    message.error((e as Error).message)
  }
}

// 语义深度检查（每个有效分块一次模型调用，最多 8 次，结果不落库）
async function doDeepCheck() {
  if (!content.value.trim()) return
  deepLoading.value = true
  try {
    const res = await api.checkConsistencyDeep(props.novelId, content.value)
    deepConflicts.value = res.conflicts
    deepMeta.value = {
      chunks_checked: res.chunks_checked,
      chunks_skipped: res.chunks_skipped,
      truncated: res.truncated,
    }
    if (res.conflicts.length) message.warning(`语义层发现 ${res.conflicts.length} 处冲突`)
    else message.success('语义层未发现冲突')
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deepLoading.value = false
  }
}

// 按需检查当前编辑稿与绑定大纲的契合度；不保存、不修改正文。
async function doOutlineCheck() {
  if (outlineCheckLoading.value || !selectedId.value || !selectedChapter.value?.outline_node_id || !content.value.trim()) return
  const chapterId = selectedId.value
  const checkedContent = content.value
  outlineCheckLoading.value = true
  outlineCheckResult.value = null
  try {
    const result = await api.checkChapterOutline(props.novelId, chapterId, checkedContent)
    if (selectedId.value !== chapterId || content.value !== checkedContent) return
    outlineCheckResult.value = result
    if (result.issues.length) message.warning(`大纲契合检查发现 ${result.issues.length} 处待核对项`)
    else message.success('未发现明显大纲偏差，仍请人工校对')
  } catch (e) {
    if (selectedId.value === chapterId && content.value === checkedContent) {
      message.error((e as Error).message)
    }
  } finally {
    outlineCheckLoading.value = false
  }
}

// 检查当前编辑稿，显式传空章前指令也能覆盖已保存指令；检查不会修改正文。
async function doStoryCheck() {
  if (storyCheckLoading.value || !selectedId.value || !content.value.trim() || streaming.value) return false
  if (storyWordLimitError.value) { message.error(storyWordLimitError.value); return false }
  const requestId = ++storyCheckRequestId
  const novelId = props.novelId
  const chapterId = selectedId.value
  const checkedContent = content.value
  const checkedDirective = directive.value
  const checkedWordLimits = wordLimits(storyMinWords.value, storyMaxWords.value)
  const isCurrent = () => !unmounted && requestId === storyCheckRequestId
    && props.novelId === novelId && selectedId.value === chapterId
    && content.value === checkedContent && directive.value === checkedDirective
  storyCheckLoading.value = true
  storyCheckResult.value = null
  try {
    const result = checkedWordLimits
      ? await api.checkChapterStory(novelId, chapterId, checkedContent, checkedDirective, checkedWordLimits)
      : await api.checkChapterStory(novelId, chapterId, checkedContent, checkedDirective)
    if (!isCurrent()) return false
    storyCheckResult.value = result
    if (storyReviewIncomplete.value || result.truncated) message.warning('剧情检查范围或结果不完整，请核对提示后人工校对')
    else if (result.word_count?.within_limits === false) message.warning('正文未达到字数目标，请核对字数提示')
    else if (result.issues.length || storyRequirementRows.value.some((row) => row.status === 'violation')) message.warning('剧情检查发现待核对的偏差，请核对逐项结果')
    else message.success('未发现明显剧情问题，仍请人工校对')
    return true
  } catch (e) {
    if (isCurrent()) message.error(`剧情检查未完成：${(e as Error).message}`)
    return false
  } finally {
    if (isCurrent()) storyCheckLoading.value = false
  }
}

// 点击深度检查结果，滚动定位到正文片段
function locateExcerpt(excerpt: string) {
  if (!excerpt) return
  const idx = content.value.indexOf(excerpt)
  const el = contentRef.value
  if (idx === -1) {
    message.warning('正文中未找到该片段')
    return
  }
  if (!el) return
  el.focus()
  el.setSelectionRange(idx, idx + excerpt.length)
  const ratio = idx / Math.max(content.value.length, 1)
  el.scrollTop = ratio * el.scrollHeight
}

async function saveContent() {
  if (!selectedId.value || repairApplying.value) return
  try {
    const updated = await api.updateChapter(props.novelId, selectedId.value, {
      title: title.value,
      content: content.value,
      directive: directive.value,
      revision_source: 'manual_save',
    })
    title.value = updated.title
    directive.value = updated.directive
    content.value = updated.content
    rememberSaved()
    message.success('已保存')
    await loadChapters()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function openSummary() {
  if (!selectedChapter.value || summarySaving.value || streaming.value) return
  summaryDraft.value = selectedChapter.value.summary || ''
  savedSummary.value = summaryDraft.value
  showSummary.value = true
}

async function closeSummary() {
  if (summarySaving.value) return
  if (isSummaryDirty.value && !await askDiscard('摘要有未保存修改，确定放弃并关闭吗？')) return
  showSummary.value = false
}

async function saveSummary() {
  if (!selectedId.value || summarySaving.value || !isSummaryDirty.value) return
  const novelId = props.novelId
  const chapterId = selectedId.value
  const submittedSummary = summaryDraft.value
  summarySaving.value = true
  try {
    const updated = await api.updateChapter(novelId, chapterId, { summary: submittedSummary })
    if (props.novelId !== novelId || selectedId.value !== chapterId || !showSummary.value) return
    const chapter = chapters.value.find((c) => c.id === chapterId)
    if (chapter) {
      chapter.summary = updated.summary
      chapter.updated_at = updated.updated_at
    }
    // 只同步摘要，保留编辑区尚未保存的正文、标题和章前指令。
    summaryDraft.value = updated.summary
    savedSummary.value = updated.summary
    message.success('摘要已保存')
  } catch (e) {
    if (props.novelId === novelId && selectedId.value === chapterId && showSummary.value) {
      message.error((e as Error).message)
    }
  } finally {
    summarySaving.value = false
  }
}

async function deleteChapter() {
  if (!selectedId.value || deleting.value || streaming.value) return
  const deletedIndex = chapters.value.findIndex((chapter) => chapter.id === selectedId.value)
  deleting.value = true
  try {
    await api.deleteChapter(props.novelId, selectedId.value)
    await loadChapters()
    const next = chapters.value[Math.min(deletedIndex, chapters.value.length - 1)]
    if (next) selectChapter(next, true)
    else startNew(true)
    showDelete.value = false
    message.success('章节已删除')
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deleting.value = false
  }
}

async function doConsistency() {
  if (!content.value.trim()) return
  try {
    const res = await api.checkConsistency(props.novelId, content.value)
    conflicts.value = res.conflicts
    if (res.conflicts.length) message.warning(`发现 ${res.conflicts.length} 处冲突`)
    else message.success('未发现冲突')
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function doSettle() {
  if (streaming.value || hasIncompleteOutput.value) {
    message.warning('请先处理未完成输出，再定稿沉淀')
    return
  }
  if (!selectedId.value || !requireSaved('定稿沉淀')) return
  try {
    const res = await api.settleChapter(props.novelId, selectedId.value)
    settleSummary.value = res.summary
    suggestions.value = res.suggestions
    checkedSuggestion.value = res.suggestions.map(() => true)
    showSettle.value = true
    await reloadAndReselect()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function confirmSettle() {
  if (!selectedId.value) return
  const picked = suggestions.value.filter((_, i) => checkedSuggestion.value[i])
  try {
    const res = await api.settleApply(props.novelId, selectedId.value, picked)
    message.success(`已落库 ${res.created.length} 条设定变更`)
    showSettle.value = false
  } catch (e) {
    message.error((e as Error).message)
  }
}

function suggestionLabel(s: Suggestion): string {
  const labels: Record<string, string> = {
    character: '人物',
    timeline_event: '事件',
    foreshadowing: '伏笔',
  }
  const name =
    (s.data.name as string) || (s.data.title as string) || (s.data.content as string) || ''
  return `[${labels[s.type] ?? s.type}] ${name}`
}

async function doExport(kind: 'markdown' | 'txt') {
  try {
    const text = kind === 'markdown' ? await api.exportMarkdown(props.novelId) : await api.exportTxt(props.novelId)
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = kind === 'markdown' ? 'novel.md' : 'novel.txt'
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    message.error((e as Error).message)
  }
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) {
      startNew(true)
      loadChapters()
    }
  },
  { immediate: true },
)

watch(hasUnsavedChanges, (dirty) => emit('dirty-change', dirty), { immediate: true })
watch(content, () => { outlineCheckResult.value = null })
watch(
  () => [props.novelId, selectedId.value, content.value, directive.value, storyMinWords.value, storyMaxWords.value],
  invalidateStoryCheck,
  { flush: 'sync' },
)
watch(
  () => [props.novelId, selectedId.value, title.value, content.value, directive.value, storyMinWords.value, storyMaxWords.value, streaming.value],
  () => { if (!repairInternalUpdate) invalidateRepair() },
  { flush: 'sync' },
)
watch(repairInstructions, () => {
  repairGenerationId += 1
  repairLoading.value = false
  repairPreview.value = null
  repairError.value = ''
}, { flush: 'sync' })

onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload))
onBeforeUnmount(() => {
  unmounted = true
  streamRunId += 1
  invalidateRepair()
  window.removeEventListener('beforeunload', handleBeforeUnload)
  finishDiscard(false)
})
</script>

<template>
  <div class="writing-panel">
    <div class="chapter-sidebar">
      <div class="pane-header">
        <div>
          <div>章节</div>
          <div class="book-stats">{{ chapters.length }} 章 · {{ totalWords }} 字</div>
        </div>
        <n-button size="small" type="primary" :disabled="repairApplying" @click="startNew()">新章</n-button>
      </div>
      <n-empty v-if="!chapters.length" description="暂无章节" />
      <n-list v-else clickable hoverable>
        <n-list-item
          v-for="c in chapters"
          :key="c.id"
          :class="{ active: c.id === selectedId }"
          @click="selectChapter(c)"
        >
          <div class="chapter-item">
            <span>{{ chapterLabel(c) }}</span>
            <n-tag size="tiny" :type="c.status === 'finalized' ? 'success' : 'default'">
              {{ c.status === 'finalized' ? '已定稿' : '草稿' }}
            </n-tag>
          </div>
        </n-list-item>
      </n-list>
    </div>

    <div class="editor">
      <div class="editor-toolbar">
        <n-input v-model:value="title" :disabled="repairApplying" placeholder="章节标题" class="title-input" />
        <n-input v-model:value="directive" :disabled="repairApplying" placeholder="章前指令（如：让 A 与 B 决裂）" class="directive-input" />
        <div class="toolbar-groups">
        <div class="btn-row primary-actions">
          <n-button type="primary" :loading="streaming" :disabled="repairApplying" @click="doWrite">写新章</n-button>
          <n-button
            :disabled="!selectedId || !isLatestChapter || repairApplying"
            :loading="streaming"
            :title="isLatestChapter ? '从当前最后一章继续生成' : '请选择章节列表中的最后一章'"
            @click="doWriteNext"
          >
            生成下一章
          </n-button>
          <n-button :disabled="!selectedId || repairApplying" :loading="streaming" @click="doContinue">
            续写本章
          </n-button>
          <n-button :disabled="!selectedId || !isDirty || repairApplying" @click="saveContent">保存</n-button>
          <span v-if="selectedId" class="save-state" :class="{ dirty: isDirty }">
            {{ isDirty ? '未保存' : '已保存' }}
          </span>
        </div>
        <div class="btn-row edit-actions">
          <n-select
            v-model:value="rewriteIntent"
            :options="REWRITE_INTENT_OPTIONS"
            placeholder="改写意图"
            style="width: 120px"
            clearable
          />
          <n-button :disabled="!selectedId || repairApplying" @click="doRewrite">改写选中</n-button>
        </div>
        <div class="btn-row quality-actions">
          <n-button :disabled="!selectedId" @click="doScanCliche">套路扫描</n-button>
          <n-button :disabled="!selectedId" @click="doConsistency">一致性检查</n-button>
          <n-button :disabled="!selectedId" :loading="deepLoading" @click="doDeepCheck">
            深度检查
          </n-button>
          <span class="cost-hint">按有效分块调用模型，最多 8 次</span>
          <n-button
            :disabled="!selectedId || !selectedChapter?.outline_node_id || !content.trim()"
            :loading="outlineCheckLoading"
            title="按需调用模型 1 次；仅提示与绑定大纲的偏差，不修改正文"
            @click="doOutlineCheck"
          >大纲契合检查</n-button>
          <span class="cost-hint">本项调用模型 1 次</span>
          <n-button
            :disabled="!selectedId || !content.trim() || streaming || repairApplying || !!storyWordLimitError"
            :loading="storyCheckLoading"
            title="检查当前正文的章前指令、前章承接、设定和大纲；调用模型 1 次，不修改正文"
            @click="doStoryCheck"
          >剧情检查</n-button>
          <n-button
            :disabled="!selectedId || !content.trim() || isDirty || streaming || hasIncompleteOutput || repairApplying || storyCheckLoading || !!storyWordLimitError"
            :title="isDirty ? '请先保存当前修改，再生成修订稿' : '根据已保存正文、构思和当前检查结果生成整章修订预览'"
            @click="openRepair"
          >生成修订稿</n-button>
          <n-button type="success" :disabled="!selectedId || streaming || hasIncompleteOutput || repairApplying" @click="doSettle">定稿沉淀</n-button>
        </div>
        <div class="btn-row version-actions">
          <n-button :disabled="!selectedId || summarySaving || streaming || repairApplying" @click="openSummary">章节摘要</n-button>
          <n-button :disabled="!selectedId || repairApplying" @click="openRevisionHistory">版本历史</n-button>
          <span class="cost-hint">覆盖正文前会自动保存旧版本</span>
        </div>
        <div v-if="selectedId" class="word-limit-row" aria-label="剧情检查字数目标">
          <span>本次检查字数目标（可选）</span>
          <n-input-number v-model:value="storyMinWords" :min="1" :max="1000000" :precision="0" :disabled="streaming || repairApplying" :input-props="{ 'aria-label': '检查最低字数' }" placeholder="最低字数" />
          <span>至</span>
          <n-input-number v-model:value="storyMaxWords" :min="1" :max="1000000" :precision="0" :disabled="streaming || repairApplying" :input-props="{ 'aria-label': '检查最高字数' }" placeholder="最高字数" />
          <span class="cost-hint">按非空白字符计数，含标点</span>
          <p v-if="storyWordLimitError" class="story-review-hint" role="alert">{{ storyWordLimitError }}</p>
        </div>
        <div class="btn-row file-actions">
          <n-button @click="doExport('markdown')">导出 MD</n-button>
          <n-button @click="doExport('txt')">导出 TXT</n-button>
          <n-button
            type="error"
            secondary
            :disabled="!selectedId || streaming || repairApplying"
            @click="showDelete = true"
          >
            删除章节
          </n-button>
        </div>
        <p v-if="selectedId && isDirty" class="story-review-hint">生成修订稿前，请先保存当前正文、标题和章前指令。</p>
        </div>
        <div v-if="selectedId" class="next-chapter-hint">
          <template v-if="isLatestChapter">
            “生成下一章”会携带上一章章首、结尾、最近剧情和作品设定；“续写本章”会追加到当前正文。
          </template>
          <template v-else>如需生成下一章，请先选择章节列表中的最后一章。</template>
        </div>
      </div>

      <div v-if="streaming" class="stream-box">
        <div class="stream-title">生成中…</div>
        <div class="stream-text">{{ streamText }}</div>
      </div>

      <section v-if="!streaming && hasIncompleteOutput" class="incomplete-panel" aria-label="生成输出待处理">
        <div class="incomplete-title">{{ streamCompleted ? '已完成，页面同步失败' : streamUnconfirmed ? '完成状态未确认' : '未完成，未保存' }}</div>
        <p class="incomplete-reason">{{ streamError }}</p>
        <p v-if="streamCompleted" class="incomplete-hint">已收到完成确认，请刷新后核对保存结果；以下输出仅供复制留存。</p>
        <p v-else-if="streamUnconfirmed" class="incomplete-hint">本次输出尚未收到完成确认，暂留此处；请先复制输出，再重新加载核对保存结果。</p>
        <p v-else class="incomplete-hint">以下是本次已收到的输出，尚未写入章节；当前章节的正文仍可编辑。</p>
        <div class="incomplete-actions">
          <n-button size="small" :disabled="!streamText" @click="copyIncompleteOutput">复制输出</n-button>
          <n-button v-if="incompleteCollapsed" size="small" @click="incompleteCollapsed = false">展开未完成稿</n-button>
          <n-button v-else size="small" @click="collapseIncompleteOutput">收起未完成稿</n-button>
          <n-button size="small" type="warning" @click="discardIncompleteOutput">放弃未完成稿</n-button>
        </div>
        <p v-if="copyFallback" class="incomplete-hint">自动复制失败，请在下方选中并复制需要保留的内容。</p>
        <textarea
          v-show="!incompleteCollapsed"
          ref="incompleteTextRef"
          :value="streamText"
          readonly
          aria-label="未完成稿"
          class="incomplete-text"
          placeholder="本次未收到文本输出"
        ></textarea>
      </section>

      <div v-if="!streaming && selectedId" class="editor-body">
        <textarea
          ref="contentRef"
          v-model="content"
          :disabled="repairApplying"
          class="content-editor"
          placeholder="在此写作，或点击上方按钮生成"
        ></textarea>
        <div class="editor-status">本章 {{ chapterWords }} 字</div>
      </div>
      <div v-else-if="!streaming" class="editor-empty">
        <n-empty description="选择章节或点击「新章」开始写作" />
      </div>

      <div v-if="conflicts.length || clicheHits.length" class="marks">
        <div class="marks-title">正文标记（冲突 / 套路命中）</div>
        <div class="preview">
          <span
            v-for="(seg, i) in segments"
            :key="i"
            :class="{ plain: seg.kind === 'plain', conflict: seg.kind === 'conflict', cliche: seg.kind === 'cliche' }"
            >{{ seg.text }}</span
          >
        </div>
      </div>

      <div v-if="cliche" class="cliche-panel">
        <div class="cliche-title">
          套路密度
          <span class="cliche-density">{{ cliche.density }} /千字</span>
          <span class="cliche-total">共 {{ cliche.total }} 处</span>
        </div>
        <div class="cliche-cats">
          <span v-for="cat in CLICHE_CATEGORIES" :key="cat" class="cliche-cat">
            <n-tag size="small" :type="(cliche.by_category[cat] ?? 0) > 0 ? 'warning' : 'default'">
              {{ cat }}
            </n-tag>
            <span class="cliche-cat-count">{{ cliche.by_category[cat] ?? 0 }}</span>
          </span>
        </div>
      </div>

      <div v-if="conflicts.length || deepMeta || outlineCheckResult || storyCheckResult" class="result-cols">
        <div v-if="conflicts.length" class="result-col result-conflicts">
          <div class="result-title">规则层一致性冲突</div>
          <n-list>
            <n-list-item v-for="(c, i) in conflicts" :key="i">
              <n-tag type="error" size="small">{{ c.rule }}</n-tag>
              {{ c.message }}
            </n-list-item>
          </n-list>
        </div>

        <div v-if="deepMeta" class="result-col result-deep">
          <div class="result-title">
            语义深度检查
            <span class="deep-meta">
              已查 {{ deepMeta.chunks_checked }} 块 / 跳过 {{ deepMeta.chunks_skipped }} 块
            </span>
            <n-tag v-if="deepMeta.truncated" type="warning" size="tiny">已截断（上限 8 块）</n-tag>
          </div>
          <n-empty v-if="!deepConflicts.length" description="语义层未发现冲突" />
          <n-list v-else>
            <n-list-item v-for="(d, i) in deepConflicts" :key="i">
              <div class="deep-item" @click="locateExcerpt(d.excerpt)">
                <div class="deep-head">
                  <n-tag size="small" :type="d.severity === 'high' ? 'error' : 'warning'">
                    {{ d.type }}
                  </n-tag>
                  <span class="deep-chunk">块 {{ d.chunk_index }}</span>
                </div>
                <div class="deep-excerpt">{{ d.excerpt }}</div>
                <div class="deep-reason">依据：{{ d.reason }}</div>
              </div>
            </n-list-item>
          </n-list>
        </div>

        <div v-if="outlineCheckResult" class="result-col result-outline">
          <div class="result-title">
            大纲契合检查 · {{ outlineCheckResult.outline_title }}
            <n-tag v-if="outlineCheckResult.truncated" type="warning" size="tiny">仅检查章首与章尾</n-tag>
          </div>
          <n-empty v-if="!outlineCheckResult.issues.length" description="未发现明显偏差，仍需人工校对" />
          <n-list v-else>
            <n-list-item v-for="(issue, i) in outlineCheckResult.issues" :key="i">
              <div class="deep-item" @click="locateExcerpt(issue.excerpt)">
                <div class="deep-head">
                  <n-tag size="small" :type="issue.severity === 'high' ? 'error' : 'warning'">
                    {{ OUTLINE_ISSUE_LABEL[issue.kind] }}
                  </n-tag>
                  <span>{{ issue.outline_point }}</span>
                </div>
                <div v-if="issue.excerpt" class="deep-excerpt">正文：{{ issue.excerpt }}</div>
                <div class="deep-reason">{{ issue.reason }}</div>
              </div>
            </n-list-item>
          </n-list>
        </div>

        <section v-if="storyCheckResult" class="result-col result-story" aria-label="剧情检查结果">
          <div class="result-title">
            剧情检查 · {{ storyCheckResult.chapter_title }}
            <n-tag v-if="storyCheckResult.truncated" type="warning" size="tiny">正文或依据有截断</n-tag>
            <n-tag v-if="storyReviewIncomplete" type="warning" size="tiny">检查不完整</n-tag>
          </div>
          <p class="story-context">{{ storyCheckResult.previous_chapter_title ? `承接前章：${storyCheckResult.previous_chapter_title}` : '本章没有前章' }}</p>
          <p class="story-context">核对当前正文、设定和最近前章；不等于全书检查。<template v-if="storyCheckResult.history_truncated">前章资料仅包含章首、章尾或简略脉络。</template></p>
          <p v-if="storyReviewIncomplete || storyCheckResult.truncated" class="story-review-hint">本次检查不能代表全文与全部依据均已核对，请继续人工校对。</p>
          <div v-if="storyCheckResult.word_count" class="story-word-count" aria-label="正文字数检查">
            <p class="story-context">正文 {{ storyCheckResult.word_count.count }} 字 · {{ wordLimitLabel(storyCheckResult.word_count.within_limits === null ? null : storyCheckResult.word_count) }}</p>
            <n-tag v-if="storyCheckResult.word_count.within_limits !== null" :type="storyCheckResult.word_count.within_limits ? 'success' : 'warning'" size="small">{{ storyCheckResult.word_count.within_limits ? '字数在目标范围内' : '字数未达到目标范围' }}</n-tag>
            <p v-for="warning in storyCheckResult.word_count.warnings" :key="warning" class="story-review-hint">{{ warning }}</p>
          </div>
          <section v-if="storyRequirementRows.length" class="story-requirements" aria-label="构思逐项核对">
            <div class="result-title">构思逐项核对 · {{ storyRequirementRows.length }} 项</div>
            <article v-for="requirement in storyRequirementRows" :key="requirement.id" class="story-item">
              <div class="deep-head">
                <n-tag size="small" :type="requirement.status === 'satisfied' ? 'success' : requirement.status === 'violation' ? 'error' : 'warning'">{{ STORY_REQUIREMENT_STATUS[requirement.status] }}</n-tag>
                <span>{{ STORY_ISSUE_LABEL[requirement.source] }}</span>
              </div>
              <div class="story-requirement">{{ requirement.text }}</div>
              <button v-if="requirement.excerpt" type="button" class="story-excerpt" @click="locateExcerpt(requirement.excerpt)">正文证据：{{ requirement.excerpt }}</button>
              <div v-else class="deep-reason">本项未提供可定位的正文引文。</div>
              <div class="deep-reason">{{ requirement.reason }}</div>
            </article>
          </section>
          <n-empty v-if="!storyCheckResult.issues.length" :description="storyCheckEmptyText" />
          <n-list v-else>
            <n-list-item v-for="(issue, i) in storyCheckResult.issues" :key="i">
              <div class="story-item">
                <div class="deep-head">
                  <n-tag size="small" :type="issue.severity === 'high' ? 'error' : 'warning'">{{ STORY_ISSUE_LABEL[issue.category] }}</n-tag>
                  <span>{{ STORY_SEVERITY_LABEL[issue.severity] }}</span>
                </div>
                <div class="story-requirement">原要求：{{ issue.requirement }}</div>
                <button v-if="issue.excerpt" type="button" class="story-excerpt" @click="locateExcerpt(issue.excerpt)">正文：{{ issue.excerpt }}</button>
                <div v-else class="deep-reason">本项未提供可定位的正文引文。</div>
                <div class="deep-reason">{{ issue.reason }}</div>
              </div>
            </n-list-item>
          </n-list>
        </section>
      </div>
    </div>

    <n-modal
      :show="showRepair" preset="card" title="整章修订预览"
      class="repair-modal" style="width: min(960px, calc(100vw - 32px))"
      :mask-closable="!repairApplying" :close-on-esc="!repairApplying" :closable="!repairApplying"
      @update:show="handleRepairVisibility"
    >
      <div class="repair-dialog-body">
        <p class="summary-hint">以当前已保存正文为依据。生成候选稿调用模型 1 次；确认保存后再调用模型 1 次复检。覆盖正文前会保留旧版本。</p>
        <p v-if="repairContext" class="story-context">
          本次目标：{{ wordLimitLabel(repairContext.wordLimits) }} · 检查提示偏差 {{ repairContext.issues.length }} 项 · 待核构思 {{ repairContext.unreviewedIds.length }} 项
        </p>
        <label class="repair-instructions">
          <span>修订要求（可选）</span>
          <n-input
            v-model:value="repairInstructions" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }"
            :maxlength="4000" show-count
            :disabled="repairLoading || repairApplying" :input-props="{ 'aria-label': '修订要求' }"
            placeholder="例如：压缩重复描写，保留拒绝偷取档案与依法登记的场景"
          />
        </label>
        <p v-if="repairError" class="repair-error" role="alert">{{ repairError }}</p>
        <div v-if="repairPreview" class="preview-cols repair-cols">
          <section class="preview-col" aria-label="修订原文">
            <div class="preview-col-title">原文 · {{ repairPreview.original_word_count.count }} 字</div>
            <div class="preview-col-text">{{ repairPreview.original_content }}</div>
          </section>
          <section class="preview-col" aria-label="修订候选稿">
            <div class="preview-col-title">候选稿 · {{ repairPreview.word_count.count }} 字</div>
            <n-tag v-if="repairPreview.word_count.within_limits !== null" :type="repairPreview.word_count.within_limits ? 'success' : 'warning'" size="small">
              {{ repairPreview.word_count.within_limits ? '字数在目标范围内' : '字数未达到目标范围' }}
            </n-tag>
            <p v-for="warning in repairPreview.word_count.warnings" :key="warning" class="story-review-hint">{{ warning }}</p>
            <div class="preview-col-text">{{ repairPreview.content }}</div>
          </section>
        </div>
        <p v-if="repairPreview" class="summary-hint">候选稿尚未保存，也未完成剧情复检；请对照原文确认。保存不会自动定稿或继续自动驾驶。</p>
        <p v-if="repairPreview && repairPreview.content === repairPreview.original_content" class="story-review-hint">候选稿与原文相同，未产生正文修改。可补充修订要求后重新生成。</p>
        <p v-if="repairApplying" class="story-review-hint" role="status">正在保存修订，请等待保存回应。</p>
      </div>
      <template #footer>
        <div class="modal-actions repair-actions">
          <n-button :disabled="repairApplying" @click="discardRepair">放弃修订</n-button>
          <n-button v-if="repairRefreshNeeded" :disabled="repairApplying" @click="refreshRepairChapter">刷新章节并核对</n-button>
          <n-button :disabled="repairApplying || repairRefreshNeeded" :loading="repairLoading" @click="generateRepair">{{ repairPreview ? '重新生成候选稿' : '生成候选稿' }}</n-button>
          <n-button type="primary" :loading="repairApplying" :disabled="!repairPreview || repairPreview.content === repairPreview.original_content || repairLoading || repairRefreshNeeded" @click="applyRepair">保存修订并复检</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showPreview" preset="card" title="改写预览（尚未落库）" style="width: 760px">
      <div class="preview-cols">
        <div class="preview-col">
          <div class="preview-col-title">原文</div>
          <div class="preview-col-text">{{ previewPassage }}</div>
        </div>
        <div class="preview-col">
          <div class="preview-col-title">改写</div>
          <div class="preview-col-text">{{ previewNew }}</div>
        </div>
      </div>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="discardPreview">放弃</n-button>
          <n-button type="primary" :disabled="hasIncompleteOutput || streaming || !previewFullContent.trim()" @click="applyPreview">应用</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showDelete" preset="card" title="删除章节" style="width: 460px">
      <p>
        确定删除“{{ selectedChapter ? chapterLabel(selectedChapter) : '' }}”吗？该章约
        {{ chapterWords }} 字，删除后无法恢复。
      </p>
      <p class="delete-warning">
        该章的<b>历史版本也会一并删除</b>，本阶段没有回收站。
      </p>
      <p v-if="hasUnsavedChanges" class="delete-warning">当前未保存的修改和未完成输出也会一并丢失。</p>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showDelete = false">取消</n-button>
          <n-button type="error" :loading="deleting" @click="deleteChapter">确认删除</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showWriteNext" preset="card" title="生成下一章" style="width: 560px">
      <div class="next-form">
        <label>
          <span>下一章标题（可选）</span>
          <n-input v-model:value="nextTitle" placeholder="留空则自动使用章节序号" />
        </label>
        <label>
          <span>下一章指令（可选，最高优先级）</span>
          <n-input
            v-model:value="nextDirective"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="例如：黑西装男人跟踪吴迪，推进上一章留下的悬念"
          />
        </label>
        <label>
          <span>章级大纲（可选）</span>
          <n-select
            v-model:value="nextOutlineNodeId"
            :options="nextOutlineOptions"
            placeholder="选择要承接的章级大纲"
            clearable
          />
        </label>
        <p v-if="selectedNextOutline?.summary" class="next-modal-hint">
          大纲提要：{{ selectedNextOutline.summary }}
        </p>
        <p class="next-modal-hint">
          系统会同时携带上一章章首、结尾、最近剧情、Story Bible 与未解决伏笔，避免重复开局或改写既有能力规则。
        </p>
      </div>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showWriteNext = false">取消</n-button>
          <n-button type="primary" @click="confirmWriteNext">开始生成</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal
      :show="showSummary"
      :closable="!summarySaving"
      :mask-closable="!summarySaving"
      :close-on-esc="!summarySaving"
      @update:show="closeSummary"
      preset="card"
      :title="selectedChapter ? `章节摘要 · ${chapterLabel(selectedChapter)}` : '章节摘要'"
      style="width: min(560px, calc(100vw - 32px))"
    >
      <n-input
        v-model:value="summaryDraft"
        type="textarea"
        :autosize="{ minRows: 4, maxRows: 12 }"
        :disabled="summarySaving"
        placeholder="填写或校正本章摘要"
      />
      <p v-if="!savedSummary.trim()" class="summary-hint">暂无已保存摘要，可在这里填写，也可在定稿沉淀后校正。</p>
      <p class="summary-hint">摘要会用于后续写作。这里只保存摘要，定稿仍需点击“定稿沉淀”。</p>
      <p v-if="isDirty" class="summary-hint">当前章节有未保存修改，保存摘要不会保存这些修改。</p>
      <p v-if="isSummaryDirty" class="summary-hint">摘要有未保存修改。</p>
      <template #footer>
        <div class="modal-actions">
          <n-button :disabled="summarySaving" @click="closeSummary">关闭</n-button>
          <n-button type="primary" :disabled="!isSummaryDirty" :loading="summarySaving" @click="saveSummary">保存摘要</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal
      :show="showDiscard"
      preset="dialog"
      type="warning"
      title="未保存修改"
      :content="discardPrompt"
      :positive-text="discardConfirmText"
      negative-text="继续编辑"
      @positive-click="finishDiscard(true)"
      @negative-click="finishDiscard(false)"
      @update:show="finishDiscard(false)"
    />

    <n-modal v-model:show="showSettle" preset="card" title="设定变更建议" style="width: 560px">
      <p class="settle-summary">摘要：{{ settleSummary }}</p>
      <div v-if="suggestions.length">
        <div v-for="(s, i) in suggestions" :key="i" class="suggestion-row">
          <n-checkbox v-model:checked="checkedSuggestion[i]">{{ suggestionLabel(s) }}</n-checkbox>
        </div>
      </div>
      <n-empty v-else description="未识别到新增设定" />
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showSettle = false">取消</n-button>
          <n-button type="primary" @click="confirmSettle">确认落库</n-button>
        </div>
      </template>
    </n-modal>

    <ChapterRevisionPanel
      v-model:show="showRevisions"
      :novel-id="props.novelId"
      :chapter-id="selectedId"
      :chapter-title="title"
      :current-content="content"
      :current-status="selectedChapter?.status ?? 'draft'"
      @restored="onRestored"
    />
  </div>
</template>

<style scoped>
.word-limit-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
}
.word-limit-row :deep(.n-input-number) {
  width: 140px;
}
.story-requirements .story-item {
  padding: 10px 0;
  border-bottom: 1px solid #eee;
}
.writing-panel {
  display: flex;
  height: 100%;
}
.chapter-sidebar {
  width: 240px;
  border-right: 1px solid #e5e5e5;
  padding: 12px;
  overflow: auto;
}
.pane-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-weight: 600;
}
.chapter-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.book-stats {
  margin-top: 2px;
  color: #888;
  font-size: 12px;
  font-weight: 400;
}
.chapter-sidebar :deep(.n-list-item.active) {
  background: #e8f4ff;
}
.editor {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 12px 16px;
  overflow-y: auto;
}
.editor-toolbar {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 8px;
  margin-bottom: 12px;
}
.title-input {
  width: 100%;
}
.directive-input {
  width: 100%;
}
.btn-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.toolbar-groups {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
}
.toolbar-groups > .btn-row:not(:last-child) {
  padding-right: 16px;
  border-right: 1px solid #e5e5e5;
}
.save-state {
  color: #18a058;
  font-size: 12px;
}
.save-state.dirty {
  color: #d07830;
}
.next-chapter-hint {
  color: #666;
  font-size: 12px;
}
.editor-body {
  flex: 1;
  min-height: 320px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.content-editor {
  flex: 1;
  width: 100%;
  min-height: 0;
  padding: 12px;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  font-family: inherit;
  line-height: 1.7;
  resize: none;
  box-sizing: border-box;
}
.editor-status {
  flex-shrink: 0;
  padding-top: 6px;
  color: #777;
  font-size: 12px;
  text-align: right;
}
.delete-warning {
  color: #d03050;
}
.next-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.next-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-weight: 600;
}
.next-modal-hint {
  margin: 0;
  color: #666;
  font-size: 12px;
  line-height: 1.6;
}
.editor-empty {
  padding: 40px;
}
.stream-box {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 12px;
}
.stream-title {
  color: #888;
  margin-bottom: 8px;
}
.stream-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.incomplete-panel {
  flex-shrink: 0;
  border: 1px solid #e4b15a;
  border-radius: 4px;
  padding: 12px;
  margin-bottom: 12px;
  background: #fffaf0;
}
.incomplete-title {
  color: #9c5c00;
  font-weight: 600;
}
.incomplete-reason,
.incomplete-hint {
  margin: 6px 0;
  font-size: 13px;
  line-height: 1.5;
}
.incomplete-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}
.incomplete-text {
  width: 100%;
  height: 130px;
  min-height: 80px;
  max-height: 260px;
  padding: 8px;
  font: inherit;
  line-height: 1.7;
  box-sizing: border-box;
  resize: vertical;
}
.cost-hint {
  color: #999;
  font-size: 12px;
}
.version-actions {
  border-top: 1px dashed #eee;
  padding-top: 8px;
  margin-top: 8px;
}
.marks {
  margin-top: 12px;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 12px;
  max-height: 200px;
  overflow: auto;
}
.marks-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.preview {
  white-space: pre-wrap;
  line-height: 1.7;
  margin-bottom: 8px;
}
/* 冲突一种底色、套路命中一种底色 */
.preview .conflict {
  background: #ffd8d8;
}
.preview .cliche {
  background: #ffe58f;
}
.cliche-panel {
  margin-top: 12px;
  border: 1px solid #f0d9a0;
  border-radius: 4px;
  padding: 12px;
}
.cliche-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.cliche-density {
  color: #d48806;
  margin-left: 8px;
}
.cliche-total {
  color: #999;
  margin-left: 8px;
  font-weight: 400;
}
.cliche-cats {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.cliche-cat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.cliche-cat-count {
  color: #666;
}
.result-cols {
  display: flex;
  flex-wrap: wrap;
  flex-shrink: 0;
  gap: 12px;
  margin-top: 12px;
  align-items: flex-start;
}
.result-col {
  flex: 1;
  min-width: 280px;
  border-radius: 4px;
  padding: 12px;
  max-height: 240px;
  overflow: auto;
}
.result-conflicts {
  border: 1px solid #f0c0c0;
}
.result-deep {
  border: 1px solid #c0d8f0;
}
.result-outline {
  border: 1px solid #a9d9ba;
}
.result-story {
  border: 1px solid #e4b15a;
}
.story-context,
.story-review-hint {
  margin: 4px 0 8px;
  color: #777;
  font-size: 12px;
}
.story-review-hint {
  color: #9c5c00;
}
.story-requirement,
.story-excerpt {
  line-height: 1.6;
  white-space: pre-wrap;
}
.story-excerpt {
  display: block;
  width: 100%;
  padding: 0;
  margin-top: 4px;
  border: 0;
  color: inherit;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.story-excerpt:hover {
  background: #e8f4ff;
}
.result-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.result-conflicts .result-title {
  color: #d03050;
}
.result-deep .result-title {
  color: #2080f0;
}
.result-outline .result-title {
  color: #18a058;
}
.deep-meta {
  color: #999;
  font-weight: 400;
  font-size: 12px;
  margin-left: 8px;
}
.deep-item {
  cursor: pointer;
}
.deep-item:hover .deep-excerpt {
  background: #e8f4ff;
}
.deep-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.deep-chunk {
  color: #999;
  font-size: 12px;
}
.deep-excerpt {
  line-height: 1.6;
}
.deep-reason {
  color: #666;
  font-size: 12px;
  margin-top: 4px;
}
.preview-cols {
  display: flex;
  gap: 12px;
}
.preview-col {
  flex: 1;
  min-width: 0;
}
.preview-col-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.preview-col-text {
  white-space: pre-wrap;
  line-height: 1.7;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 8px;
  max-height: 360px;
  overflow: auto;
}
.repair-dialog-body {
  max-height: calc(100dvh - 230px);
  overflow-y: auto;
}
.repair-modal {
  max-height: calc(100dvh - 32px);
}
.repair-modal :deep(.n-card__content) {
  min-height: 0;
  overflow-y: auto;
}
.repair-instructions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 12px 0;
}
.repair-cols {
  margin-top: 12px;
  align-items: flex-start;
}
.repair-cols .preview-col-text {
  max-height: min(42dvh, 360px);
}
.repair-error {
  color: #d03050;
  white-space: pre-wrap;
}
.repair-actions {
  flex-wrap: wrap;
}
@media (max-width: 640px) {
  .repair-cols {
    flex-direction: column;
  }
  .repair-cols .preview-col {
    width: 100%;
  }
}
.settle-summary {
  color: #666;
}
.summary-hint {
  color: #888;
  font-size: 12px;
}
.suggestion-row {
  margin: 6px 0;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
