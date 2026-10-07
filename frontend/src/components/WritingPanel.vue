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
import { isChapterCreationResult, listChapterCreations, newChapterCreation, removeChapterCreation, storeChapterCreation,
  type ChapterCreationIntent } from '../utils/chapterCreation'
import ChapterRevisionPanel from './ChapterRevisionPanel.vue'
import type {
  Chapter,
  ChapterAuthorFields,
  ChapterMetadata,
  ApplicationFailure,
  ChapterRepairPreview,
  ClicheHit,
  ClicheResult,
  Conflict,
  DeepCheckConflict,
  DeepCheckResult,
  OutlineCheckResult,
  OutlineNode,
  ParagraphRewriteRequest,
  Segment,
  StoryCheckResult,
  StoryCheckIssue,
  Suggestion,
  SettleApplyResult,
  SettleReceipt,
  WordLimits,
} from '../types'

const props = defineProps<{ novelId: string; initialChapterId?: string | null }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const chapters = ref<ChapterMetadata[]>([])
const selectedId = ref<string | null>(null)
const selectedDetail = ref<Chapter | null>(null)
const chapterLoadingId = ref<string | null>(null)
const title = ref('')
const directive = ref('')
const content = ref('')
const contentRef = ref<HTMLTextAreaElement | null>(null)
const savedSnapshot = ref({ title: '', directive: '', content: '' })
const saving = ref(false)
const activeCreation = ref<ChapterCreationIntent | null>(null)
const creationRecords = ref<ChapterCreationIntent[]>([])
const creationStorageWarning = ref('')
const creationMemory = new Map<string, ChapterCreationIntent>()
const creationPersisted = new Set<string>()
const creationVolatile = new Set<string>()
const creationKey = (novelId: string, requestId: string) => `${novelId}/${requestId}`
let creationBindingRevision = 0
const hasUnverifiedCreation = computed(() => !!activeCreation.value && !selectedId.value)
const creationBlocked = computed(() => !!activeCreation.value?.blockedCode)
const creationBlockedMessage = computed(() => {
  const labels: Record<string, string> = {
    chapter_creation_deleted: '原创建章节已删除，本次未重建。请先复制稿件并核对目录，再决定是否另开新章。',
    chapter_creation_payload_conflict: '原创建请求与首次提交不一致。请先复制稿件并核对目录，再决定是否另开新章。',
    chapter_creation_scope_conflict: '原创建请求属于其他作品。请先复制稿件并核对目标作品，再决定是否另开新章。',
    local_record_discarded: '本地核对记录已移除，原稿仍可能在目录。请先复制稿件并核对目录，再决定是否另开新章。',
  }
  return labels[activeCreation.value?.blockedCode ?? ''] ?? '原创建请求已被拒绝，请先复制稿件并核对目录。'
})
interface EditorScope { novelId: string; chapterId: string | null; epoch: number }
let pageEpoch = 0
let editorEpoch = 0
let authorRevision = 0
let chapterLoadRequestId = 0
let chapterDetailRequestId = 0
let saveRequestId = 0
let qualityEpoch = 0
const qualityRequests = { cliche: 0, consistency: 0, deep: 0, outline: 0 }

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
const deepMeta = ref<DeepCheckResult | null>(null)
const deepResultConfirmed = ref(false)
const deepCoverageConfirmed = ref(false)
const deepRequestError = ref('')
const deepLoading = ref(false)
const DEEP_STATUS_LABEL = { complete: '已完成可核对范围', partial: '部分完成', failed: '检查失败', skipped: '未执行语义检查' }
const deepRuleConflicts = computed(() => deepMeta.value?.rule_conflicts ?? [])
const deepStatusLabel = computed(() => {
  if (deepLoading.value) return '正在检查'
  if (deepRequestError.value) return '请求未完成'
  if (!deepResultConfirmed.value || !deepMeta.value) return '未完整确认'
  return DEEP_STATUS_LABEL[deepMeta.value.status]
})
const deepClean = computed(() => deepResultConfirmed.value && deepMeta.value?.status === 'complete'
  && !deepConflicts.value.length && !deepRuleConflicts.value.length)
const deepIncompleteHint = computed(() => {
  if (deepRequestError.value) return '深度检查请求未完成，不能判断是否存在语义冲突；请重试或人工核对。'
  if (!deepResultConfirmed.value) return '本次深度检查未得到完整确认，不能判断是否存在语义冲突；请核对服务版本并重试或人工校对。'
  if (deepMeta.value?.status === 'failed') return '语义检查未完成，所有尝试分块均失败；请重试并人工核对。'
  if (deepMeta.value?.status === 'partial') return '部分范围未完成核对；返回的冲突只来自已检查内容，请重试并人工核对失败及未查范围。'
  if (deepMeta.value?.status === 'skipped') return '未执行语义检查，本次没有可核对分块；请查看跳过原因与规则冲突并人工校对。'
  return ''
})
const deepCheckEmptyText = computed(() => deepIncompleteHint.value
  || (deepRuleConflicts.value.length ? '语义已检查范围未返回冲突；规则层发现问题，请核对下方冲突。'
    : '已检查范围内未发现明显冲突，仍需人工校对'))
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
const previewApplying = ref(false)
interface ParagraphRewriteContext extends EditorScope {
  authorRevision: number
  source: ChapterAuthorFields
  selectionStart: number
  selectionEnd: number
}
const previewContext = ref<ParagraphRewriteContext | null>(null)
const previewGuarded = ref(false)
const previewError = ref('')
const previewRefreshNeeded = ref(false)
const previewRefreshing = ref(false)
let previewRefreshRequestId = 0
let previewApplyRequestId = 0
let deleteRequestId = 0
let summaryRequestId = 0
let summaryEpoch = 0
let summaryRevision = 0
let restoreRequestId = 0
const revisionContext = ref<(EditorScope & { authorRevision: number }) | null>(null)
const nextChapterContext = ref<(EditorScope & { authorRevision: number }) | null>(null)
let nextChapterRequestId = 0

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
const settleDataTexts = ref<string[]>([])
const settleErrors = ref<Record<number, string>>({})
const settleCompleted = ref<boolean[]>([])
const settleApplying = ref(false)
const settledCount = ref(0)
const settleLoading = ref(false)
const settleWarnings = ref<string[]>([])
const settleReceipts = ref<Record<number, SettleReceipt>>({})
const settleConfirmedTexts = ref<Record<number, string>>({})
const settleRefreshNeeded = ref(false)
const settleReceiptUnavailable = ref(false)
interface SettleSubmission {
  sourceIndices: number[]
  sourceFingerprint: string
  payloadJson: string
  submittedTexts: Record<number, string>
  editorRevision: number
}
const settlePendingSubmission = ref<SettleSubmission | null>(null)
const settledNewCount = computed(() => Object.values(settleReceipts.value).filter((row) => !row.replayed).length)
const settledReplayedCount = computed(() => Object.values(settleReceipts.value).filter((row) => row.replayed).length)
const settledDeletedCount = computed(() => Object.values(settleReceipts.value).filter((row) => row.deleted).length)
interface SettleContext {
  novelId: string
  chapterId: string
  epoch: number
  title: string
  directive: string
  content: string
  sourceFingerprint: string | null
}
const settleContext = ref<SettleContext | null>(null)
const settleConfirmBlocked = computed(() => !validSettleFingerprint(settleContext.value?.sourceFingerprint)
  || settleRefreshNeeded.value || settleReceiptUnavailable.value)
let settleEpoch = 0
let settleRequestId = 0
let settleApplyRequestId = 0
let settleEditorRevision = 0

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

const selectedChapter = computed(() => selectedDetail.value?.id === selectedId.value ? selectedDetail.value : null)
const selectedMetadata = computed(() => chapters.value.find((c) => c.id === selectedId.value) ?? null)
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
const savedTotalWords = computed(() => chapters.value.reduce((total, chapter) => total + chapter.word_count, 0))
const totalWords = computed(() => savedTotalWords.value
  + (selectedMetadata.value ? chapterWords.value - selectedMetadata.value.word_count : 0))
const isDirty = computed(
  () =>
    !!props.novelId &&
    (title.value !== savedSnapshot.value.title ||
      directive.value !== savedSnapshot.value.directive ||
      content.value !== savedSnapshot.value.content),
)
const isSummaryDirty = computed(() => showSummary.value && summaryDraft.value !== savedSummary.value)
const hasIncompleteOutput = computed(() => !!streamError.value)
const hasUnsavedChanges = computed(
  () => isDirty.value || isSummaryDirty.value || streaming.value || hasIncompleteOutput.value
    || saving.value || previewApplying.value || showPreview.value
    || settleLoading.value || settleApplying.value || showSettle.value || hasUnverifiedCreation.value,
)

function captureEditorScope(): EditorScope {
  return { novelId: props.novelId, chapterId: selectedId.value, epoch: editorEpoch }
}

function isCurrentEditorScope(scope: EditorScope): boolean {
  return !unmounted && scope.epoch === editorEpoch
    && scope.novelId === props.novelId && scope.chapterId === selectedId.value
}

function invalidateEditorBinding() {
  editorEpoch += 1
  activeCreation.value = null
  invalidateChapterDetail()
  saveRequestId += 1
  saving.value = false
  deleteRequestId += 1
  deleting.value = false
  showDelete.value = false
  restoreRequestId += 1
  revisionContext.value = null
  showRevisions.value = false
  showWriteNext.value = false
  nextChapterRequestId += 1
  nextChapterContext.value = null
  invalidatePreview()
}

function authorSnapshot() {
  return { title: title.value, directive: directive.value, content: content.value }
}

function chapterMetadata(chapter: Chapter): ChapterMetadata {
  const { id, novel_id, outline_node_id, seq, title, status, tension_score, drift_score, created_at, updated_at } = chapter
  return { id, novel_id, outline_node_id, seq, title, status, tension_score, drift_score, created_at, updated_at,
    word_count: countCharacters(chapter.content) }
}

function invalidateChapterDetail() {
  chapterDetailRequestId += 1
  chapterLoadingId.value = null
}

function syncSelectedMetadata() {
  const metadata = selectedMetadata.value
  if (!metadata || !selectedChapter.value) return
  const { word_count: _wordCount, ...fields } = metadata
  selectedDetail.value = { ...selectedChapter.value, ...fields }
}

function upsertChapter(chapter: Chapter) {
  invalidateChapterDetail()
  const metadata = chapterMetadata(chapter)
  const index = chapters.value.findIndex((item) => item.id === chapter.id)
  if (index < 0) chapters.value = [...chapters.value, metadata].sort((a, b) => a.seq - b.seq)
  else chapters.value = chapters.value.map((item) => item.id === chapter.id ? metadata : item)
  if (chapter.id === selectedId.value) selectedDetail.value = chapter
}

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
  return !hasUnsavedChanges.value || await askDiscard(hasUnverifiedCreation.value
    ? '这次草稿可能已经保存，结果尚未核对。已保留的浏览器记录可用于恢复；记录保存失败或已移除时请先复制稿件。确定离开当前稿吗？'
    : '当前章节有未保存修改或未完成输出，确定放弃并离开吗？')
}

function refreshCreationRecords() {
  if (!props.novelId) { creationRecords.value = []; return }
  const result = listChapterCreations(props.novelId)
  const memory = [...creationMemory.values()].filter((record) => record.novelId === props.novelId)
  const combined = new Map(result.records.map((record) => [record.requestId, record]))
  for (const record of result.records) {
    const key = creationKey(record.novelId, record.requestId)
    creationPersisted.add(key)
    if (!creationVolatile.has(key)) creationMemory.set(key, record)
  }
  for (const record of memory) {
    if (!combined.has(record.requestId) || creationVolatile.has(creationKey(record.novelId, record.requestId))) combined.set(record.requestId, record)
  }
  if (activeCreation.value?.novelId === props.novelId) combined.set(activeCreation.value.requestId, activeCreation.value)
  creationRecords.value = [...combined.values()].sort((a, b) => b.createdAt - a.createdAt)
  if (result.unavailable) creationStorageWarning.value = '浏览器核对记录不可用，请先复制稿件；关闭或刷新后可能无法恢复本次核对入口。'
  else if (result.invalid) creationStorageWarning.value = '部分待核对记录无法读取，请保留稿件并检查浏览器存储。'
}

function persistCreation(intent: ChapterCreationIntent) {
  const key = creationKey(intent.novelId, intent.requestId)
  creationMemory.set(key, intent)
  const index = creationRecords.value.findIndex((record) => record.requestId === intent.requestId)
  if (index < 0) creationRecords.value.push(intent)
  else creationRecords.value[index] = intent
  if (storeChapterCreation(intent)) {
    creationPersisted.add(key)
    creationVolatile.delete(key)
    if (!creationVolatile.size && /^(最新编辑稿|本次核对记录)/.test(creationStorageWarning.value)) creationStorageWarning.value = ''
  } else {
    creationVolatile.add(key)
    creationStorageWarning.value = creationPersisted.has(key)
      ? '最新编辑稿未能更新到浏览器核对记录，请先复制稿件；刷新后只能恢复先前保存的记录。'
      : '本次核对记录未能保存在浏览器，请先复制稿件；关闭或刷新后可能无法找回这次保存结果。'
  }
}

async function restoreCreation(requestId: string) {
  if (saving.value || streaming.value || repairApplying.value) return
  const scope = captureEditorScope(), revision = authorRevision
  if (!await confirmDiscard() || !isCurrentEditorScope(scope) || revision !== authorRevision) return
  refreshCreationRecords()
  const record = creationRecords.value.find((row) => row.requestId === requestId && row.novelId === scope.novelId)
  if (!record) { message.warning('待核对记录已不存在，请核对章节目录'); return }
  const restored = JSON.parse(JSON.stringify(record)) as ChapterCreationIntent
  const page = pageEpoch
  // force 分支同步重置；恢复与赋值之间不让出微任务，避免迟到恢复覆盖刚切换的编辑器。
  void startNew(true)
  if (unmounted || page !== pageEpoch || props.novelId !== scope.novelId) return
  title.value = restored.draft.title
  content.value = restored.draft.content
  directive.value = restored.draft.directive
  activeCreation.value = restored
  creationBindingRevision = authorRevision
  message.info('待核对稿已恢复，请点击“核对原保存”确认结果；编辑稿仍保留。')
}

async function discardCreation(requestId: string) {
  if (saving.value) return
  const scope = captureEditorScope()
  if (!await askDiscard('这条草稿可能已经保存在章节目录。此操作只移除浏览器核对记录，不删除章节；请先复制需要保留的稿件。确定移除本地记录吗？', '移除本地记录')
    || !isCurrentEditorScope(scope)) return
  if (!removeChapterCreation(scope.novelId, requestId)) {
    creationStorageWarning.value = '浏览器记录移除失败，记录仍保留；章节未受到影响。'
    message.warning(creationStorageWarning.value)
    return
  }
  creationRecords.value = creationRecords.value.filter((record) => record.requestId !== requestId)
  const key = creationKey(scope.novelId, requestId)
  creationMemory.delete(key)
  creationPersisted.delete(key)
  creationVolatile.delete(key)
  if (activeCreation.value?.requestId === requestId) {
    // 移除核对记录不等于授权把可能已保存的稿件再创建一遍。
    activeCreation.value.blockedCode = 'local_record_discarded'
  }
  message.info('本地核对记录已移除，请按需要检查章节目录')
}

async function copyCreationDraft() {
  const text = `${title.value}\n章前指令：${directive.value}\n\n${content.value}`
  try { await navigator.clipboard.writeText(text); message.success('已复制当前稿件') }
  catch { message.warning('复制未完成，请选中编辑区的标题、指令和正文自行复制') }
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
  if (streaming.value || saving.value || previewApplying.value || deleting.value || repairApplying.value) return null
  const scope = captureEditorScope()
  if (hasIncompleteOutput.value && !await askDiscard('上次生成的输出尚未处理，新一轮生成会放弃这些输出，确定继续吗？', '放弃旧稿并生成')) return null
  if (!isCurrentEditorScope(scope)) return null
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
  const epoch = pageEpoch
  const requestId = ++chapterLoadRequestId
  const scope = captureEditorScope()
  const revision = authorRevision
  const loaded = await api.listChapterMetadata(novelId)
  if (unmounted || props.novelId !== novelId || epoch !== pageEpoch || requestId !== chapterLoadRequestId) return
  chapters.value = loaded
  syncSelectedMetadata()
  if (props.initialChapterId && !selectedId.value && !streaming.value && !isDirty.value
    && isCurrentEditorScope(scope) && revision === authorRevision) {
    const initial = chapters.value.find((chapter) => chapter.id === props.initialChapterId)
    if (initial) await selectChapter(initial, true)
  }
}

function clearMarks() {
  qualityEpoch += 1
  for (const kind of Object.keys(qualityRequests) as (keyof typeof qualityRequests)[]) qualityRequests[kind] += 1
  deepLoading.value = false
  outlineCheckLoading.value = false
  conflicts.value = []
  clicheHits.value = []
  cliche.value = null
  deepConflicts.value = []
  deepMeta.value = null
  deepResultConfirmed.value = false
  deepCoverageConfirmed.value = false
  deepRequestError.value = ''
  outlineCheckResult.value = null
  invalidateStoryCheck()
}

function invalidateStoryCheck() {
  storyCheckRequestId += 1
  storyCheckResult.value = null
  storyCheckLoading.value = false
}

function adoptChapter(c: Chapter) {
  invalidateEditorBinding()
  abandonStreamOutput()
  if (c.id !== selectedId.value) {
    showRevisions.value = false
    showSummary.value = false
  }
  selectedId.value = c.id
  selectedDetail.value = c
  title.value = c.title
  content.value = c.content
  directive.value = c.directive
  rememberSaved()
  clearMarks()
}

async function selectChapter(c: Pick<Chapter, 'id' | 'novel_id'>, force = false): Promise<boolean> {
  if (c.novel_id !== props.novelId || unmounted) return false
  if (!force && repairApplying.value) { message.warning('正在保存修订，请等待保存回应'); return false }
  const requestId = ++chapterDetailRequestId
  const targetId = c.id
  chapterLoadingId.value = null
  if (!force && targetId === selectedId.value) return false
  const scope = captureEditorScope()
  const revision = authorRevision
  const isCurrent = () => isCurrentEditorScope(scope) && requestId === chapterDetailRequestId && revision === authorRevision
  if (!force && !await confirmDiscard()) return false
  if (!isCurrent()) return false
  chapterLoadingId.value = targetId
  try {
    const chapter = await api.getChapter(scope.novelId, targetId)
    if (!isCurrent()) return false
    if (chapter.id !== targetId || chapter.novel_id !== scope.novelId) {
      message.warning('章节读取响应与目标不一致，当前编辑稿已保留')
      return false
    }
    upsertChapter(chapter)
    adoptChapter(chapter)
    return true
  } catch (error) {
    if (isCurrent()) message.error(`章节读取失败，当前编辑稿已保留：${(error as Error).message}`)
    return false
  } finally {
    if (requestId === chapterDetailRequestId) chapterLoadingId.value = null
  }
}

async function startNew(force = false) {
  if (!force && repairApplying.value) { message.warning('正在保存修订，请等待保存回应'); return }
  const scope = captureEditorScope()
  if (!force && !await confirmDiscard()) return
  if (!isCurrentEditorScope(scope)) return
  invalidateEditorBinding()
  abandonStreamOutput()
  showRevisions.value = false
  showSummary.value = false
  selectedId.value = null
  selectedDetail.value = null
  title.value = ''
  directive.value = ''
  content.value = ''
  rememberSaved()
  clearMarks()
}

async function reloadAndReselect(chapter?: Chapter, runId?: number) {
  const scope = captureEditorScope()
  const revision = authorRevision
  const detailRequestId = ++chapterDetailRequestId
  const c = chapter ?? (scope.chapterId ? await api.getChapter(scope.novelId, scope.chapterId) : null)
  if (!isCurrentEditorScope(scope) || detailRequestId !== chapterDetailRequestId) return
  if (runId !== undefined && !isCurrentStream(runId)) return
  if (!c || c.novel_id !== scope.novelId || c.id !== scope.chapterId) throw new Error('续写保存响应与原章节不一致，请核对原章')
  chapterLoadRequestId += 1
  upsertChapter(c)
  savedSnapshot.value = { title: c.title, directive: c.directive, content: c.content }
  if (revision === authorRevision) {
    content.value = c.content
    title.value = c.title
    directive.value = c.directive
    rememberSaved()
    clearMarks()
  }
  try {
    await loadChapters()
    if (!isCurrentEditorScope(scope)) return
    if (runId !== undefined && !isCurrentStream(runId)) return
    upsertChapter(c)
  } catch (error) {
    if (isCurrentEditorScope(scope) && (runId === undefined || isCurrentStream(runId))) {
      message.warning(`续写已保存，但章节列表刷新失败：${(error as Error).message}`)
    }
  }
}

// 恢复历史版本：后端已把恢复前状态另存为快照，这里只需重载并清空标记
async function onRestored(chapter: Chapter) {
  const context = revisionContext.value
  if (!showRevisions.value || !context || !isCurrentEditorScope(context)
    || chapter.novel_id !== context.novelId || chapter.id !== context.chapterId) return
  const requestId = ++restoreRequestId
  const isCurrent = () => isCurrentEditorScope(context) && requestId === restoreRequestId
  chapterLoadRequestId += 1
  upsertChapter(chapter)
  savedSnapshot.value = { title: chapter.title, directive: chapter.directive, content: chapter.content }
  try {
    await loadChapters()
  } catch (error) {
    if (isCurrent()) message.warning(`版本已恢复，但章节列表刷新失败：${(error as Error).message}`)
  }
  if (!isCurrent()) return
  upsertChapter(chapter)
  if (context.authorRevision !== authorRevision) return
  title.value = chapter.title
  directive.value = chapter.directive
  content.value = chapter.content
  rememberSaved()
  clearMarks()
}

function openRevisionHistory() {
  if (!selectedId.value || saving.value || previewApplying.value || deleting.value || repairApplying.value || !requireSaved('查看版本历史')) return
  revisionContext.value = { ...captureEditorScope(), authorRevision }
  showRevisions.value = true
}

async function generateChapter(
  previousChapterId = '',
  chapterTitle = title.value,
  chapterDirective = directive.value,
  outlineNodeId = '',
) {
  const scope = captureEditorScope()
  const runId = await beginStream()
  if (runId === null || !isCurrentEditorScope(scope) || !isCurrentStream(runId)) return
  clearMarks()
  await streamSSE(
    `/api/v1/novels/${scope.novelId}/chapters/write`,
    {
      title: chapterTitle,
      directive: chapterDirective,
      previous_chapter_id: previousChapterId,
      outline_node_id: outlineNodeId,
    },
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: async (data) => {
        if (!isCurrentStream(runId)) return
        const created = data.chapter as Chapter | undefined
        if (!created?.id || created.novel_id !== scope.novelId) throw new Error('新章已生成，但未收到对应章节详情，请保留输出并核对目录')
        chapterLoadRequestId += 1
        upsertChapter(created)
        adoptChapter(created)
        const createdScope = captureEditorScope()
        const completedRunId = streamRunId, completedRevision = authorRevision
        // 完成回包已被采用；后置目录读取不能再清理另一轮生成的状态。
        message.success('已生成新章')
        try { await loadChapters() }
        catch (error) {
          if (isCurrentEditorScope(createdScope) && streamRunId === completedRunId && authorRevision === completedRevision) {
            message.warning(`新章已保存，但章节列表刷新失败：${(error as Error).message}`)
          }
        }
      },
      onError: (m, code) => failStream(runId, m, code),
    },
  )
}

async function doWrite() {
  if (hasUnverifiedCreation.value) { message.warning('请先核对原草稿保存，或明确另开新章后再生成'); return }
  if (isDirty.value && content.value.trim() && !requireSaved('AI生成新章')) return
  await generateChapter()
}

async function doWriteNext() {
  if (!selectedId.value || saving.value || previewApplying.value || deleting.value || streaming.value || !requireSaved('生成下一章')) return
  if (!isLatestChapter.value) {
    message.warning('只能从最后一章继续生成，请先选择章节列表中的最后一章')
    return
  }
  nextTitle.value = ''
  nextDirective.value = ''
  const context = { ...captureEditorScope(), authorRevision }
  const requestId = ++nextChapterRequestId
  const isCurrent = () => isCurrentEditorScope(context) && context.authorRevision === authorRevision && requestId === nextChapterRequestId
  try {
    const nodes = await api.listOutline(context.novelId)
    if (!isCurrent()) return
    nextOutlineNodes.value = nodes.filter(
      (node) => node.level === 'chapter',
    )
  } catch (error) {
    if (isCurrent()) message.error((error as Error).message)
    return
  }
  const currentIndex = nextOutlineNodes.value.findIndex(
    (node) => node.id === selectedChapter.value?.outline_node_id,
  )
  const nextIndex = currentIndex >= 0 ? currentIndex + 1 : chapters.value.length
  nextOutlineNodeId.value = nextOutlineNodes.value[nextIndex]?.id ?? null
  nextChapterContext.value = context
  showWriteNext.value = true
}

async function confirmWriteNext() {
  const context = nextChapterContext.value
  if (!context || !showWriteNext.value || !isCurrentEditorScope(context) || context.authorRevision !== authorRevision
    || !selectedId.value || saving.value || previewApplying.value || deleting.value || streaming.value) return
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
  const scope = captureEditorScope()
  const runId = await beginStream()
  if (runId === null || !isCurrentEditorScope(scope) || !isCurrentStream(runId)) return
  await streamSSE(
    `/api/v1/novels/${scope.novelId}/chapters/${scope.chapterId}/continue`,
    { directive: directive.value },
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: async (data) => {
        if (!isCurrentStream(runId)) return
        await reloadAndReselect(data.chapter, runId)
        if (!isCurrentStream(runId)) return
        clearStreamOutput()
        streaming.value = false
        message.success('已续写')
      },
      onError: (m, code) => failStream(runId, m, code),
    },
  )
}

function getSelection() {
  const el = contentRef.value
  if (!el) return null
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end <= start || end > content.value.length) return null
  return { passage: content.value.slice(start, end),
    start: Array.from(content.value.slice(0, start)).length, end: Array.from(content.value.slice(0, end)).length }
}

function sameRewriteSource(value: unknown, source: ChapterAuthorFields): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const row = value as Record<string, unknown>
  return row.title === source.title && row.content === source.content && row.directive === source.directive
}

function rewriteCandidate(context: ParagraphRewriteContext, replacement: string): string {
  const original = Array.from(context.source.content)
  return original.slice(0, context.selectionStart).join('') + replacement + original.slice(context.selectionEnd).join('')
}

function isGuardedRewrite(value: unknown, context: ParagraphRewriteContext, replacement: string): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !replacement.trim()) return false
  const row = value as Record<string, unknown>, chapter = row.chapter as Record<string, unknown> | undefined
  return row.type === 'done' && row.preview === true && row.selection_start === context.selectionStart
    && row.selection_end === context.selectionEnd && sameRewriteSource(row.expected_author_fields, context.source)
    && !!chapter && chapter.id === context.chapterId && chapter.novel_id === context.novelId
    && sameRewriteSource(chapter, context.source) && row.content === rewriteCandidate(context, replacement)
}

async function doRewrite() {
  if (!selectedId.value || streaming.value || !requireSaved('改写选中内容')) return
  const selection = getSelection()
  if (!selection?.passage.trim()) {
    message.error('请先在正文中选中要改写的段落')
    return
  }
  const context: ParagraphRewriteContext = { ...captureEditorScope(), authorRevision, source: authorSnapshot(),
    selectionStart: selection.start, selectionEnd: selection.end }
  const passage = selection.passage
  const runId = await beginStream()
  if (runId === null || !isCurrentEditorScope(context) || context.authorRevision !== authorRevision || !isCurrentStream(runId)) return
  previewContext.value = context
  const isCurrent = () => isCurrentStream(runId) && isCurrentEditorScope(context) && context.authorRevision === authorRevision
  previewPassage.value = passage
  previewNew.value = ''
  previewFullContent.value = ''
  previewGuarded.value = false
  previewError.value = ''
  previewRefreshNeeded.value = false
  const payload: ParagraphRewriteRequest = { passage, intent: rewriteIntent.value, preview: true,
    expected_author_fields: { ...context.source }, selection_start: context.selectionStart, selection_end: context.selectionEnd }
  await streamSSE(
    `/api/v1/novels/${context.novelId}/chapters/${context.chapterId}/rewrite`,
    payload,
    {
      onToken: (t) => { if (isCurrentStream(runId)) streamText.value += t },
      onDone: (obj) => {
        if (!isCurrentStream(runId)) return
        if (!isCurrent()) { failStream(runId, '生成期间作者输入已改变，请保留候选输出并重新核对'); return }
        streaming.value = false
        previewNew.value = streamText.value
        previewFullContent.value = typeof obj?.content === 'string' ? obj.content : rewriteCandidate(context, previewNew.value)
        previewGuarded.value = isGuardedRewrite(obj, context, previewNew.value)
        if (!previewGuarded.value) {
          previewError.value = '改写回应的原稿、选区或候选全文未得到完整核对；正文未保存。请复制候选并核对服务版本与原稿。'
          previewRefreshNeeded.value = true
          message.warning(previewError.value)
        }
        clearStreamOutput()
        showPreview.value = true
      },
      onError: (m, code) => {
        if (!isCurrentStream(runId)) return
        failStream(runId, m, code)
        if (isCurrentEditorScope(context) && code === 'chapter_write_conflict') {
          previewContext.value = context
          previewPassage.value = passage
          previewNew.value = streamText.value
          previewFullContent.value = rewriteCandidate(context, previewNew.value)
          previewGuarded.value = false
          previewError.value = '已保存原稿已变化，本次未生成可应用预览；当前输入仍保留，请重新读取章节核对。'
          previewRefreshNeeded.value = true
          showPreview.value = true
        }
      },
    },
  )
}

// 应用改写：仅此时才落库，来源记为 rewrite_apply
async function applyPreview() {
  const context = previewContext.value
  if (!context || !showPreview.value || !isCurrentEditorScope(context) || context.authorRevision !== authorRevision
    || !previewGuarded.value || previewRefreshNeeded.value || previewRefreshing.value
    || previewApplying.value || saving.value || streaming.value || hasIncompleteOutput.value || !previewFullContent.value.trim()) return
  const requestId = ++previewApplyRequestId
  restoreRequestId += 1
  const submittedContent = previewFullContent.value
  const evidence = { passage: previewPassage.value, replacement: previewNew.value, content: submittedContent }
  const isCurrent = () => isCurrentEditorScope(context) && requestId === previewApplyRequestId
  previewApplying.value = true
  try {
    const updated = await api.updateChapter(context.novelId, context.chapterId!, {
      content: submittedContent,
      revision_source: 'rewrite_apply',
      expected_author_fields: { ...context.source },
    })
    if (!isCurrent()) return
    if (updated.id !== context.chapterId || updated.novel_id !== context.novelId) {
      message.warning('改写保存响应与原章节不一致，请保留编辑稿并刷新核对')
      return
    }
    chapterLoadRequestId += 1
    upsertChapter(updated)
    savedSnapshot.value = { title: updated.title, directive: updated.directive, content: updated.content }
    if (context.authorRevision === authorRevision) {
      title.value = updated.title
      directive.value = updated.directive
      content.value = updated.content
    }
    invalidatePreview(false)
    try {
      await loadChapters()
      if (!isCurrent()) return
      message.success('已应用改写')
    } catch (error) {
      if (isCurrent()) message.warning(`已应用改写，但章节列表刷新失败：${(error as Error).message}`)
    }
  } catch (e) {
    if (isCurrent()) {
      const error = e as Error & { status?: number; code?: string }
      if (error.status === 409 && error.code === 'chapter_write_conflict') {
        previewContext.value = context
        previewPassage.value = evidence.passage
        previewNew.value = evidence.replacement
        previewFullContent.value = evidence.content
        previewGuarded.value = false
        previewRefreshNeeded.value = true
        previewError.value = '已保存原稿已变化，本次未应用改写。候选、原文和当前输入仍保留，请先复制，再重新读取章节核对并重新生成。'
        showPreview.value = true
        message.warning(previewError.value)
      } else message.error(error.message)
    }
  } finally {
    if (isCurrent()) previewApplying.value = false
  }
}

function invalidatePreview(invalidateApply = true) {
  if (invalidateApply) {
    previewApplyRequestId += 1
    previewApplying.value = false
  }
  previewContext.value = null
  previewRefreshRequestId += 1
  previewRefreshing.value = false
  previewGuarded.value = false
  previewError.value = ''
  previewRefreshNeeded.value = false
  showPreview.value = false
  previewPassage.value = ''
  previewNew.value = ''
  previewFullContent.value = ''
}

function handlePreviewVisibility(visible: boolean) {
  if (!visible && !previewApplying.value && !previewRefreshing.value) invalidatePreview()
}

function discardPreview() {
  if (previewApplying.value || previewRefreshing.value) return
  invalidatePreview()
  message.info('已放弃本次改写')
}

async function copyRewritePreview(text: string) {
  const scope = captureEditorScope(), context = previewContext.value
  try { await navigator.clipboard.writeText(text); if (isCurrentEditorScope(scope) && context === previewContext.value) message.success('已复制改写稿件') }
  catch { if (isCurrentEditorScope(scope) && context === previewContext.value) message.warning('自动复制失败，请在只读原文或候选输入框中手动选中复制。') }
}

async function refreshPreviewChapter() {
  const context = previewContext.value
  if (!context || !isCurrentEditorScope(context) || previewApplying.value || previewRefreshing.value || saving.value || streaming.value) return
  const revision = authorRevision, requestId = ++previewRefreshRequestId
  const isCurrent = () => isCurrentEditorScope(context) && revision === authorRevision && requestId === previewRefreshRequestId
  previewRefreshing.value = true
  try {
    if (!await askDiscard('重新读取会替换当前编辑稿并关闭本次候选。请先复制需要保留的原文、候选和新输入，确定读取已保存章节吗？', '读取已保存章节') || !isCurrent()) return
    const fresh = await api.getChapter(context.novelId, context.chapterId!)
    if (!isCurrent()) return
    if (fresh.id !== context.chapterId || fresh.novel_id !== context.novelId
      || !['title', 'content', 'directive'].every(field => typeof fresh[field as keyof Chapter] === 'string')) {
      previewError.value = '章节读取回应与原来源不一致，当前输入与候选仍保留'; return
    }
    adoptChapter(fresh)
    upsertChapter(fresh)
    message.info('已读取章节，请核对正文，重新选择段落并生成改写')
  } catch (error) {
    if (isCurrent()) previewError.value = `章节读取未完成，当前输入与候选仍保留：${(error as Error).message}`
  } finally { if (isCurrent()) previewRefreshing.value = false }
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
    if (!await selectChapter(chapter, true)) return
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
      chapterLoadRequestId += 1
      upsertChapter(updated)
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
  const scope = captureEditorScope(), epoch = qualityEpoch
  const checkedContent = content.value
  const requestId = ++qualityRequests.cliche
  const isCurrent = () => isCurrentEditorScope(scope) && epoch === qualityEpoch && requestId === qualityRequests.cliche
  try {
    const res = await api.scanCliche(scope.novelId, checkedContent)
    if (!isCurrent()) return
    cliche.value = res
    clicheHits.value = res.hits
    if (res.total) message.warning(`命中 ${res.total} 处套路表达，密度 ${res.density}/千字`)
    else message.success('未命中套路表达')
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  }
}

const DEEP_COUNT_KEYS = ['chunks_total', 'chunks_eligible', 'chunks_attempted', 'chunks_checked',
  'chunks_failed', 'chunks_skipped', 'chunks_skipped_rule', 'chunks_skipped_no_entity', 'chunks_unchecked'] as const

function readDeepCheckResult(value: unknown, checkedContent: string) {
  const raw = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  const integer = (item: unknown): item is number => typeof item === 'number' && Number.isSafeInteger(item) && item >= 0
  const counts = Object.fromEntries(DEEP_COUNT_KEYS.map((key) => [key, integer(raw[key]) ? raw[key] : 0])) as Pick<DeepCheckResult, typeof DEEP_COUNT_KEYS[number]>
  const coverage = DEEP_COUNT_KEYS.every((key) => integer(raw[key]))
    && counts.chunks_total === counts.chunks_eligible + counts.chunks_skipped
    && counts.chunks_eligible === counts.chunks_attempted + counts.chunks_unchecked
    && counts.chunks_attempted === counts.chunks_checked + counts.chunks_failed
    && counts.chunks_skipped === counts.chunks_skipped_rule + counts.chunks_skipped_no_entity
    && counts.chunks_attempted === Math.min(counts.chunks_eligible, 8) && typeof raw.truncated === 'boolean'
    && raw.truncated === (counts.chunks_unchecked > 0)
  const status: DeepCheckResult['status'] = !counts.chunks_attempted ? 'skipped'
    : !counts.chunks_checked ? 'failed' : counts.chunks_failed || counts.chunks_unchecked ? 'partial' : 'complete'
  const object = (item: unknown): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item)
  const text = (item: unknown): item is string => typeof item === 'string' && !!item.trim()
  const semantic = (item: unknown): item is DeepCheckConflict => object(item) && item.rule === 'semantic'
    && text(item.type) && text(item.excerpt) && checkedContent.includes(item.excerpt) && text(item.reason)
    && ['high', 'medium', 'low'].includes(item.severity as string) && integer(item.chunk_index)
    && (!coverage || item.chunk_index < counts.chunks_total)
  const contentLength = Array.from(checkedContent).length
  const rule = (item: unknown): item is Conflict => object(item) && text(item.rule) && text(item.message)
    && integer(item.start) && integer(item.end) && item.start < item.end && item.end <= contentLength && text(item.entity_id)
  const semanticRows = Array.isArray(raw.conflicts) ? raw.conflicts : []
  const ruleRows = Array.isArray(raw.rule_conflicts) ? raw.rule_conflicts : []
  const warningRows = Array.isArray(raw.warnings) ? raw.warnings : []
  const validConflicts = semanticRows.filter(semantic)
  const validRules = ruleRows.filter(rule)
  const warnings = warningRows.filter(text)
  const confirmed = coverage && raw.status === status
    && Array.isArray(raw.conflicts) && validConflicts.length === semanticRows.length
    && Array.isArray(raw.rule_conflicts) && validRules.length === ruleRows.length
    && Array.isArray(raw.warnings) && warnings.length === warningRows.length
    && new Set(validConflicts.map((item) => item.chunk_index)).size <= counts.chunks_checked
    && (validRules.length > 0) === (counts.chunks_skipped_rule > 0)
  if (!confirmed) warnings.push('检查回应缺少完整状态或包含不一致数据，本次结果未得到完整确认；请核对服务版本并重试或人工校对。')
  const result: DeepCheckResult = { ...counts, status, conflicts: validConflicts, rule_conflicts: validRules,
    truncated: raw.truncated === true, warnings }
  return { result, confirmed, coverage }
}

// 语义深度检查（每个有效分块一次模型调用，最多 8 次，结果不落库）
async function doDeepCheck() {
  if (!content.value.trim() || deepLoading.value) return
  const scope = captureEditorScope(), epoch = qualityEpoch
  const checkedContent = content.value
  const requestId = ++qualityRequests.deep
  const isCurrent = () => isCurrentEditorScope(scope) && epoch === qualityEpoch && requestId === qualityRequests.deep
  deepConflicts.value = []
  deepMeta.value = null
  deepResultConfirmed.value = false
  deepCoverageConfirmed.value = false
  deepRequestError.value = ''
  deepLoading.value = true
  try {
    const res = await api.checkConsistencyDeep(scope.novelId, checkedContent)
    if (!isCurrent()) return
    const view = readDeepCheckResult(res, checkedContent)
    deepConflicts.value = view.result.conflicts
    deepMeta.value = view.result
    deepResultConfirmed.value = view.confirmed
    deepCoverageConfirmed.value = view.coverage
    if (!view.confirmed) message.warning('深度检查结果未得到完整确认，请核对返回提示并重试或人工校对')
    else if (view.result.status !== 'complete') message.warning(`${DEEP_STATUS_LABEL[view.result.status]}：已完成 ${view.result.chunks_checked} 块，失败 ${view.result.chunks_failed} 块，未查 ${view.result.chunks_unchecked} 块；请核对检查范围${view.result.rule_conflicts.length ? '与规则冲突' : ''}`)
    else if (deepConflicts.value.length || deepRuleConflicts.value.length) message.warning(`语义层发现 ${deepConflicts.value.length} 处冲突，规则层发现 ${deepRuleConflicts.value.length} 处冲突`)
    else message.success('已检查范围内未发现明显冲突，仍需人工校对')
  } catch (e) {
    if (isCurrent()) {
      deepRequestError.value = (e as Error).message
      deepMeta.value = { ...readDeepCheckResult(null, checkedContent).result, warnings: [`检查请求未完成：${deepRequestError.value}`] }
      message.error(`深度检查未完成：${deepRequestError.value}`)
    }
  } finally {
    if (isCurrent()) deepLoading.value = false
  }
}

// 按需检查当前编辑稿与绑定大纲的契合度；不保存、不修改正文。
async function doOutlineCheck() {
  if (outlineCheckLoading.value || !selectedId.value || !selectedChapter.value?.outline_node_id || !content.value.trim()) return
  const chapterId = selectedId.value
  const checkedContent = content.value
  const scope = captureEditorScope(), epoch = qualityEpoch
  const requestId = ++qualityRequests.outline
  const isCurrent = () => isCurrentEditorScope(scope) && epoch === qualityEpoch && requestId === qualityRequests.outline
  outlineCheckLoading.value = true
  outlineCheckResult.value = null
  try {
    const result = await api.checkChapterOutline(scope.novelId, chapterId, checkedContent)
    if (!isCurrent()) return
    outlineCheckResult.value = result
    if (result.issues.length) message.warning(`大纲契合检查发现 ${result.issues.length} 处待核对项`)
    else message.success('未发现明显大纲偏差，仍请人工校对')
  } catch (e) {
    if (isCurrent()) {
      message.error((e as Error).message)
    }
  } finally {
    if (isCurrent()) outlineCheckLoading.value = false
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
  if (!props.novelId || saving.value || previewApplying.value || deleting.value || streaming.value || repairApplying.value
    || (!isDirty.value && !hasUnverifiedCreation.value)) return
  if (creationBlocked.value) { message.warning('原创建意图已被拒绝，请保留稿件核对目录，或明确另开新章'); return }
  let scope = captureEditorScope()
  let requestId = ++saveRequestId
  restoreRequestId += 1
  const revision = authorRevision
  const submitted = authorSnapshot()
  let intent = scope.chapterId ? null : activeCreation.value
  if (!scope.chapterId && !intent) {
    try { intent = newChapterCreation(scope.novelId, submitted) }
    catch { message.warning('无法建立安全的草稿保存记录，请先复制稿件并检查浏览器支持；本次未提交。'); return }
    activeCreation.value = intent
    creationBindingRevision = revision
    persistCreation(intent)
  }
  const isCurrent = () => isCurrentEditorScope(scope) && requestId === saveRequestId
  saving.value = true
  try {
    const updated = scope.chapterId
      ? await api.updateChapter(scope.novelId, scope.chapterId, { ...submitted, revision_source: 'manual_save' })
      : await api.createChapter(scope.novelId, JSON.parse(intent!.payloadJson))
    if (!isCurrent()) return
    if (!updated.id || updated.novel_id !== scope.novelId || (scope.chapterId && updated.id !== scope.chapterId)) {
      message.warning('保存响应与原作品章节不一致，请保留文本并刷新核对保存结果')
      return
    }
    let preserveCreationDraft = false
    let creationReplayed = false
    if (intent) {
      if (!isChapterCreationResult(updated)) {
        message.warning('未收到完整的草稿创建确认，保存结果尚未核对；原提交与编辑稿已保留，请核对原保存。')
        return
      }
      const result = updated
      preserveCreationDraft = intent.edited || authorRevision !== creationBindingRevision || result.author_changed_since_creation
      creationReplayed = result.creation_replayed
      if (activeCreation.value?.requestId !== intent.requestId) return
      if (!removeChapterCreation(intent.novelId, intent.requestId)) {
        creationStorageWarning.value = '草稿已确认保存，但浏览器核对记录清理失败；再次核对会读取原章节，不会另建。'
        message.warning(creationStorageWarning.value)
      } else {
        creationRecords.value = creationRecords.value.filter((record) => record.requestId !== intent!.requestId)
        const key = creationKey(intent.novelId, intent.requestId)
        creationMemory.delete(key)
        creationPersisted.delete(key)
        creationVolatile.delete(key)
        if (!creationRecords.value.length) creationStorageWarning.value = ''
      }
      activeCreation.value = null
    }
    // 保存回应是基线；等待期间的新输入仍属于作者，不能被回应覆盖或标成已保存。
    chapterLoadRequestId += 1
    upsertChapter(updated)
    savedSnapshot.value = { title: updated.title, directive: updated.directive, content: updated.content }
    if (revision === authorRevision && !preserveCreationDraft) {
      title.value = updated.title
      directive.value = updated.directive
      content.value = updated.content
    }
    if (!scope.chapterId) {
      selectedId.value = updated.id
      selectedDetail.value = updated
      scope = captureEditorScope()
      requestId = ++saveRequestId
      saving.value = true
    }
    try {
      await loadChapters()
      if (!isCurrent()) return
      upsertChapter(updated)
      message.success(intent ? (creationReplayed ? '已核对原保存，未另建章节' : '草稿已保存') : '已保存')
      if (preserveCreationDraft) message.warning('原章节已确认，当前编辑稿已保留；请核对差异，需要保存新改动时再点击“保存”。')
    } catch (error) {
      if (isCurrent()) {
        message.warning(`已保存，但章节列表刷新失败：${(error as Error).message}`)
        if (preserveCreationDraft) message.warning('原章节已确认，当前编辑稿已保留；请核对差异，需要保存新改动时再点击“保存”。')
      }
    }
  } catch (e) {
    if (isCurrent()) {
      if (intent && activeCreation.value?.requestId === intent.requestId) {
        const failure = e as Error & { code?: string }
        if (['chapter_creation_deleted', 'chapter_creation_payload_conflict', 'chapter_creation_scope_conflict'].includes(failure.code ?? '')) {
          intent.blockedCode = failure.code
          persistCreation(intent)
          message.warning(`${failure.message}；当前稿件仍保留，原请求不会换标识重新创建。请核对目录或明确另开新章。`)
        } else message.warning(`保存结果尚未核对：${failure.message}。请点击“核对原保存”沿用原提交确认，等待期间的新编辑仍保留。`)
      } else message.error((e as Error).message)
    }
  } finally {
    if (isCurrent()) saving.value = false
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
  const scope = captureEditorScope(), epoch = summaryEpoch, revision = summaryRevision
  const requestId = ++summaryRequestId
  const isCurrent = () => isCurrentEditorScope(scope) && epoch === summaryEpoch && requestId === summaryRequestId && showSummary.value
  summarySaving.value = true
  try {
    const updated = await api.updateChapter(novelId, chapterId, { summary: submittedSummary })
    if (!isCurrent()) return
    if (updated.id !== chapterId || updated.novel_id !== novelId) { message.warning('摘要保存响应与原章节不一致，请刷新核对'); return }
    chapterLoadRequestId += 1
    invalidateChapterDetail()
    if (selectedChapter.value) {
      selectedDetail.value = { ...selectedChapter.value, summary: updated.summary, updated_at: updated.updated_at }
    }
    const metadata = chapters.value.find((c) => c.id === chapterId)
    if (metadata) metadata.updated_at = updated.updated_at
    // 只同步摘要，保留编辑区尚未保存的正文、标题和章前指令。
    savedSummary.value = updated.summary
    if (revision === summaryRevision) summaryDraft.value = updated.summary
    message.success('摘要已保存')
  } catch (e) {
    if (isCurrent()) {
      message.error((e as Error).message)
    }
  } finally {
    if (isCurrent()) summarySaving.value = false
  }
}

async function deleteChapter() {
  if (!selectedId.value || deleting.value || saving.value || previewApplying.value || repairApplying.value || streaming.value) return
  let scope = captureEditorScope()
  let requestId = ++deleteRequestId
  restoreRequestId += 1
  const chapterId = selectedId.value
  const novelId = props.novelId, epoch = pageEpoch
  const revision = authorRevision
  const isCurrent = () => isCurrentEditorScope(scope) && requestId === deleteRequestId
  const deletedIndex = chapters.value.findIndex((chapter) => chapter.id === selectedId.value)
  deleting.value = true
  try {
    await api.deleteChapter(scope.novelId, chapterId)
    if (unmounted || novelId !== props.novelId || epoch !== pageEpoch) return
    chapterLoadRequestId += 1
    invalidateChapterDetail()
    chapters.value = chapters.value.filter((chapter) => chapter.id !== chapterId)
    if (!isCurrent()) {
      // 同作品换章后仍移除已删记录；当前新输入不受旧选择回调影响。
      if (selectedId.value === chapterId) {
        selectedId.value = null
        selectedDetail.value = null
        savedSnapshot.value = { title: '', directive: '', content: '' }
      }
      return
    }
    showDelete.value = false
    const keepNewEdits = revision !== authorRevision
    if (keepNewEdits) {
      // 删除确认后的新增文字不能随删除丢弃，转为尚未落库的新章草稿。
      selectedId.value = null
      selectedDetail.value = null
      savedSnapshot.value = { title: '', directive: '', content: '' }
    } else {
      const next = chapters.value[Math.min(deletedIndex, chapters.value.length - 1)]
      if (next) {
        // 已删章的正文不能继续作为可保存的章节；邻章读取失败时保留为新草稿。
        selectedId.value = null
        selectedDetail.value = null
        savedSnapshot.value = { title: '', directive: '', content: '' }
        await selectChapter(next, true)
      }
      else await startNew(true)
    }
    scope = captureEditorScope()
    requestId = ++deleteRequestId
    deleting.value = true
    try {
      await loadChapters()
      if (!isCurrent()) return
      chapters.value = chapters.value.filter((chapter) => chapter.id !== chapterId)
      if (keepNewEdits) message.warning('章节已删除，删除过程中新增的文字已保留为未保存草稿')
      else message.success('章节已删除')
    } catch (error) {
      if (isCurrent()) message.warning(`章节已删除，但章节列表刷新失败${keepNewEdits ? '；新增文字已保留为未保存草稿' : ''}：${(error as Error).message}`)
    }
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  } finally {
    if (isCurrent()) deleting.value = false
  }
}

async function doConsistency() {
  if (!content.value.trim()) return
  const scope = captureEditorScope(), epoch = qualityEpoch
  const checkedContent = content.value
  const requestId = ++qualityRequests.consistency
  const isCurrent = () => isCurrentEditorScope(scope) && epoch === qualityEpoch && requestId === qualityRequests.consistency
  try {
    const res = await api.checkConsistency(scope.novelId, checkedContent)
    if (!isCurrent()) return
    conflicts.value = res.conflicts
    if (res.conflicts.length) message.warning(`发现 ${res.conflicts.length} 处冲突`)
    else message.success('未发现冲突')
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  }
}

async function doSettle() {
  if (!props.novelId || saving.value || previewApplying.value || deleting.value || summarySaving.value || repairApplying.value || settleLoading.value || settleApplying.value) return
  if (streaming.value || hasIncompleteOutput.value) {
    message.warning('请先处理未完成输出，再定稿沉淀')
    return
  }
  if (!selectedId.value || !requireSaved('定稿沉淀')) return
  invalidateSettle()
  const context: SettleContext = {
    novelId: props.novelId, chapterId: selectedId.value, epoch: settleEpoch,
    title: title.value, directive: directive.value, content: content.value,
    sourceFingerprint: null,
  }
  const requestId = ++settleRequestId
  const isCurrent = () => isCurrentSettle(context) && requestId === settleRequestId
  settleLoading.value = true
  try {
    const res = await api.settleChapter(context.novelId, context.chapterId, {
      title: context.title, content: context.content, directive: context.directive,
    })
    if (!isCurrent()) return
    const sourceFingerprint = validSettleFingerprint(res.source_fingerprint) ? res.source_fingerprint : null
    settleContext.value = Object.freeze({ ...context, sourceFingerprint })
    settleSummary.value = res.summary
    suggestions.value = res.suggestions
    checkedSuggestion.value = res.suggestions.map(() => true)
    settleDataTexts.value = res.suggestions.map((item) => JSON.stringify(item.data, null, 2))
    settleCompleted.value = res.suggestions.map(() => false)
    settleErrors.value = {}
    settledCount.value = 0
    settleWarnings.value = (res.warnings ?? []).filter((warning) => typeof warning === 'string' && warning.trim())
    if (!sourceFingerprint) settleWarnings.value.push('未取得可核对的章节来源，不能确认建议；请更新后端并重新定稿。')
    showSettle.value = true
    if (selectedChapter.value) {
      upsertChapter({ ...selectedChapter.value, summary: res.summary, status: 'finalized',
        drift_score: typeof res.drift === 'number' && Number.isFinite(res.drift) ? res.drift : null })
    }
    const metadataRequestId = ++chapterLoadRequestId
    // 定稿只改变摘要/状态等派生字段；重读目录不能覆盖等待期间的作者正文。
    try {
      const loaded = await api.listChapterMetadata(context.novelId)
      if (isCurrent() && metadataRequestId === chapterLoadRequestId) {
        chapters.value = loaded
        syncSelectedMetadata()
      }
    } catch (error) {
      if (isCurrent()) {
        const warning = `定稿沉淀已完成，但章节列表刷新失败：${(error as Error).message}`
        settleWarnings.value.push(warning)
        message.warning(warning)
      }
    }
  } catch (e) {
    if (!isCurrent()) return
    if (isSettleSourceChanged(e)) {
      settleContext.value = Object.freeze(context)
      settleRefreshNeeded.value = true
      message.warning('当前编辑稿与已保存章节的来源已变化，本次未开始定稿。请保留文本，重新读取章节核对后再定稿。')
    } else message.error((e as Error).message)
  } finally {
    if (isCurrent()) settleLoading.value = false
  }
}

function isCurrentSettle(context: SettleContext): boolean {
  return !unmounted && context.epoch === settleEpoch
    && props.novelId === context.novelId && selectedId.value === context.chapterId
    && title.value === context.title && directive.value === context.directive && content.value === context.content
}

function invalidateSettle(clearPreview = true) {
  settleEpoch += 1
  settleRequestId += 1
  settleApplyRequestId += 1
  settleContext.value = null
  settleLoading.value = false
  settleApplying.value = false
  showSettle.value = false
  settlePendingSubmission.value = null
  settleRefreshNeeded.value = false
  settleReceiptUnavailable.value = false
  if (clearPreview) {
    settleSummary.value = ''
    suggestions.value = []
    checkedSuggestion.value = []
    settleDataTexts.value = []
    settleCompleted.value = []
    settleErrors.value = {}
    settleWarnings.value = []
    settledCount.value = 0
    settleReceipts.value = {}
    settleConfirmedTexts.value = {}
  }
}

function handleSettleVisibility(visible: boolean) {
  if (visible) showSettle.value = true
  else if (!settleApplying.value) invalidateSettle(false)
}

function validSettleFingerprint(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
}

function isSettleSourceChanged(error: unknown): boolean {
  const failure = error as Error & { status?: number; code?: string }
  return failure?.code === 'settle_source_changed' || failure?.status === 409
}

function settleReceiptLabel(index: number): string {
  const row = settleReceipts.value[index]
  return row?.deleted ? '原目标已删除，本次未重建' : row?.replayed ? '此前已确认' : '本次新建'
}

function settleReceiptSummary(): string {
  return `新建 ${settledNewCount.value} 条，已确认 ${settledReplayedCount.value} 条`
    + (settledDeletedCount.value ? `；其中 ${settledDeletedCount.value} 条原目标已删除，本次未重建` : '')
}

// 不完整回执不能推测哪些写入成功，也不能向旧后端盲目重复提交。
function normalizeSettleReceipts(result: SettleApplyResult, picked: Suggestion[]): {
  created: (SettleReceipt & { index: number })[]; failed: ApplicationFailure[]
} | null {
  if (!result || !Array.isArray(result.created) || (result.failed !== undefined && !Array.isArray(result.failed))
    || !Number.isInteger(result.new_count) || !Number.isInteger(result.replayed_count)) return null
  const failed = result.failed ?? []
  const validIndex = (index: number) => Number.isInteger(index) && index >= 0 && index < picked.length
  if (failed.some((row) => !row || typeof row !== 'object' || !validIndex(row.index) || typeof row.error !== 'string' || !row.error
    || row.type !== picked[row.index]?.type)) return null
  const failedIndices = new Set(failed.map((row) => row.index))
  const remaining = picked.map((_, index) => index).filter((index) => !failedIndices.has(index))
  if (result.created.some((row) => !row || typeof row !== 'object')) return null
  const created = result.created.map((row, index) => ({ ...row, index: row.index ?? remaining[index] }))
  if (created.some((row) => !validIndex(row.index) || row.type !== picked[row.index]?.type
    || typeof row.id !== 'string' || !row.id || typeof row.receipt_id !== 'string' || !row.receipt_id
    || typeof row.replayed !== 'boolean' || typeof row.deleted !== 'boolean' || (row.deleted && !row.replayed))) return null
  const indices = [...failed.map((row) => row.index), ...created.map((row) => row.index)]
  if (new Set(indices).size !== indices.length || indices.length !== picked.length
    || result.new_count !== created.filter((row) => !row.replayed).length
    || result.replayed_count !== created.filter((row) => row.replayed).length) return null
  return { created, failed }
}

async function refreshSettleChapter() {
  const context = settleContext.value
  if (!context || !isCurrentSettle(context) || settleApplying.value || settleLoading.value || !requireSaved('读取已保存章节')) return
  const requestId = ++settleRequestId
  const isCurrent = () => isCurrentSettle(context) && requestId === settleRequestId
  settleLoading.value = true
  try {
    await loadChapters()
    if (!isCurrent()) return
    const chapter = chapters.value.find((row) => row.id === context.chapterId)
    if (!chapter) { message.warning('原章节已不存在，请保留文本并核对目录'); return }
    if (!await selectChapter(chapter, true)) return
    invalidateSettle()
    message.info('已读取章节，请核对正文后重新定稿')
  } catch (error) {
    if (isCurrent()) message.warning(`章节读取未完成，当前文本与候选仍保留：${(error as Error).message}`)
  } finally {
    if (isCurrent()) settleLoading.value = false
  }
}

async function confirmSettle() {
  const context = settleContext.value
  if (!showSettle.value || !context || !isCurrentSettle(context) || settleApplying.value || settleLoading.value || settleConfirmBlocked.value) return
  const pending = settlePendingSubmission.value
  const sourceIndices = pending?.sourceIndices ?? suggestions.value.map((_, i) => i)
    .filter((i) => checkedSuggestion.value[i] && !settleCompleted.value[i])
  if (!sourceIndices.length) {
    if (settledCount.value) handleSettleVisibility(false)
    else message.warning('请选择需要落库的建议')
    return
  }
  let picked: Suggestion[]
  try {
    picked = pending ? JSON.parse(pending.payloadJson) : sourceIndices.map((index) => {
      const data = JSON.parse(settleDataTexts.value[index] ?? JSON.stringify(suggestions.value[index].data))
      if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error('建议数据必须是 JSON 对象')
      return { ...suggestions.value[index], data }
    })
  } catch (error) {
    message.error((error as Error).message || '建议数据 JSON 格式错误')
    return
  }
  const sourceFingerprint = context.sourceFingerprint!
  const submission: SettleSubmission = pending ?? {
    sourceIndices: [...sourceIndices], sourceFingerprint, payloadJson: JSON.stringify(picked),
    submittedTexts: Object.fromEntries(sourceIndices.map((index) => [index, settleDataTexts.value[index]])),
    editorRevision: settleEditorRevision,
  }
  if (submission.sourceFingerprint !== sourceFingerprint) return
  const requestId = ++settleApplyRequestId
  const isCurrent = () => isCurrentSettle(context) && requestId === settleApplyRequestId && showSettle.value
    && settleContext.value === context && context.sourceFingerprint === sourceFingerprint
  settleApplying.value = true
  settlePendingSubmission.value = submission
  try {
    const res = await api.settleApply(context.novelId, context.chapterId, JSON.parse(submission.payloadJson), sourceFingerprint)
    if (!isCurrent()) return
    const receipts = normalizeSettleReceipts(res, picked)
    if (!receipts) {
      settleReceiptUnavailable.value = true
      message.warning('未收到完整的确认回执，不能判断本次新建或已确认的项目。请保留候选并核对后端版本和保存结果，不要重复提交。')
      return
    }
    settlePendingSubmission.value = null
    const editedWhileApplying = settleEditorRevision !== submission.editorRevision
    settleErrors.value = {}
    const failed = receipts.failed
    for (const item of failed) {
      const index = sourceIndices[item.index]
      if (index !== undefined) settleErrors.value[index] = item.error
    }
    for (const row of receipts.created) {
      const source = sourceIndices[row.index]
      if (source !== undefined) {
        settleReceipts.value[source] = row
        settleConfirmedTexts.value[source] = submission.submittedTexts[source]
        settleCompleted.value[source] = true
        checkedSuggestion.value[source] = false
      }
    }
    settledCount.value = Object.keys(settleReceipts.value).length
    if (failed.some((row) => row.code === 'settle_source_changed')) {
      settleRefreshNeeded.value = true
      message.warning(`${settleReceiptSummary()}；剩余建议的章节来源已变化，请保留候选，重新读取核对并定稿`)
      return
    }
    if (failed.length) {
      message.warning(`${settleReceiptSummary()}，${failed.length} 条未确认；请修正标出的数据后重试`)
      return
    }
    if (!receipts.created.length) {
      message.warning('本次未确认任何建议，请核对后重试')
      return
    }
    if (editedWhileApplying || settledDeletedCount.value) {
      const text = settleReceiptSummary() + (editedWhileApplying ? '；等待期间的新编辑已保留，已确认项以原提交为准，请核对后继续' : '')
      if (settledDeletedCount.value) message.warning(text)
      else message.success(text)
      return
    }
    message.success(settleReceiptSummary())
    // 已取得完整回执后的内部完成，不受请求在途时的用户关闭保护限制。
    invalidateSettle(false)
  } catch (e) {
    if (!isCurrent()) return
    const failure = e as Error & { status?: number }
    if (isSettleSourceChanged(e)) {
      settlePendingSubmission.value = null
      settleRefreshNeeded.value = true
      message.warning('章节来源已变化，本次未确认剩余建议。候选与当前文本已保留，请重新读取章节核对后重新定稿。')
    } else if (failure.status && failure.status >= 400 && failure.status < 500 && failure.status !== 408 && failure.status !== 429) {
      settlePendingSubmission.value = null
      message.error(failure.message)
    } else {
      message.warning(`未收到确认回执，保存结果尚未核对：${failure.message}。重试将沿用原提交的建议和来源，等待期间的新编辑仍保留。`)
    }
  } finally {
    if (isCurrent()) settleApplying.value = false
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

watch(() => [props.novelId, selectedId.value], invalidateEditorBinding, { flush: 'sync' })
watch(
  () => props.novelId,
  () => {
    pageEpoch += 1
    chapterLoadRequestId += 1
    chapters.value = []
    creationRecords.value = []
    creationStorageWarning.value = ''
    startNew(true)
    refreshCreationRecords()
    if (props.novelId) {
      const epoch = pageEpoch
      loadChapters().catch((error) => {
        if (!unmounted && epoch === pageEpoch) message.error(`章节读取失败：${(error as Error).message}`)
      })
    }
  },
  { immediate: true, flush: 'sync' },
)
watch(() => [title.value, content.value, directive.value], () => {
  authorRevision += 1
  if (activeCreation.value) {
    activeCreation.value.draft = authorSnapshot()
    activeCreation.value.edited = activeCreation.value.edited || authorRevision !== creationBindingRevision
    if (activeCreation.value.blockedCode !== 'local_record_discarded') persistCreation(activeCreation.value)
  }
  invalidateChapterDetail()
  invalidatePreview(false)
  nextChapterRequestId += 1
  nextChapterContext.value = null
  showWriteNext.value = false
}, { flush: 'sync' })
watch(() => [props.novelId, selectedId.value, showSummary.value], () => {
  summaryEpoch += 1
  summaryRequestId += 1
  summarySaving.value = false
}, { flush: 'sync' })
watch(summaryDraft, () => { summaryRevision += 1 }, { flush: 'sync' })
watch(() => [props.novelId, selectedId.value, title.value, content.value, directive.value, selectedChapter.value?.outline_node_id],
  clearMarks, { flush: 'sync' })

watch(hasUnsavedChanges, (dirty) => emit('dirty-change', dirty), { immediate: true })
watch(
  () => [props.novelId, selectedId.value, title.value, content.value, directive.value],
  () => invalidateSettle(),
  { flush: 'sync' },
)
watch(
  () => JSON.stringify([suggestions.value, checkedSuggestion.value, settleDataTexts.value]),
  () => { settleEditorRevision += 1 },
  { flush: 'sync' },
)
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
  saveRequestId += 1
  saving.value = false
  chapterLoadRequestId += 1
  invalidateChapterDetail()
  clearMarks()
  deleteRequestId += 1
  deleting.value = false
  summaryRequestId += 1
  summarySaving.value = false
  restoreRequestId += 1
  nextChapterRequestId += 1
  nextChapterContext.value = null
  revisionContext.value = null
  invalidatePreview()
  streamRunId += 1
  invalidateRepair()
  invalidateSettle()
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
            <span v-if="chapterLoadingId === c.id" class="cost-hint">读取中</span>
            <n-tag size="tiny" :type="c.status === 'finalized' ? 'success' : 'default'">
              {{ c.status === 'finalized' ? '已定稿' : '草稿' }}
            </n-tag>
          </div>
        </n-list-item>
      </n-list>
    </div>

    <div class="editor">
      <div class="editor-toolbar">
        <n-input v-model:value="title" :disabled="repairApplying || streaming" placeholder="章节标题" class="title-input" />
        <n-input v-model:value="directive" :disabled="repairApplying || streaming" placeholder="章前指令（如：让 A 与 B 决裂）" class="directive-input" />
        <div class="toolbar-groups">
        <div class="btn-row primary-actions">
          <n-button type="primary" :loading="streaming" :disabled="repairApplying || saving" @click="doWrite">AI生成新章</n-button>
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
          <n-button :disabled="!novelId || (!isDirty && !hasUnverifiedCreation) || creationBlocked || repairApplying || streaming" :loading="saving" @click="saveContent">{{ selectedId ? '保存' : hasUnverifiedCreation ? '核对原保存' : '保存草稿' }}</n-button>
          <span v-if="novelId" class="save-state" :class="{ dirty: isDirty }">
            {{ isDirty ? '未保存' : selectedId ? '已保存' : '新章草稿' }}
          </span>
        </div>
        <section v-if="creationRecords.length || activeCreation || creationStorageWarning" class="story-review-hint" aria-label="待核对草稿保存">
          <p v-if="hasUnverifiedCreation">这次草稿可能已经保存，结果尚未核对。核对原保存会沿用首次提交，当前新编辑仍保留；需要另写一章时请明确点击“新章”。</p>
          <p v-if="creationBlocked" role="alert">{{ creationBlockedMessage }}</p>
          <p v-if="creationStorageWarning" class="repair-error" role="alert">{{ creationStorageWarning }}</p>
          <n-button v-if="hasUnverifiedCreation" size="small" @click="copyCreationDraft">复制当前稿件</n-button>
          <div v-for="record in creationRecords" :key="record.requestId" class="btn-row">
            <span>待核对：{{ record.draft.title || '未命名草稿' }} · {{ new Date(record.createdAt).toLocaleString() }}</span>
            <n-button size="small" :disabled="saving || activeCreation?.requestId === record.requestId" @click="restoreCreation(record.requestId)">恢复待核对稿</n-button>
            <n-button size="small" :disabled="saving" @click="discardCreation(record.requestId)">移除本地记录</n-button>
          </div>
        </section>
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
          <n-button type="success" :disabled="!selectedId || streaming || hasIncompleteOutput || repairApplying || settleApplying" :loading="settleLoading" @click="doSettle">定稿沉淀</n-button>
          <n-button v-if="settleRefreshNeeded && !showSettle" :disabled="settleApplying" :loading="settleLoading" @click="refreshSettleChapter">重新读取章节核对</n-button>
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

      <div v-if="!streaming && novelId" class="editor-body">
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

      <div v-if="conflicts.length || deepLoading || deepMeta || outlineCheckResult || storyCheckResult" class="result-cols">
        <div v-if="conflicts.length" class="result-col result-conflicts">
          <div class="result-title">规则层一致性冲突</div>
          <n-list>
            <n-list-item v-for="(c, i) in conflicts" :key="i">
              <n-tag type="error" size="small">{{ c.rule }}</n-tag>
              {{ c.message }}
            </n-list-item>
          </n-list>
        </div>

        <section v-if="deepLoading || deepMeta" class="result-col result-deep" aria-label="语义深度检查结果">
          <div class="result-title">
            语义深度检查
            <n-tag :type="deepLoading ? 'default' : deepClean ? 'success' : 'warning'" size="small">{{ deepStatusLabel }}</n-tag>
          </div>
          <p v-if="deepLoading" role="status">正在检查当前稿件，本次结果尚未返回。</p>
          <template v-else-if="deepMeta">
            <p v-if="deepCoverageConfirmed" class="deep-meta">
              共 {{ deepMeta.chunks_total }} 块；可核对 {{ deepMeta.chunks_eligible }} 块；尝试 {{ deepMeta.chunks_attempted }} 块；已完成 {{ deepMeta.chunks_checked }} 块；失败 {{ deepMeta.chunks_failed }} 块；未查 {{ deepMeta.chunks_unchecked }} 块；跳过 {{ deepMeta.chunks_skipped }} 块（规则命中 {{ deepMeta.chunks_skipped_rule }} 块，无相关设定 {{ deepMeta.chunks_skipped_no_entity }} 块）。
            </p>
            <n-tag v-if="deepMeta.truncated" type="warning" size="tiny">部分正文未检查（上限 8 块）</n-tag>
            <p class="story-context">语义层只核对包含人物、地点或世界观名称且未命中规则的分块；跳过及未查内容仍需人工校对。</p>
            <p v-if="deepIncompleteHint" class="story-review-hint">{{ deepIncompleteHint }}</p>
            <p v-for="(warning, i) in deepMeta.warnings" :key="i" class="story-review-hint" role="alert">{{ warning }}</p>
            <n-empty v-if="!deepConflicts.length" :description="deepCheckEmptyText" />
            <n-list v-else>
              <n-list-item v-for="(d, i) in deepConflicts" :key="i">
                <div class="deep-item" @click="locateExcerpt(d.excerpt)">
                  <div class="deep-head">
                    <n-tag size="small" :type="d.severity === 'high' ? 'error' : 'warning'">
                      {{ d.type }}
                    </n-tag>
                    <span class="deep-chunk">块 {{ d.chunk_index + 1 }}</span>
                  </div>
                  <div class="deep-excerpt">{{ d.excerpt }}</div>
                  <div class="deep-reason">依据：{{ d.reason }}</div>
                </div>
              </n-list-item>
            </n-list>
            <div v-if="deepRuleConflicts.length" class="result-conflicts" aria-label="深度检查发现的规则冲突">
              <div class="result-title">规则层一致性冲突</div>
              <n-list>
                <n-list-item v-for="(c, i) in deepRuleConflicts" :key="i">
                  <n-tag type="error" size="small">{{ c.rule }}</n-tag>
                  {{ c.message }}
                </n-list-item>
              </n-list>
            </div>
          </template>
        </section>

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

    <n-modal :show="showPreview" @update:show="handlePreviewVisibility" :closable="!previewApplying && !previewRefreshing" :mask-closable="!previewApplying && !previewRefreshing" :close-on-esc="!previewApplying && !previewRefreshing" preset="card" title="改写预览（尚未落库）" style="width: 760px">
      <p v-if="previewError" role="alert" aria-label="改写来源核对提示" class="delete-warning">{{ previewError }}</p>
      <div class="preview-cols" aria-label="段落改写候选">
        <div class="preview-col">
          <div class="preview-col-title">原文</div>
          <textarea class="preview-col-text rewrite-copy-text" aria-label="改写选中原文" readonly :value="previewPassage" />
        </div>
        <div class="preview-col">
          <div class="preview-col-title">改写</div>
          <textarea class="preview-col-text rewrite-copy-text" aria-label="改写候选段落" readonly :value="previewNew" />
        </div>
      </div>
      <template v-if="previewRefreshNeeded">
        <p class="summary-hint">当前输入与候选未被保存。复制后可明确重新读取已保存章节，再选择段落生成改写。</p>
        <label>原稿全文<textarea class="preview-col-text rewrite-copy-text" aria-label="改写原稿全文" readonly :value="previewContext?.source.content ?? ''" /></label>
        <label>候选全文<textarea class="preview-col-text rewrite-copy-text" aria-label="改写候选全文" readonly :value="previewFullContent" /></label>
      </template>
      <template #footer>
        <div class="modal-actions repair-actions">
          <n-button :disabled="previewApplying || previewRefreshing" @click="discardPreview">放弃</n-button>
          <n-button :disabled="!previewNew" @click="copyRewritePreview(previewNew)">复制候选段落</n-button>
          <n-button v-if="previewRefreshNeeded" @click="copyRewritePreview(previewContext?.source.content ?? '')">复制原稿</n-button>
          <n-button v-if="previewRefreshNeeded" @click="copyRewritePreview(previewFullContent)">复制候选全文</n-button>
          <n-button v-if="previewRefreshNeeded" :loading="previewRefreshing" :disabled="previewApplying || saving || streaming" @click="refreshPreviewChapter">重新读取章节核对</n-button>
          <n-button :disabled="previewApplying || previewRefreshing || previewRefreshNeeded || saving || streaming" @click="doRewrite">重新生成改写</n-button>
          <n-button type="primary" :loading="previewApplying" :disabled="!previewGuarded || previewRefreshNeeded || previewRefreshing || saving || hasIncompleteOutput || streaming || !previewFullContent.trim()" @click="applyPreview">应用</n-button>
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

    <n-modal :show="showSettle" @update:show="handleSettleVisibility" :mask-closable="!settleApplying" :closable="!settleApplying" :close-on-esc="!settleApplying" preset="card" title="设定变更建议" style="width: 560px">
      <p class="settle-summary">摘要：{{ settleSummary }}</p>
      <ul v-if="settleWarnings.length" class="settle-warning"><li v-for="(warning, i) in settleWarnings" :key="i">{{ warning }}</li></ul>
      <p v-if="settleRefreshNeeded" class="repair-error" role="alert">章节来源已变化。请先复制需要保留的候选，再重新读取章节核对并定稿。</p>
      <p v-if="settleReceiptUnavailable" class="repair-error" role="alert">确认回执不完整，不能继续提交。请核对后端版本与已保存项目。</p>
      <p v-else-if="settlePendingSubmission && !settleApplying" class="settle-warning">上次提交尚未得到确认。重试会核对原提交的建议，等待期间的新编辑不会作为新项目重复创建。</p>
      <div v-if="suggestions.length">
        <div v-for="(s, i) in suggestions" :key="i" class="suggestion-row">
          <n-checkbox v-model:checked="checkedSuggestion[i]" :disabled="settleCompleted[i] || settleApplying || !!settlePendingSubmission || settleConfirmBlocked">{{ suggestionLabel(s) }}</n-checkbox>
          <n-tag v-if="settleCompleted[i]" :type="settleReceipts[i]?.deleted ? 'warning' : 'success'" size="small">{{ settleReceiptLabel(i) }}</n-tag>
          <template v-if="settleErrors[i]">
            <p class="repair-error">{{ settleErrors[i] }}</p>
            <n-input v-model:value="settleDataTexts[i]" type="textarea" :disabled="settleApplying || !!settlePendingSubmission || settleConfirmBlocked" :autosize="{ minRows: 2, maxRows: 8 }" />
          </template>
          <template v-if="settleCompleted[i] && settleDataTexts[i] !== settleConfirmedTexts[i]">
            <p class="settle-warning">此项已按原提交确认，等待期间的新编辑保留供复制，不会再次创建。</p>
            <n-input :value="settleDataTexts[i]" type="textarea" readonly :autosize="{ minRows: 2, maxRows: 8 }" />
          </template>
        </div>
      </div>
      <n-empty v-else description="未识别到新增设定" />
      <template #footer>
        <div class="modal-actions">
          <n-button :disabled="settleApplying" @click="handleSettleVisibility(false)">取消</n-button>
          <n-button v-if="settleRefreshNeeded" :disabled="settleApplying" :loading="settleLoading" @click="refreshSettleChapter">重新读取章节核对</n-button>
          <n-button type="primary" :disabled="settleLoading || settleConfirmBlocked" :loading="settleApplying" @click="confirmSettle">{{ settlePendingSubmission ? '重试核对原提交' : '确认落库' }}</n-button>
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
.rewrite-copy-text {
  box-sizing: border-box;
  display: block;
  width: 100%;
  height: 150px;
  resize: vertical;
  background: #fafafa;
  font: inherit;
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
.settle-warning {
  color: #ad6800;
  line-height: 1.6;
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
