<script lang="ts">
import { ref as sharedRef } from 'vue'
import type { AutopilotInjectionIntent } from '../utils/autopilotInjection'

interface DirectiveInput { text: string; revision: number }
interface DirectiveSubmission {
  token: number; state: 'pending' | 'confirmed' | 'unknown'
  intent: AutopilotInjectionIntent; revision: number; notice?: string; canRetry?: boolean; retainedDraft?: boolean; trackingDraft?: boolean
}
// 离开页签只卸载监控；同一页面内保留作者构思与原任务的在途提交。
const autopilotAuthor = {
  inputs: sharedRef(new Map<string, DirectiveInput>()),
  submissions: sharedRef(new Map<string, DirectiveSubmission>()),
  notices: sharedRef(new Map<string, string>()),
  sequence: 0,
}
</script>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, MarkLineComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { NButton, NInput, NInputNumber, NModal, NProgress, NTag, useMessage } from 'naive-ui'
import { api, streamEvents } from '../api'
import { getAutopilotDraftWriter } from '../autopilotDrafts'
import { wordLimitError, wordLimitLabel, wordLimits } from '../utils/wordLimits'
import { hashAutopilotDirective, isAutopilotInjectionReceipt, listAutopilotInjections, removeAutopilotInjection, storeAutopilotInjection } from '../utils/autopilotInjection'
import type { AutopilotDraftWriter, DraftSaveState } from '../autopilotDrafts'
import type { AutopilotDraftInput, AutopilotEvent, AutopilotTaskSnapshot, AutopilotTaskStatus, WordLimits } from '../types'

echarts.use([LineChart, GridComponent, LegendComponent, MarkLineComponent, TitleComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const running = ref(false)
const serverRunning = ref(false)
const startingTask = ref(false)
const currentTaskId = ref('')
let directiveScopeRevision = 0
watch(currentTaskId, () => { directiveScopeRevision += 1 }, { flush: 'sync' })
const taskStatus = ref<AutopilotTaskStatus | 'idle'>('idle')
const connectionState = ref<'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'>('idle')
const checkingStatus = ref(false)
const statusNotice = ref('')
const paused = ref(false)
const pauseRequested = ref(false)
const controlPending = ref(false)
const connectionUnconfirmed = ref(false)
const taskNotice = ref('')
const progressText = ref('')
const done = ref(0)
const total = ref(0)
const directive = computed({
  get: () => autopilotAuthor.inputs.value.get(props.novelId)?.text ?? '',
  set: (text: string) => {
    if (!props.novelId) return
    const previous = autopilotAuthor.inputs.value.get(props.novelId)
    autopilotAuthor.inputs.value.set(props.novelId, { text, revision: (previous?.revision ?? 0) + 1 })
    for (const submission of autopilotAuthor.submissions.value.values()) {
      if (submission.intent.novelId !== props.novelId || submission.intent.taskId !== currentTaskId.value
        || submission.state === 'confirmed' || !submission.trackingDraft) continue
      submission.intent.currentDraft = text
      submission.intent.currentDraftEdited = true
      if (submission.intent.hash) persistInjection(submission.intent)
    }
  },
})
const submissionKey = (intent: AutopilotInjectionIntent) => JSON.stringify([intent.novelId, intent.taskId, intent.requestId])
const taskSubmissions = computed(() => [...autopilotAuthor.submissions.value.values()].filter(submission =>
  submission.intent.novelId === props.novelId && submission.intent.taskId === currentTaskId.value))
const directiveSubmission = computed(() => taskSubmissions.value.find(row => row.state === 'pending')
  ?? taskSubmissions.value.find(row => row.state === 'unknown') ?? taskSubmissions.value[taskSubmissions.value.length - 1])
const injecting = computed(() => taskSubmissions.value.some(row => row.state === 'pending'))
const injectionUnknown = computed(() => taskSubmissions.value.some(row => row.state === 'unknown'))
const pendingDirective = computed(() => [...autopilotAuthor.submissions.value.values()]
  .some((submission) => submission.intent.novelId === props.novelId && submission.state === 'pending'))
const injectionRecoveries = computed(() => [...autopilotAuthor.submissions.value.values()].filter(submission =>
  submission.intent.novelId === props.novelId && (submission.state !== 'confirmed' || submission.retainedDraft)))
const injectionStorageNotice = computed(() => autopilotAuthor.notices.value.get(props.novelId) ?? '')
const restoreInjectionCandidate = ref<{ requestId: string; novelId: string; revision: number; scope: number; taskScope: number; draft: string } | null>(null)
const showRestoreInjection = computed(() => !!restoreInjectionCandidate.value)
const minWords = ref<number | null>(null)
const maxWords = ref<number | null>(null)
const taskWordLimits = ref<WordLimits | null>(null)
const wordTargetError = computed(() => wordLimitError(minWords.value, maxWords.value))
interface NodeDraft {
  nodeId: string; title: string; text: string
  draftKey: string; revision: number; draftAttempt: number
  serverManaged: boolean; saveState: DraftSaveState
}
interface FailedDraft extends NodeDraft {
  id: number
  attempt: number
  reason: string
  unconfirmed: boolean
  collapsed: boolean
  copyFallback: boolean
  eventId?: number
}
const activeDrafts = ref<Record<string, NodeDraft>>({})
const failedDrafts = ref<FailedDraft[]>([])
const qualityWarnings = ref<string[]>([])
const showDiscard = ref(false)
const discardPending = ref(false)
const discardDraftId = ref<number | null>(null)
const checkingDrafts = ref(false)
const draftsLoaded = ref(false)
const recoveryNotice = ref('')
const persistenceErrors = ref<Record<string, string>>({})
const storageNotice = computed(() => {
  const error = Object.values(persistenceErrors.value)[0]
  return error ? `暂存操作失败：${error}。请复制需要保留的片段或重试；尚未得到暂存确认的内容仍只在本页面。` : ''
})
const draftTextRefs = new Map<number, HTMLTextAreaElement>()
const activeDraftList = computed(() => Object.values(activeDrafts.value))
const hasActiveTask = computed(() => startingTask.value || serverRunning.value)
const hasPendingOutput = computed(() => activeDraftList.value.some((draft) => draft.saveState !== 'saved')
  || failedDrafts.value.some((draft) => draft.saveState !== 'saved'))
const authorDirty = computed(() => hasPendingOutput.value || directive.value.length > 0 || pendingDirective.value || injectionUnknown.value)
let runId = 0
let statusRequestId = 0
let attempt = 0
let draftId = 0
let unmounted = false
let latestTaskTerminal = false
let connectionController: AbortController | null = null
let draftsRequestId = 0
let draftWriter: AutopilotDraftWriter | null = null
let unsubscribeWriter: (() => void) | null = null
let connectionEpoch = 0
let eventCursor = 0
let reconnectAttempt = 0
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
const pendingStartRequestId = ref<string | null>(null)
let pendingStartPreviousTaskId: string | null = null
let pendingStartWordLimits: WordLimits | null = null
const wordTargetLocked = computed(() => hasActiveTask.value || !!pendingStartRequestId.value || checkingStatus.value)
const seenDraftRevisions = new Map<string, number>()
const finishedDraftIds = new Set<string>()
const dismissedDraftIds = new Set<string>()

function bindDraftWriter(novelId: string) {
  unsubscribeWriter?.()
  draftWriter = novelId ? getAutopilotDraftWriter(novelId, {
    save: api.saveAutopilotDraft, remove: api.deleteAutopilotDraft,
  }) : null
  unsubscribeWriter = draftWriter?.subscribe((id, status) => {
    if (unmounted || props.novelId !== novelId) return
    const draft = [...Object.values(activeDrafts.value), ...failedDrafts.value].find((item) => item.draftKey === id)
    if (draft) draft.saveState = status.state
    if (status.state === 'error') persistenceErrors.value[id] = status.error || '写入失败'
    else delete persistenceErrors.value[id]
  }) || null
}

function newNodeDraft(nodeId: string, title: string, event?: AutopilotEvent): NodeDraft {
  return {
    nodeId, title, text: '', draftKey: event?.draft_id || crypto.randomUUID(),
    revision: event?.draft_revision || 0, draftAttempt: event?.draft_attempt || attempt,
    serverManaged: !!event?.draft_id, saveState: event?.draft_persisted ? 'saved' : 'pending',
  }
}

function updateDraftMetadata(draft: NodeDraft, event: AutopilotEvent) {
  if (!event.draft_id) return
  draft.draftKey = event.draft_id
  draft.revision = event.draft_revision || draft.revision
  draft.draftAttempt = event.draft_attempt || draft.draftAttempt
  draft.serverManaged = true
  draft.saveState = event.draft_persisted ? 'saved' : 'error'
  if (event.draft_persisted) delete persistenceErrors.value[draft.draftKey]
  else persistenceErrors.value[draft.draftKey] = '未收到该片段的暂存确认'
}

function persistDraft(draft: NodeDraft, state: AutopilotDraftInput['state'], reason = '', immediate = false) {
  // 带 draft_id 的正文在服务器发出前已暂存，客户端不覆盖可能仍在推进的后台数据。
  if (draft.serverManaged) return
  draftWriter?.save(draft.draftKey, {
    node_id: draft.nodeId, title: draft.title, content: draft.text,
    attempt: draft.draftAttempt, reason, state, revision: draft.revision,
  }, immediate)
}

async function loadStoredDrafts(expectedRunId = runId, novelId = props.novelId) {
  if (!novelId || !isCurrentRun(expectedRunId, novelId)) return
  const requestId = ++draftsRequestId
  const requestedCursor = eventCursor
  const writer = draftWriter
  const isLatest = () => requestId === draftsRequestId && isCurrentRun(expectedRunId, novelId)
  checkingDrafts.value = true
  try {
    await writer?.flush()
    const records = await api.listAutopilotDrafts(novelId)
    if (!isLatest()) return
    const previous = new Map(failedDrafts.value.map((draft) => [draft.draftKey, draft]))
    const activeIds = new Set(Object.values(activeDrafts.value).map((draft) => draft.draftKey))
    const recovered = records.filter((record) => !activeIds.has(record.id) && !dismissedDraftIds.has(record.id)).map((record): FailedDraft => {
      const existing = previous.get(record.id)
      if (existing && existing.text !== record.content && (
        (!existing.serverManaged && existing.saveState !== 'saved') || existing.revision > record.revision
      )) return existing
      return {
        id: existing?.id || ++draftId, draftKey: record.id, nodeId: record.node_id,
        title: record.title, text: record.content, revision: record.revision,
        attempt: record.attempt, draftAttempt: record.attempt,
        reason: record.reason || '恢复的生成片段，章节保存状态尚未确认',
        unconfirmed: record.state !== 'failed', serverManaged: true, saveState: 'saved',
        collapsed: existing?.collapsed || false, copyFallback: existing?.copyFallback || false,
      }
    })
    const recoveredIds = new Set(records.map((record) => record.id))
    // 写库失败的本页片段不能因一次列表刷新被清空。
    for (const draft of failedDrafts.value) {
      if (!recoveredIds.has(draft.draftKey) && !dismissedDraftIds.has(draft.draftKey) && (
        ['pending', 'saving', 'error'].includes(draft.saveState) || (draft.eventId || 0) > requestedCursor
      )) recovered.push(draft)
    }
    failedDrafts.value = recovered
    attempt = Math.max(attempt, ...records.map((record) => record.attempt))
    // 历史半稿的未知状态不能覆盖本页最新任务已经收到的终态确认。
    if (!latestTaskTerminal && !serverRunning.value) connectionUnconfirmed.value = recovered.some((draft) => draft.unconfirmed)
    draftsLoaded.value = true
    recoveryNotice.value = recovered.length ? `已从本地数据库找回 ${records.length} 份暂存片段。暂存片段不会自动成为正式章节。` : ''
  } catch (error) {
    if (isLatest()) recoveryNotice.value = `无法读取暂存片段：${(error as Error).message}。请重试读取，现有页面片段仍可复制。`
  } finally {
    if (isLatest()) checkingDrafts.value = false
  }
}

async function retryDraftStorage() {
  await draftWriter?.retry()
  await loadStoredDrafts()
}

async function refreshTaskStatus() {
  const currentRunId = runId
  const novelId = props.novelId
  await Promise.all([loadTaskStatus(currentRunId, novelId), loadStoredDrafts(currentRunId, novelId)])
}

function draftSaveLabel(draft: NodeDraft) {
  return draft.saveState === 'saved' ? '片段已自动暂存'
    : draft.saveState === 'error' ? '暂存失败，请复制保留' : '正在自动暂存…'
}

function isCurrentRun(expectedRunId: number, novelId: string) {
  return !unmounted && expectedRunId === runId && props.novelId === novelId
}

function invalidateStatusRequests() {
  statusRequestId += 1
  checkingStatus.value = false
}

function disconnect() {
  runId += 1
  draftsRequestId += 1
  checkingDrafts.value = false
  invalidateStatusRequests()
  stopMonitoring()
  running.value = false
  serverRunning.value = false
  paused.value = false
  pauseRequested.value = false
  controlPending.value = false
  checkingStatus.value = false
  startingTask.value = false
  currentTaskId.value = ''
  taskStatus.value = 'idle'
  pendingStartRequestId.value = null
  pendingStartPreviousTaskId = null
  pendingStartWordLimits = null
  taskWordLimits.value = null
  minWords.value = null
  maxWords.value = null
  eventCursor = 0
  seenDraftRevisions.clear()
  finishedDraftIds.clear()
  dismissedDraftIds.clear()
}

function preserveNode(nodeId: string, reason: string, unconfirmed: boolean, title?: string) {
  const draft = activeDrafts.value[nodeId] || newNodeDraft(nodeId, title || '未命名章节')
  const preserved: FailedDraft = {
    ...draft,
    id: ++draftId,
    attempt: draft.draftAttempt,
    nodeId,
    title: title || draft.title,
    text: draft.text,
    reason,
    unconfirmed,
    collapsed: false,
    copyFallback: false,
    eventId: eventCursor + 1,
  }
  const existingIndex = failedDrafts.value.findIndex((item) => item.draftKey === preserved.draftKey)
  if (existingIndex < 0) failedDrafts.value.push(preserved)
  else failedDrafts.value[existingIndex] = { ...preserved, id: failedDrafts.value[existingIndex]!.id }
  finishedDraftIds.add(preserved.draftKey)
  persistDraft(preserved, unconfirmed ? 'unconfirmed' : 'failed', reason, true)
  delete activeDrafts.value[nodeId]
}

function preserveActiveDrafts(reason: string) {
  for (const nodeId of Object.keys(activeDrafts.value)) preserveNode(nodeId, reason, true)
}

function handleConnectionError(error: string, code?: string) {
  connectionState.value = 'disconnected'
  running.value = false
  if (currentTaskId.value && serverRunning.value && !latestTaskTerminal) {
    // 监控断线不改变后台任务和服务器持有的实时半稿。
    statusNotice.value = `监控连接断开：${error}。后台任务仍独立运行，可以重新连接、暂停或注入构思。`
    return
  }
  latestTaskTerminal = code === 'CLIENT_UPDATE_FAILED'
  invalidateStatusRequests()
  statusNotice.value = ''
  preserveActiveDrafts(error || '连接中断，未收到章节保存确认')
  running.value = false
  paused.value = false
  pauseRequested.value = false
  connectionUnconfirmed.value = code !== 'CLIENT_UPDATE_FAILED' && code !== 'STREAM_REQUEST_FAILED'
  taskNotice.value = code === 'CLIENT_UPDATE_FAILED'
    ? `已收到任务结束确认，但页面同步失败：${error}。请重新核对章节。`
    : code === 'STREAM_REQUEST_FAILED' ? `任务未启动：${error}`
      : `连接中断，完成状态未确认：${error}。断开连接不等于停止后台任务。`
  progressText.value = taskNotice.value
  void draftWriter?.flush()
}

function registerDraftTextRef(id: number, element: unknown) {
  if (element instanceof HTMLTextAreaElement) draftTextRefs.set(id, element)
  else draftTextRefs.delete(id)
}

async function copyDraft(draft: FailedDraft) {
  if (!draft.text) return
  try {
    await navigator.clipboard.writeText(draft.text)
    message.success('已复制未完成稿')
  } catch {
    draft.copyFallback = true
    draft.collapsed = false
    await nextTick()
    draftTextRefs.get(draft.id)?.focus()
    draftTextRefs.get(draft.id)?.select()
  }
}

function requestDiscard(draft: FailedDraft) {
  discardDraftId.value = draft.id
  showDiscard.value = true
}

function cancelDiscard() {
  if (discardPending.value) return
  showDiscard.value = false
  discardDraftId.value = null
}

async function confirmDiscard() {
  const id = discardDraftId.value
  const draft = failedDrafts.value.find((item) => item.id === id)
  const writer = draftWriter
  if (!draft || !writer || discardPending.value) return
  const novelId = props.novelId
  discardPending.value = true
  try {
    await writer.flush()
    const removed = await writer.remove(draft.draftKey, draft.revision)
    if (unmounted || props.novelId !== novelId) return
    if (removed) {
      dismissedDraftIds.add(draft.draftKey)
      finishedDraftIds.add(draft.draftKey)
      failedDrafts.value = failedDrafts.value.filter((item) => item.draftKey !== draft.draftKey)
      recoveryNotice.value = ''
      showDiscard.value = false
      discardDraftId.value = null
    } else message.error('未能删除暂存片段，已继续保留。请复制需要保留的内容后再试。')
  } finally {
    if (!unmounted && props.novelId === novelId) discardPending.value = false
  }
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!authorDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

function stopMonitoring() {
  connectionEpoch += 1
  connectionController?.abort()
  connectionController = null
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = null
  running.value = false
  connectionState.value = 'idle'
}

function preserveLocalActiveDrafts(reason: string) {
  for (const draft of Object.values(activeDrafts.value)) {
    if (!draft.serverManaged) preserveNode(draft.nodeId, reason, true)
  }
}

function applySnapshot(snapshot: AutopilotTaskSnapshot) {
  if (snapshot.novel_id !== props.novelId || !snapshot.task_id
    || !Number.isSafeInteger(snapshot.event_id) || snapshot.event_id < 0
    || !Array.isArray(snapshot.active_drafts)) throw new Error('后台任务快照格式无效')
  const changedTask = currentTaskId.value !== snapshot.task_id
  if (!changedTask && snapshot.event_id < eventCursor) return false
  if (changedTask) {
    stopMonitoring()
    preserveLocalActiveDrafts('上一次监控未收到章节保存确认')
    currentTaskId.value = snapshot.task_id
    eventCursor = 0
    reconnectAttempt = 0
    latestTaskTerminal = false
    controlPending.value = false
    invalidateStatusRequests()
  }
  const previous = activeDrafts.value
  const active: Record<string, NodeDraft> = {}
  for (const record of snapshot.active_drafts) {
    if (!record.draft_id || typeof record.content !== 'string') throw new Error('后台实时稿快照格式无效')
    if (dismissedDraftIds.has(record.draft_id)) continue
    const existing = previous[record.node_id]
    const draft = existing?.draftKey === record.draft_id && existing.revision > record.draft_revision
      ? existing : { ...newNodeDraft(record.node_id, record.title, { type: 'text', ...record }), text: record.content }
    active[record.node_id] = draft
    seenDraftRevisions.set(record.draft_id, Math.max(seenDraftRevisions.get(record.draft_id) || 0, draft.revision))
    finishedDraftIds.delete(record.draft_id)
  }
  for (const draft of Object.values(previous)) {
    if (!draft.serverManaged && !active[draft.nodeId]) preserveNode(draft.nodeId, '重新连接时未收到该片段的正式章节确认', true)
  }
  activeDrafts.value = active
  const activeIds = new Set(Object.values(active).map((draft) => draft.draftKey))
  failedDrafts.value = failedDrafts.value.filter((draft) => !activeIds.has(draft.draftKey) && !dismissedDraftIds.has(draft.draftKey))
  eventCursor = snapshot.event_id
  serverRunning.value = snapshot.running
  paused.value = snapshot.paused
  pauseRequested.value = snapshot.pause_requested
  taskStatus.value = snapshot.status
  latestTaskTerminal = !snapshot.running
  connectionUnconfirmed.value = snapshot.status === 'interrupted'
  done.value = snapshot.done
  total.value = snapshot.total
  progressText.value = snapshot.message || (snapshot.running ? '后台运行中' : '任务已结束')
  taskNotice.value = snapshot.task_notice || ''
  taskWordLimits.value = snapshot.word_limits ? { ...snapshot.word_limits } : null
  if (snapshot.running) {
    minWords.value = taskWordLimits.value?.min_words ?? null
    maxWords.value = taskWordLimits.value?.max_words ?? null
  }
  qualityWarnings.value = [...new Set(snapshot.quality_warnings)]
  driftWarnings.value = snapshot.drift_warnings.map((warning) => ({ ...warning }))
  statusNotice.value = ''
  attempt = Math.max(attempt, ...snapshot.active_drafts.map((draft) => draft.draft_attempt))
  return true
}

function handleObservedEvent(event: AutopilotEvent, taskId: string) {
  if (currentTaskId.value !== taskId) return
  if (event.task_id && event.task_id !== taskId) return
  if (event.novel_id && event.novel_id !== props.novelId) return
  if (event.type === 'snapshot') {
    if (event.task_id !== taskId) throw new Error('订阅收到其它后台任务的快照')
    if (!applySnapshot(event as AutopilotTaskSnapshot)) return
    connectionState.value = event.running ? 'connected' : 'idle'
    running.value = !!event.running
    return
  }
  if (event.event_id != null) {
    if (!Number.isSafeInteger(event.event_id) || event.event_id < 0) throw new Error('后台事件游标格式无效')
    if (event.event_id <= eventCursor) return
  }
  handleEvent(event)
  if (event.event_id != null) eventCursor = event.event_id
}

function scheduleReconnect(expectedRunId: number, novelId: string, taskId: string, expectedEpoch: number) {
  if (!isCurrentRun(expectedRunId, novelId) || currentTaskId.value !== taskId
    || connectionEpoch !== expectedEpoch || !serverRunning.value || latestTaskTerminal) return
  if (reconnectAttempt >= 4) {
    connectionState.value = 'disconnected'
    statusNotice.value = '监控连接断开，自动重连未成功。后台任务仍独立运行，可手动重新连接或核对后台状态。'
    return
  }
  const delay = 500 * (2 ** reconnectAttempt++)
  connectionState.value = 'reconnecting'
  statusNotice.value = '监控连接断开，正在重新连接；后台任务继续运行。'
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (isCurrentRun(expectedRunId, novelId) && currentTaskId.value === taskId && connectionEpoch === expectedEpoch) {
      void observeTask(taskId, expectedRunId, novelId)
    }
  }, delay)
}

async function observeTask(taskId: string, expectedRunId = runId, novelId = props.novelId) {
  if (!taskId || !isCurrentRun(expectedRunId, novelId) || currentTaskId.value !== taskId || latestTaskTerminal) return
  stopMonitoring()
  const epoch = connectionEpoch
  const controller = new AbortController()
  connectionController = controller
  running.value = true
  connectionState.value = reconnectAttempt ? 'reconnecting' : 'connecting'
  const isCurrentConnection = () => isCurrentRun(expectedRunId, novelId)
    && currentTaskId.value === taskId && connectionEpoch === epoch && !controller.signal.aborted
  let errorReceived = false
  try {
    await streamEvents(`/api/v1/novels/${novelId}/autopilot/tasks/${taskId}/events`, (event: AutopilotEvent) => {
      if (isCurrentConnection()) handleObservedEvent(event, taskId)
    }, {
      signal: controller.signal,
      onError: (error, code) => {
        if (!isCurrentConnection()) return
        errorReceived = true
        handleConnectionError(error, code)
      },
    })
    if (isCurrentConnection() && !latestTaskTerminal && !errorReceived) handleConnectionError('监控连接未收到任务终态')
  } catch (error) {
    if (isCurrentConnection()) handleConnectionError((error as Error).message, 'STREAM_INCOMPLETE')
  } finally {
    if (isCurrentConnection()) {
      running.value = false
      connectionController = null
      if (latestTaskTerminal) {
        connectionState.value = 'idle'
        reconnectAttempt = 0
        await draftWriter?.flush()
        await Promise.all([loadCurve(expectedRunId, novelId), loadStoredDrafts(expectedRunId, novelId)])
      } else scheduleReconnect(expectedRunId, novelId, taskId, epoch)
    }
  }
}

async function reconnect() {
  if (startingTask.value || !props.novelId) return
  reconnectAttempt = 0
  stopMonitoring()
  await loadTaskStatus()
  await loadStoredDrafts()
}

async function loadTaskStatus(expectedRunId = runId, novelId = props.novelId) {
  if (!novelId || !isCurrentRun(expectedRunId, novelId)) return
  const requestId = ++statusRequestId
  const isLatestStatusRequest = () => requestId === statusRequestId && isCurrentRun(expectedRunId, novelId)
  checkingStatus.value = true
  try {
    const status = await api.autopilotStatus(novelId)
    if (!isLatestStatusRequest()) return
    if (status.task_id) {
      if (pendingStartRequestId.value && (status.running || status.task_id !== pendingStartPreviousTaskId)) {
        pendingStartRequestId.value = null
        pendingStartPreviousTaskId = null
        pendingStartWordLimits = null
      }
      if (!applySnapshot(status)) return
      if (status.running && !connectionController && !reconnectTimer) void observeTask(status.task_id, expectedRunId, novelId)
      else if (!status.running) stopMonitoring()
    } else {
      serverRunning.value = false
      if (currentTaskId.value && !latestTaskTerminal) {
        stopMonitoring()
        preserveActiveDrafts('后台任务已不可用，正式章节保存状态需要核对')
        taskStatus.value = 'interrupted'
        latestTaskTerminal = true
        connectionUnconfirmed.value = true
        progressText.value = '后台任务已中断；暂存片段仍可找回，继续生成需要显式重试。'
      }
    }
  } catch (e) {
    if (isLatestStatusRequest()) statusNotice.value = `无法核对后台状态：${(e as Error).message}`
  } finally {
    if (isLatestStatusRequest()) checkingStatus.value = false
  }
}
const driftWarnings = ref<{ seq: number; score: number; message: string }[]>([])
// 质量曲线：每章张力与漂移（初始来自 GET /quality/curve，运行时由 SSE 追加）
const curvePoints = ref<{ seq: number; tension: number | null; drift: number | null }[]>([])
const driftThreshold = ref(0.6)
const anchorSet = ref(false)

const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

function renderChart() {
  if (!chart) return
  chart.setOption({
    title: { text: '张力 / 漂移曲线', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    legend: { data: ['张力', '漂移'], top: 22 },
    xAxis: { type: 'category', data: curvePoints.value.map((p) => `第${p.seq}章`) },
    yAxis: [
      { type: 'value', name: '张力', min: 0, max: 10 },
      { type: 'value', name: '漂移相似度', min: 0, max: 1 },
    ],
    series: [
      {
        name: '张力',
        type: 'line',
        yAxisIndex: 0,
        data: curvePoints.value.map((p) => p.tension),
        smooth: true,
        areaStyle: {},
        connectNulls: true,
      },
      {
        name: '漂移',
        type: 'line',
        yAxisIndex: 1,
        data: curvePoints.value.map((p) => p.drift),
        smooth: true,
        connectNulls: true,
        // 阈值参考线，低于此值视为文风漂移
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: '#d03050', type: 'dashed' },
          data: [{ yAxis: driftThreshold.value, label: { formatter: '漂移阈值' } }],
        },
      },
    ],
  })
}

// 按 seq upsert 曲线点，保证初始数据与 SSE 追加数据合并
function upsertPoint(seq: number, patch: { tension?: number | null; drift?: number | null }) {
  const found = curvePoints.value.find((p) => p.seq === seq)
  if (found) {
    if (patch.tension !== undefined) found.tension = patch.tension
    if (patch.drift !== undefined) found.drift = patch.drift
  } else {
    curvePoints.value.push({
      seq,
      tension: patch.tension ?? null,
      drift: patch.drift ?? null,
    })
    curvePoints.value.sort((a, b) => a.seq - b.seq)
  }
}

async function loadCurve(expectedRunId = runId, novelId = props.novelId) {
  if (!novelId) return
  const taskId = currentTaskId.value
  try {
    const res = await api.getQualityCurve(novelId)
    if (!isCurrentRun(expectedRunId, novelId) || currentTaskId.value !== taskId) return
    driftThreshold.value = res.drift_threshold
    anchorSet.value = res.anchor_set
    curvePoints.value = res.chapters.map((c) => ({
      seq: c.seq,
      tension: c.tension_score,
      drift: c.drift_score,
    }))
    renderChart()
  } catch (e) {
    if (isCurrentRun(expectedRunId, novelId) && currentTaskId.value === taskId) message.error((e as Error).message)
  }
}

function applyControlState(ev: AutopilotEvent) {
  if (typeof ev.paused === 'boolean') paused.value = ev.paused
  if (typeof ev.pause_requested === 'boolean') pauseRequested.value = ev.pause_requested
  taskStatus.value = paused.value ? 'paused' : 'running'
  if (ev.message && ev.action !== 'inject') progressText.value = ev.message
}

function handleEvent(ev: AutopilotEvent) {
  switch (ev.type) {
    case 'control':
      applyControlState(ev)
      break
    case 'progress':
      serverRunning.value = true
      progressText.value = ev.message || ''
      if (ev.done != null) done.value = ev.done
      if (ev.total != null) total.value = ev.total
      if (ev.phase === 'generating' && ev.node_id) {
        paused.value = false
        const existing = activeDrafts.value[ev.node_id]
        if (ev.draft_id && (dismissedDraftIds.has(ev.draft_id) || finishedDraftIds.has(ev.draft_id))) break
        if (existing && ev.draft_id && existing.draftKey !== ev.draft_id) preserveNode(ev.node_id, '前次生成未收到章节保存确认', true)
        const draft = existing && (!ev.draft_id || existing.draftKey === ev.draft_id)
          ? existing : newNodeDraft(ev.node_id, ev.title || '未命名章节', ev)
        if (ev.title) draft.title = ev.title
        updateDraftMetadata(draft, ev)
        activeDrafts.value[ev.node_id] = draft
        if (ev.draft_id) failedDrafts.value = failedDrafts.value.filter((item) => item.draftKey !== ev.draft_id)
        persistDraft(draft, 'active')
      }
      break
    case 'text':
      if (!ev.node_id || typeof ev.content !== 'string') break
      if (ev.draft_id && (dismissedDraftIds.has(ev.draft_id) || finishedDraftIds.has(ev.draft_id))) break
      if (ev.draft_id && ev.draft_revision != null && ev.draft_revision <= (seenDraftRevisions.get(ev.draft_id) || 0)) break
      if (activeDrafts.value[ev.node_id] && ev.draft_id && activeDrafts.value[ev.node_id]!.draftKey !== ev.draft_id) {
        preserveNode(ev.node_id, '前次生成未收到章节保存确认', true)
      }
      if (!activeDrafts.value[ev.node_id]) {
        activeDrafts.value[ev.node_id] = newNodeDraft(ev.node_id, ev.title || '未命名章节', ev)
      }
      updateDraftMetadata(activeDrafts.value[ev.node_id], ev)
      if (ev.draft_id) failedDrafts.value = failedDrafts.value.filter((item) => item.draftKey !== ev.draft_id)
      activeDrafts.value[ev.node_id].text += ev.content
      if (ev.draft_id && ev.draft_revision != null) seenDraftRevisions.set(ev.draft_id, ev.draft_revision)
      persistDraft(activeDrafts.value[ev.node_id], 'active')
      break
    case 'chapter_done':
      if (ev.node_id) {
        const draft = activeDrafts.value[ev.node_id]
        if (ev.draft_id) finishedDraftIds.add(ev.draft_id)
        if (draft && (!ev.draft_id || draft.draftKey === ev.draft_id)) {
          if (!draft.serverManaged) void draftWriter?.remove(draft.draftKey, draft.revision)
          delete activeDrafts.value[ev.node_id]
        }
      }
      progressText.value = `第${ev.seq}章生成完成`
      break
    case 'check':
    case 'story_check':
    case 'word_check':
      addQualityWarnings(ev.warnings || [])
      if ((ev.type === 'story_check' || ev.type === 'word_check') && ev.message) progressText.value = ev.message
      break
    case 'tension':
      upsertPoint(ev.seq, { tension: ev.score })
      renderChart()
      break
    case 'drift':
      upsertPoint(ev.seq, { drift: ev.score })
      renderChart()
      if (ev.warning) {
        driftWarnings.value.push({
          seq: ev.seq,
          score: ev.score,
          message: `第${ev.seq}章文风漂移（相似度 ${ev.score}）`,
        })
      }
      break
    case 'paused':
      paused.value = true
      taskStatus.value = 'paused'
      pauseRequested.value = false
      progressText.value = ev.message || '已暂停'
      break
    case 'error':
      if (ev.scope === 'quality' || (ev.saved === true && !ev.fatal && ev.scope !== 'task')) {
        addQualityWarnings([ev.message || '章节已保存，但质量检查失败'])
        message.warning(ev.message || '章节已保存，质量检查待核对')
        break
      } else if (ev.scope === 'chapter' && ev.node_id) {
        if (ev.draft_id && (dismissedDraftIds.has(ev.draft_id) || finishedDraftIds.has(ev.draft_id))) break
        if (activeDrafts.value[ev.node_id] && ev.draft_id && activeDrafts.value[ev.node_id]!.draftKey !== ev.draft_id) break
        if (!activeDrafts.value[ev.node_id]) activeDrafts.value[ev.node_id] = newNodeDraft(ev.node_id, ev.title || '未命名章节', ev)
        updateDraftMetadata(activeDrafts.value[ev.node_id], ev)
        preserveNode(ev.node_id, ev.message || '章节生成失败', ev.saved !== false, ev.title)
      } else if (ev.fatal || ev.scope === 'task') {
        latestTaskTerminal = true
        invalidateStatusRequests()
        statusNotice.value = ''
        preserveActiveDrafts(ev.message || '任务异常，完成状态未确认')
        running.value = false
        serverRunning.value = false
        taskStatus.value = ev.status || 'incomplete'
        paused.value = false
        pauseRequested.value = false
        connectionUnconfirmed.value = ev.saved !== true && ev.code !== 'autopilot_busy' && ev.code !== 'task_start_failed'
        taskNotice.value = ev.message || '任务未完成'
        progressText.value = taskNotice.value
      }
      message.error(ev.message || '发生错误')
      break
    case 'done':
      latestTaskTerminal = true
      invalidateStatusRequests()
      statusNotice.value = ''
      preserveActiveDrafts('任务已结束，但未收到该章节的保存确认')
      running.value = false
      serverRunning.value = false
      taskStatus.value = ev.status || 'completed'
      paused.value = false
      pauseRequested.value = false
      connectionUnconfirmed.value = false
      if (ev.done != null) done.value = ev.done
      if (ev.total != null) total.value = ev.total
      progressText.value = ev.message || '完成'
      void draftWriter?.flush()
      break
  }
}

function addQualityWarnings(warnings: string[]) {
  qualityWarnings.value = [...new Set([
    ...qualityWarnings.value,
    ...warnings.filter((warning) => typeof warning === 'string' && !!warning.trim()),
  ])]
}

async function start() {
  if (hasActiveTask.value || checkingStatus.value || checkingDrafts.value || discardPending.value || !draftsLoaded.value || !props.novelId) return
  if (!pendingStartRequestId.value && wordTargetError.value) { message.error(wordTargetError.value); return }
  invalidateStatusRequests()
  preserveActiveDrafts('上次连接未收到章节保存确认')
  const novelId = props.novelId
  const currentRunId = ++runId
  const previousTaskId = currentTaskId.value
  const requestId = pendingStartRequestId.value || crypto.randomUUID()
  if (!pendingStartRequestId.value) {
    pendingStartPreviousTaskId = previousTaskId
    pendingStartWordLimits = wordLimits(minWords.value, maxWords.value)
  }
  pendingStartRequestId.value = requestId
  latestTaskTerminal = false
  draftsRequestId += 1
  attempt += 1
  stopMonitoring()
  startingTask.value = true
  paused.value = false
  pauseRequested.value = false
  connectionUnconfirmed.value = false
  taskNotice.value = ''
  statusNotice.value = ''
  done.value = 0
  total.value = 0
  driftWarnings.value = []
  curvePoints.value = []
  progressText.value = '启动中…'
  try {
    const snapshot = pendingStartWordLimits
      ? await api.startAutopilot(novelId, requestId, { ...pendingStartWordLimits })
      : await api.startAutopilot(novelId, requestId)
    if (!isCurrentRun(currentRunId, novelId)) return
    applySnapshot(snapshot)
    pendingStartRequestId.value = null
    pendingStartPreviousTaskId = null
    pendingStartWordLimits = null
    if (snapshot.running) void observeTask(snapshot.task_id, currentRunId, novelId)
    else await loadStoredDrafts(currentRunId, novelId)
    void loadCurve(currentRunId, novelId)
  } catch (e) {
    if (!isCurrentRun(currentRunId, novelId)) return
    const error = e as Error & { task_id?: string }
    try {
      const status = await api.autopilotStatus(novelId)
      if (!isCurrentRun(currentRunId, novelId)) return
      if (status.task_id && (status.running || status.task_id !== previousTaskId || status.task_id === error.task_id)) {
        applySnapshot(status)
        pendingStartRequestId.value = null
        pendingStartPreviousTaskId = null
        pendingStartWordLimits = null
        if (status.running) void observeTask(status.task_id, currentRunId, novelId)
        else await loadStoredDrafts(currentRunId, novelId)
        return
      }
    } catch { /* 创建请求的结果仍未确认，保留原 request_id 供作者显式重试。 */ }
    if (isCurrentRun(currentRunId, novelId)) {
      taskNotice.value = `启动结果未确认：${error.message}。可核对后台状态；再次点击启动会使用同一次请求，避免重复创建。`
      progressText.value = taskNotice.value
      connectionUnconfirmed.value = true
    }
  } finally {
    if (isCurrentRun(currentRunId, novelId)) startingTask.value = false
  }
}

async function pause() {
  if (!serverRunning.value || !currentTaskId.value || paused.value || pauseRequested.value || controlPending.value) return
  const currentRunId = runId
  const novelId = props.novelId
  const taskId = currentTaskId.value
  controlPending.value = true
  try {
    const control = await api.pauseAutopilot(novelId, taskId)
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId && serverRunning.value) {
      invalidateStatusRequests()
      if (control.type === 'control') {
        if ((control.event_id || 0) >= eventCursor) applyControlState(control)
      } else pauseRequested.value = !paused.value
    }
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId) message.error((e as Error).message)
  } finally {
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId) controlPending.value = false
  }
}

async function resume() {
  if (!serverRunning.value || !currentTaskId.value || (!paused.value && !pauseRequested.value) || controlPending.value) return
  const currentRunId = runId
  const novelId = props.novelId
  const taskId = currentTaskId.value
  controlPending.value = true
  try {
    const control = await api.resumeAutopilot(novelId, taskId)
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId && serverRunning.value) {
      invalidateStatusRequests()
      if (control.type === 'control') {
        if ((control.event_id || 0) >= eventCursor) applyControlState(control)
      } else {
        paused.value = false
        pauseRequested.value = false
      }
    }
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId) message.error((e as Error).message)
  } finally {
    if (isCurrentRun(currentRunId, novelId) && currentTaskId.value === taskId) controlPending.value = false
  }
}

function persistInjection(intent: AutopilotInjectionIntent) {
  if (!storeAutopilotInjection(intent)) autopilotAuthor.notices.value.set(intent.novelId,
    '浏览器未能保存最新待核对构思；刷新后可能只能找回早前稿或无法找回。请复制当前构思和本次提交原文。')
}

function loadInjectionRecoveries() {
  if (!props.novelId) return
  const result = listAutopilotInjections(props.novelId)
  for (const intent of result.records) {
    const key = submissionKey(intent)
    if (!autopilotAuthor.submissions.value.has(key)) autopilotAuthor.submissions.value.set(key, {
      intent, token: ++autopilotAuthor.sequence, state: 'unknown', revision: -1,
      notice: '找到原任务的待核对记录；恢复和核对都需要明确点击。',
    })
  }
  if (result.unavailable || result.invalid) autopilotAuthor.notices.value.set(props.novelId,
    result.unavailable ? '浏览器待核对缓存不可读取，请复制构思保留；本页面不会自动重发。'
      : `发现 ${result.invalid} 条损坏的待核对缓存，未恢复或删除。请保留可用原文并核对实际章节。`)
}

function restoreInjection(submission: DirectiveSubmission) {
  if (submission.intent.novelId !== props.novelId || (submission.state === 'confirmed' && !submission.retainedDraft)) return
  if (directive.value.length && directive.value !== submission.intent.currentDraft) {
    restoreInjectionCandidate.value = { requestId: submission.intent.requestId, novelId: props.novelId,
      revision: autopilotAuthor.inputs.value.get(props.novelId)?.revision ?? 0, scope: runId, taskScope: directiveScopeRevision,
      draft: submission.intent.currentDraft }
    return
  }
  directive.value = submission.intent.currentDraft
  submission.trackingDraft = true
  finishInjectionRestore(submission)
}

function finishInjectionRestore(submission: DirectiveSubmission) {
  if (submission.state !== 'confirmed') return
  submission.retainedDraft = false
  if (!removeAutopilotInjection(submission.intent.novelId, submission.intent.requestId)) autopilotAuthor.notices.value.set(submission.intent.novelId,
    '稿件已恢复，但浏览器未能清理旧缓存；请复制当前构思，后续核对不会重新注入。')
}

function confirmRestoreInjection() {
  const candidate = restoreInjectionCandidate.value
  if (!candidate) return
  if (!isCurrentRun(candidate.scope, candidate.novelId)
    || candidate.taskScope !== directiveScopeRevision
    || (autopilotAuthor.inputs.value.get(candidate.novelId)?.revision ?? 0) !== candidate.revision) {
    restoreInjectionCandidate.value = null
    message.warning('当前构思已变化，请保留新输入后重新选择恢复。')
    return
  }
  const submission = injectionRecoveries.value.find(row => row.intent.requestId === candidate.requestId)
  restoreInjectionCandidate.value = null
  if (submission) { directive.value = candidate.draft; submission.trackingDraft = true; finishInjectionRestore(submission) }
}

async function performInjection(submission: DirectiveSubmission, mode: 'inject' | 'check' | 'retry') {
  const { intent } = submission
  const key = submissionKey(intent), token = ++autopilotAuthor.sequence
  const currentRunId = runId, taskAtClick = currentTaskId.value, scopeRevision = directiveScopeRevision
  const active = { ...submission, token, state: 'pending' as const, canRetry: false }
  autopilotAuthor.submissions.value.set(key, active)
  const own = () => autopilotAuthor.submissions.value.get(key)?.token === token
  const current = () => own() && isCurrentRun(currentRunId, intent.novelId)
    && currentTaskId.value === taskAtClick && directiveScopeRevision === scopeRevision
  let sent = false
  try {
    const hash = await hashAutopilotDirective(intent.text)
    if (!own()) return
    if (intent.hash && intent.hash !== hash) throw new Error('缓存原文与校验信息不一致，请复制保留并人工核对；未发送请求')
    intent.hash = hash
    persistInjection(intent)
    sent = true
    const result = mode === 'check'
      ? await api.getAutopilotInjection(intent.novelId, intent.taskId, intent.requestId)
      : await api.injectAutopilot(intent.novelId, intent.taskId, intent.text, intent.requestId)
    if (!own()) return
    if (!isAutopilotInjectionReceipt(result, intent) || (mode === 'check' && !result.receipt_replayed)) throw new Error('未收到与原请求原文一致的完整确认')
    const retainedDraft = intent.currentDraft.length > 0 && (intent.currentDraftEdited || !current() || mode !== 'inject')
    autopilotAuthor.submissions.value.set(key, { ...active, state: 'confirmed', retainedDraft,
      notice: retainedDraft ? '原构思已记录；待恢复稿仍保留，请明确恢复或复制。' : undefined })
    if (!retainedDraft && !removeAutopilotInjection(intent.novelId, intent.requestId)) autopilotAuthor.notices.value.set(intent.novelId,
      '构思已记录，但浏览器未能清理待核对缓存；再次核对只读取原记录，请勿当作尚未提交。')
    if (!current() || (mode === 'inject' && !serverRunning.value)) return
    message.success('构思已记录，请核对实际章节执行结果')
    if (mode === 'inject' && serverRunning.value && currentTaskId.value === intent.taskId && !intent.currentDraftEdited
      && autopilotAuthor.inputs.value.get(intent.novelId)?.revision === submission.revision && directive.value === intent.text) directive.value = ''
  } catch (e) {
    if (!own()) return
    const error = e as Error & { status?: number; code?: string }
    const rejected = mode === 'inject' && sent && typeof error.status === 'number'
      && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429
      && error.code !== 'autopilot_injection_request_conflict'
    if (rejected) {
      autopilotAuthor.submissions.value.delete(key)
      if (!removeAutopilotInjection(intent.novelId, intent.requestId)) autopilotAuthor.notices.value.set(intent.novelId,
        '本次注入已被拒绝，但浏览器缓存未清理；请复制保留并核对原记录。')
      if (isCurrentRun(currentRunId, intent.novelId) && currentTaskId.value === taskAtClick && directiveScopeRevision === scopeRevision) message.error(error.message)
    } else {
      autopilotAuthor.submissions.value.set(key, { ...active, state: 'unknown', notice: error.message,
        canRetry: mode === 'check' && error.status === 404 && error.code === 'autopilot_injection_not_found' })
      if (current()) message.warning(`构思记录尚未确认：${error.message}。请核对原注入，不会自动重发。`)
    }
  }
}

async function inject() {
  if (!props.novelId || !serverRunning.value || !currentTaskId.value || injecting.value || injectionUnknown.value) return
  if (!directive.value.trim()) { message.error('请输入构思'); return }
  const text = directive.value
  let requestId: string
  try { requestId = crypto.randomUUID() } catch { message.error('无法生成本次请求身份，请复制构思保留'); return }
  await performInjection({ token: 0, state: 'pending', trackingDraft: true, revision: autopilotAuthor.inputs.value.get(props.novelId)?.revision ?? 0,
    intent: { version: 1, novelId: props.novelId, taskId: currentTaskId.value, requestId, text, hash: '',
      currentDraft: text, currentDraftEdited: false, createdAt: Date.now() } }, 'inject')
}

async function checkInjection(submission: DirectiveSubmission) {
  if (submission.intent.novelId !== props.novelId || submission.state !== 'unknown'
    || autopilotAuthor.submissions.value.get(submissionKey(submission.intent))?.token !== submission.token) return
  await performInjection(submission, 'check')
}

async function retryInjection(submission: DirectiveSubmission) {
  if (!submission.canRetry || submission.state !== 'unknown' || submission.intent.novelId !== props.novelId
    || autopilotAuthor.submissions.value.get(submissionKey(submission.intent))?.token !== submission.token) return
  await performInjection(submission, 'retry')
}

async function copyDirectiveText(text: string) {
  const expectedRunId = runId, novelId = props.novelId
  try {
    await navigator.clipboard.writeText(text)
    if (isCurrentRun(expectedRunId, novelId)) message.success('已复制构思')
  } catch {
    if (isCurrentRun(expectedRunId, novelId)) message.warning('自动复制失败，请在构思输入框或下方原文中手动选中复制。')
  }
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
  loadInjectionRecoveries()
  if (chartRef.value) chart = echarts.init(chartRef.value)
  bindDraftWriter(props.novelId)
  loadCurve()
  loadTaskStatus()
  loadStoredDrafts()
})

onBeforeUnmount(() => {
  preserveLocalActiveDrafts('离开页面时未收到章节保存确认')
  void draftWriter?.flush()
  unmounted = true
  unsubscribeWriter?.()
  disconnect()
  window.removeEventListener('beforeunload', handleBeforeUnload)
  chart?.dispose()
  chart = null
})

watch(
  () => props.novelId,
  () => {
    restoreInjectionCandidate.value = null
    loadInjectionRecoveries()
    preserveLocalActiveDrafts('切换作品时未收到章节保存确认')
    void draftWriter?.flush()
    disconnect()
    activeDrafts.value = {}
    failedDrafts.value = []
    qualityWarnings.value = []
    driftWarnings.value = []
    curvePoints.value = []
    done.value = 0
    total.value = 0
    progressText.value = ''
    taskNotice.value = ''
    connectionUnconfirmed.value = false
    latestTaskTerminal = false
    statusNotice.value = ''
    draftsLoaded.value = false
    recoveryNotice.value = ''
    persistenceErrors.value = {}
    discardPending.value = false
    cancelDiscard()
    bindDraftWriter(props.novelId)
    if (props.novelId) {
      loadCurve()
      loadTaskStatus()
      loadStoredDrafts()
    }
  },
  { flush: 'sync' },
)
watch(authorDirty, (dirty) => emit('dirty-change', dirty), { immediate: true, flush: 'sync' })
</script>

<template>
  <div class="autopilot-panel">
    <div class="controls">
      <n-button type="primary" :disabled="hasActiveTask || checkingStatus || checkingDrafts || discardPending || !draftsLoaded || !novelId || (!pendingStartRequestId && !!wordTargetError)" @click="start">{{ failedDrafts.length ? '重试未完成章节' : '启动自动驾驶' }}</n-button>
      <n-button :disabled="!hasActiveTask || paused || pauseRequested || controlPending" @click="pause">暂停</n-button>
      <n-button :disabled="!hasActiveTask || (!paused && !pauseRequested) || controlPending" @click="resume">{{ pauseRequested && !paused ? '取消暂停' : '恢复' }}</n-button>
      <n-button :disabled="checkingStatus || checkingDrafts || discardPending || !novelId" size="small" @click="refreshTaskStatus">核对后台状态</n-button>
      <n-button v-if="serverRunning && connectionState !== 'connected'" :disabled="checkingStatus || startingTask || !novelId" size="small" @click="reconnect">重新连接监控</n-button>
      <n-button v-if="storageNotice || !draftsLoaded" :disabled="checkingDrafts || discardPending || !novelId" size="small" @click="retryDraftStorage">{{ draftsLoaded ? '重试暂存操作' : '重试读取暂存稿' }}</n-button>
      <n-tag v-if="startingTask" type="warning" size="small">启动中</n-tag>
      <n-tag v-else-if="serverRunning" :type="paused || pauseRequested ? 'warning' : 'success'" size="small">
        {{ paused ? '后台已暂停' : pauseRequested ? '等待章节结束后暂停' : '后台运行中' }}
      </n-tag>
      <n-tag v-else-if="taskStatus === 'interrupted'" size="small" type="warning">任务已中断</n-tag>
      <n-tag v-else-if="connectionUnconfirmed" size="small" type="warning">完成状态未确认</n-tag>
      <n-tag v-else size="small" type="default">空闲</n-tag>
      <n-tag v-if="serverRunning" size="small" :type="connectionState === 'connected' ? 'success' : 'warning'">{{ connectionState === 'connected' ? '监控已连接' : connectionState === 'reconnecting' || connectionState === 'connecting' ? '正在重连' : '监控连接断开' }}</n-tag>
    </div>

    <div class="word-limit-row" aria-label="自动驾驶每章字数目标">
      <span>每章字数目标（可选）</span>
      <n-input-number v-model:value="minWords" :min="1" :max="1000000" :precision="0" :disabled="wordTargetLocked" :input-props="{ 'aria-label': '每章最低字数' }" placeholder="最低字数" />
      <span>至</span>
      <n-input-number v-model:value="maxWords" :min="1" :max="1000000" :precision="0" :disabled="wordTargetLocked" :input-props="{ 'aria-label': '每章最高字数' }" placeholder="最高字数" />
      <span class="output-hint">按非空白字符计数，含标点</span>
      <p v-if="wordTargetError && !pendingStartRequestId" class="task-notice" role="alert">{{ wordTargetError }}</p>
    </div>
    <p v-if="currentTaskId" class="output-hint">{{ serverRunning ? '当前任务' : '上次任务' }}每章目标：{{ wordLimitLabel(taskWordLimits) }}。<template v-if="serverRunning">目标在本次任务期间保持不变。</template></p>

    <div class="inject-row">
      <n-input
        v-model:value="directive"
        :disabled="!novelId"
        placeholder="注入构思（应用到下一章，如：让主角与 B 决裂）"
        style="flex: 1"
      />
      <n-button type="info" :loading="injecting" :disabled="!serverRunning || !currentTaskId || injecting || injectionUnknown" @click="inject">注入</n-button>
      <n-button v-if="directive.length" size="small" @click="copyDirectiveText(directive)">复制当前构思</n-button>
    </div>
    <p v-if="directive.length || pendingDirective" class="output-hint">未提交构思在当前页面内按作品保留，切换页签后可以找回；完整刷新或关闭页面前请复制保留，不会自动注入后台。</p>
    <p v-if="directiveSubmission?.state === 'confirmed' && directive.length" class="output-hint">上一次构思已记录，当前输入已保留；只有再次点击“注入”才会提交。记录不代表已经应用到章节，请核对实际执行结果。</p>
    <p v-if="injectionStorageNotice" class="output-hint" role="alert">{{ injectionStorageNotice }}</p>
    <section v-if="injectionUnknown && directiveSubmission" class="quality-warnings" aria-label="构思注入结果待核对">
      <div class="draft-heading">本次构思注入结果尚未确认</div>
      <p class="output-hint">{{ directiveSubmission.notice }} 请在下方核对原注入。公开任务状态不能替代原请求确认；暂停、恢复和后台运行仍可继续。</p>
      <textarea :value="directiveSubmission.intent.text" readonly class="draft-text" aria-label="本次提交的构思原文"></textarea>
      <n-button size="small" @click="copyDirectiveText(directiveSubmission.intent.text)">复制本次构思</n-button>
    </section>
    <section v-if="injectionRecoveries.length" class="quality-warnings" aria-label="待核对构思记录">
      <div class="draft-heading">待核对构思</div>
      <p class="output-hint">当前浏览器、同一访问地址保存原提交和等待期间稿件。不会自动恢复输入或发送；清理浏览器数据或缓存失败时，请复制保留。核对只确认已记录，不代表已应用或已写入章节。</p>
      <article v-for="submission in injectionRecoveries" :key="submission.intent.requestId">
        <p class="output-hint">{{ submission.intent.taskId === currentTaskId ? '当前任务的原提交' : '此前任务的原提交（不会改绑当前任务）' }}：{{ submission.notice }}</p>
        <textarea :value="submission.intent.currentDraft" readonly class="draft-text" aria-label="待恢复构思稿"></textarea>
        <textarea :value="submission.intent.text" readonly class="draft-text" aria-label="待核对原构思"></textarea>
        <n-button size="small" @click="restoreInjection(submission)">恢复待核对构思</n-button>
        <n-button v-if="submission.state !== 'confirmed'" size="small" :loading="submission.state === 'pending'" :disabled="submission.state === 'pending'" @click="checkInjection(submission)">核对原注入</n-button>
        <n-button v-if="submission.canRetry" size="small" :disabled="submission.state === 'pending'" @click="retryInjection(submission)">原请求重试</n-button>
        <n-button size="small" @click="copyDirectiveText(submission.intent.text)">复制原提交</n-button>
      </article>
    </section>
    <n-modal :show="showRestoreInjection" preset="card" title="恢复待核对构思" style="width: 520px" @update:show="restoreInjectionCandidate = null">
      <p>恢复会替换当前构思输入。请先复制需要保留的新稿；不会自动发送原构思。</p>
      <n-button @click="restoreInjectionCandidate = null">取消</n-button>
      <n-button type="primary" @click="confirmRestoreInjection">确认恢复</n-button>
    </n-modal>

    <p class="output-hint">每章保存后会逐项核对构思、前章承接和设定，并核对已设置的字数目标；发现问题、字数越界或检查未完成时暂停后续生成，正文保留。</p>

    <div class="progress-row">
      <n-progress type="line" :percentage="total ? Math.round((done / total) * 100) : 0" />
      <div class="progress-text">{{ progressText }}</div>
    </div>

    <p v-if="taskNotice && taskNotice !== progressText" class="task-notice">{{ taskNotice }}</p>
    <p v-if="statusNotice" class="task-notice">{{ statusNotice }}</p>
    <p v-if="checkingDrafts" class="output-hint">正在读取本地暂存片段…</p>
    <p v-if="recoveryNotice" class="task-notice" role="status">{{ recoveryNotice }}</p>
    <p v-if="storageNotice" class="task-notice" role="alert">{{ storageNotice }}</p>
    <p v-if="hasActiveTask || failedDrafts.length" class="output-hint">自动驾驶在后台独立运行，离开或刷新页面只断开本页监控，重新打开会自动连接已有任务。重试会跳过已有章节并保留此前的未完成稿；已自动暂存的片段可以找回，未得到暂存确认的内容请复制保留。后端重启后中断的任务需显式重试。</p>

    <section v-for="draft in activeDraftList" :key="draft.nodeId" class="draft-panel active-draft" aria-label="当前章节实时生成稿">
      <div class="draft-heading">生成中 · {{ draft.title }}</div>
      <p class="output-hint">尚未收到该章节的保存确认。</p>
      <n-tag :type="draft.saveState === 'saved' ? 'success' : 'warning'" size="small">{{ draftSaveLabel(draft) }}</n-tag>
      <textarea :value="draft.text" readonly class="draft-text" :aria-label="`实时生成稿 · ${draft.title}`" placeholder="等待正文输出…"></textarea>
    </section>

    <section v-if="failedDrafts.length" class="failed-drafts" aria-label="自动驾驶未完成稿">
      <div class="draft-heading">未完成稿 · {{ failedDrafts.length }} 份（含此前尝试）</div>
      <article v-for="draft in failedDrafts" :key="draft.id" class="draft-panel">
        <div class="draft-heading">{{ draft.title }} · 尝试 {{ draft.attempt }}</div>
        <n-tag type="warning" size="small">{{ draft.unconfirmed ? '章节保存状态未确认' : '未完成，未写入章节' }}</n-tag>
        <n-tag :type="draft.saveState === 'saved' ? 'success' : 'warning'" size="small">{{ draftSaveLabel(draft) }}</n-tag>
        <p class="output-hint">{{ draft.reason }}</p>
        <p v-if="draft.unconfirmed" class="output-hint">请重新核对正式章节是否已保存；这些暂存片段供比较或复制，不会自动写入章节。</p>
        <div class="draft-actions">
          <n-button size="small" :disabled="!draft.text" @click="copyDraft(draft)">复制未完成稿</n-button>
          <n-button size="small" @click="draft.collapsed = !draft.collapsed">{{ draft.collapsed ? '展开未完成稿' : '收起未完成稿' }}</n-button>
          <n-button size="small" type="warning" :disabled="discardPending" @click="requestDiscard(draft)">放弃未完成稿</n-button>
        </div>
        <p v-if="draft.copyFallback" class="output-hint">自动复制失败，请在下方选中并复制需要保留的内容。</p>
        <textarea
          v-show="!draft.collapsed"
          :ref="(element) => registerDraftTextRef(draft.id, element)"
          :value="draft.text"
          readonly
          class="draft-text"
          :aria-label="`未完成稿 · ${draft.title} · 尝试 ${draft.attempt}`"
          placeholder="本次未收到正文片段"
        ></textarea>
      </article>
    </section>

    <section v-if="qualityWarnings.length" class="quality-warnings" aria-label="已保存章节的质量检查提示">
      <div class="draft-heading">章节已保存，质量检查待处理</div>
      <p v-for="(warning, index) in qualityWarnings" :key="index" class="output-hint">{{ warning }}</p>
      <p v-if="paused" class="output-hint">请到“写作”页核对提示，按需修正并保存正文，再点击“恢复”继续后续章节。已保存章节会保留。</p>
    </section>

    <div ref="chartRef" class="chart"></div>
    <div v-if="!anchorSet" class="anchor-hint">
      未设置文风基准，暂不计算漂移分（可在「设定」面板的「文风基准」区配置）
    </div>

    <div v-if="driftWarnings.length" class="drift-warnings">
      <div class="drift-title">文风漂移告警</div>
      <div v-for="(w, i) in driftWarnings" :key="i" class="drift-item">
        <n-tag type="warning" size="small">漂移</n-tag>
        {{ w.message }}
      </div>
    </div>
    <n-modal :show="showDiscard" preset="card" title="放弃未完成稿？" style="width: min(480px, calc(100vw - 32px))" @update:show="cancelDiscard">
      <p>这会从本地数据库删除这一份暂存片段，放弃后无法恢复，请先复制需要保留的内容。</p>
      <template #footer>
        <div class="draft-actions modal-actions">
          <n-button :disabled="discardPending" @click="cancelDiscard">继续保留</n-button>
          <n-button type="warning" :loading="discardPending" :disabled="discardPending" @click="confirmDiscard">放弃未完成稿</n-button>
        </div>
      </template>
    </n-modal>
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
.autopilot-panel {
  padding: 16px;
  height: 100%;
  overflow: auto;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.inject-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.progress-row {
  margin-bottom: 12px;
}
.progress-text {
  margin-top: 4px;
  color: #666;
  min-height: 20px;
}
.chart {
  width: 100%;
  height: 280px;
}
.anchor-hint {
  color: #999;
  font-size: 12px;
  margin-top: 4px;
}
.drift-warnings {
  margin-top: 12px;
  border: 1px solid #f0c0a0;
  border-radius: 4px;
  padding: 12px;
}
.drift-title {
  color: #d07830;
  font-weight: 600;
  margin-bottom: 8px;
}
.drift-item {
  margin: 4px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.draft-panel,
.quality-warnings {
  border: 1px solid #e4b15a;
  border-radius: 4px;
  padding: 12px;
  margin: 12px 0;
  background: #fffaf0;
}
.active-draft {
  border-color: #d4dce5;
  background: #f8fafc;
}
.draft-heading {
  font-weight: 600;
  margin-bottom: 8px;
}
.output-hint,
.task-notice {
  font-size: 13px;
  line-height: 1.6;
  margin: 6px 0;
  color: #666;
}
.task-notice {
  color: #9c5c00;
}
.draft-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}
.modal-actions {
  justify-content: flex-end;
}
.draft-text {
  width: 100%;
  height: 160px;
  min-height: 80px;
  max-height: 360px;
  padding: 8px;
  box-sizing: border-box;
  font: inherit;
  line-height: 1.7;
  resize: vertical;
}
</style>
