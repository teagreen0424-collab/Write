<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { NButton, NInput, NModal, NProgress, NTag, useMessage } from 'naive-ui'
import { api, streamEvents } from '../api'
import type { AutopilotEvent } from '../types'

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const running = ref(false)
const serverRunning = ref(false)
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
const directive = ref('')
interface NodeDraft { nodeId: string; title: string; text: string }
interface FailedDraft extends NodeDraft {
  id: number
  attempt: number
  reason: string
  unconfirmed: boolean
  collapsed: boolean
  copyFallback: boolean
}
const activeDrafts = ref<Record<string, NodeDraft>>({})
const failedDrafts = ref<FailedDraft[]>([])
const qualityWarnings = ref<string[]>([])
const showDiscard = ref(false)
const discardDraftId = ref<number | null>(null)
const draftTextRefs = new Map<number, HTMLTextAreaElement>()
const activeDraftList = computed(() => Object.values(activeDrafts.value))
const hasActiveTask = computed(() => running.value || serverRunning.value)
const hasPendingOutput = computed(() => hasActiveTask.value || activeDraftList.value.length > 0 || failedDrafts.value.length > 0)
let runId = 0
let statusRequestId = 0
let attempt = 0
let draftId = 0
let unmounted = false
let connectionController: AbortController | null = null

function isCurrentRun(expectedRunId: number, novelId: string) {
  return !unmounted && expectedRunId === runId && props.novelId === novelId
}

function invalidateStatusRequests() {
  statusRequestId += 1
  checkingStatus.value = false
}

function disconnect() {
  runId += 1
  invalidateStatusRequests()
  connectionController?.abort()
  connectionController = null
  running.value = false
  serverRunning.value = false
  paused.value = false
  pauseRequested.value = false
  controlPending.value = false
  checkingStatus.value = false
}

function preserveNode(nodeId: string, reason: string, unconfirmed: boolean, title?: string) {
  const draft = activeDrafts.value[nodeId]
  failedDrafts.value.push({
    id: ++draftId,
    attempt,
    nodeId,
    title: title || draft?.title || '未命名章节',
    text: draft?.text || '',
    reason,
    unconfirmed,
    collapsed: false,
    copyFallback: false,
  })
  delete activeDrafts.value[nodeId]
}

function preserveActiveDrafts(reason: string) {
  for (const nodeId of Object.keys(activeDrafts.value)) preserveNode(nodeId, reason, true)
}

function handleConnectionError(error: string, code?: string) {
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
  showDiscard.value = false
  discardDraftId.value = null
}

function confirmDiscard() {
  const id = discardDraftId.value
  cancelDiscard()
  failedDrafts.value = failedDrafts.value.filter((draft) => draft.id !== id)
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!hasPendingOutput.value) return
  event.preventDefault()
  event.returnValue = ''
}

async function loadTaskStatus(expectedRunId = runId, novelId = props.novelId) {
  if (!novelId || !isCurrentRun(expectedRunId, novelId)) return
  const requestId = ++statusRequestId
  const isLatestStatusRequest = () => requestId === statusRequestId && isCurrentRun(expectedRunId, novelId)
  checkingStatus.value = true
  try {
    const status = await api.autopilotStatus(novelId)
    if (!isLatestStatusRequest()) return
    serverRunning.value = status.running
    if (!running.value) {
      paused.value = false
      pauseRequested.value = status.running && !!status.paused
    }
    statusNotice.value = status.running && !running.value
      ? '后台任务仍在运行，本页面没有实时监控连接。可以请求暂停、取消暂停或再次核对状态。'
      : !status.running && connectionUnconfirmed.value
        ? '后台当前没有运行中的任务；本次连接的完成状态仍需核对章节。' : ''
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
  try {
    const res = await api.getQualityCurve(novelId)
    if (!isCurrentRun(expectedRunId, novelId)) return
    driftThreshold.value = res.drift_threshold
    anchorSet.value = res.anchor_set
    curvePoints.value = res.chapters.map((c) => ({
      seq: c.seq,
      tension: c.tension_score,
      drift: c.drift_score,
    }))
    renderChart()
  } catch (e) {
    if (isCurrentRun(expectedRunId, novelId)) message.error((e as Error).message)
  }
}

function handleEvent(ev: AutopilotEvent) {
  switch (ev.type) {
    case 'progress':
      serverRunning.value = true
      progressText.value = ev.message || ''
      if (ev.done != null) done.value = ev.done
      if (ev.total != null) total.value = ev.total
      if (ev.phase === 'generating' && ev.node_id) {
        paused.value = false
        const existing = activeDrafts.value[ev.node_id]
        activeDrafts.value[ev.node_id] = {
          nodeId: ev.node_id,
          title: ev.title || existing?.title || '未命名章节',
          text: existing?.text || '',
        }
      }
      break
    case 'text':
      if (!ev.node_id || typeof ev.content !== 'string') break
      if (!activeDrafts.value[ev.node_id]) {
        activeDrafts.value[ev.node_id] = { nodeId: ev.node_id, title: ev.title || '未命名章节', text: '' }
      }
      activeDrafts.value[ev.node_id].text += ev.content
      break
    case 'chapter_done':
      if (ev.node_id) delete activeDrafts.value[ev.node_id]
      progressText.value = `第${ev.seq}章生成完成`
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
      pauseRequested.value = false
      progressText.value = ev.message || '已暂停'
      break
    case 'error':
      if (ev.scope === 'quality' || ev.saved === true) {
        qualityWarnings.value.push(ev.message || '章节已保存，但质量检查失败')
      } else if (ev.scope === 'chapter' && ev.node_id) {
        preserveNode(ev.node_id, ev.message || '章节生成失败', ev.saved !== false, ev.title)
      } else if (ev.fatal || ev.scope === 'task') {
        invalidateStatusRequests()
        statusNotice.value = ''
        preserveActiveDrafts(ev.message || '任务异常，完成状态未确认')
        running.value = false
        paused.value = false
        pauseRequested.value = false
        connectionUnconfirmed.value = ev.code !== 'autopilot_busy' && ev.code !== 'task_start_failed'
        taskNotice.value = ev.message || '任务未完成'
        progressText.value = taskNotice.value
      }
      message.error(ev.message || '发生错误')
      break
    case 'done':
      invalidateStatusRequests()
      statusNotice.value = ''
      preserveActiveDrafts('任务已结束，但未收到该章节的保存确认')
      running.value = false
      serverRunning.value = false
      paused.value = false
      pauseRequested.value = false
      connectionUnconfirmed.value = false
      if (ev.done != null) done.value = ev.done
      if (ev.total != null) total.value = ev.total
      progressText.value = ev.message || '完成'
      break
  }
}

async function start() {
  if (hasActiveTask.value || !props.novelId) return
  invalidateStatusRequests()
  preserveActiveDrafts('上次连接未收到章节保存确认')
  const novelId = props.novelId
  const currentRunId = ++runId
  attempt += 1
  connectionController = new AbortController()
  const controller = connectionController
  running.value = true
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
  loadCurve(currentRunId, novelId)
  try {
    await streamEvents(`/api/v1/novels/${novelId}/autopilot/sse`, (event: AutopilotEvent) => {
      if (isCurrentRun(currentRunId, novelId)) handleEvent(event)
    }, {
      signal: controller.signal,
      onError: (error, code) => {
        if (isCurrentRun(currentRunId, novelId)) handleConnectionError(error, code)
      },
    })
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId) && !controller.signal.aborted) {
      handleConnectionError((e as Error).message, 'STREAM_INCOMPLETE')
    }
  } finally {
    if (isCurrentRun(currentRunId, novelId)) {
      running.value = false
      paused.value = false
      pauseRequested.value = false
      connectionController = null
      await Promise.all([loadCurve(currentRunId, novelId), loadTaskStatus(currentRunId, novelId)])
    }
  }
}

async function pause() {
  if (!hasActiveTask.value || paused.value || pauseRequested.value || controlPending.value) return
  const currentRunId = runId
  const novelId = props.novelId
  controlPending.value = true
  try {
    await api.pauseAutopilot(novelId)
    if (isCurrentRun(currentRunId, novelId) && hasActiveTask.value) {
      invalidateStatusRequests()
      pauseRequested.value = true
    }
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId)) message.error((e as Error).message)
  } finally {
    if (isCurrentRun(currentRunId, novelId)) controlPending.value = false
  }
}

async function resume() {
  if (!hasActiveTask.value || (!paused.value && !pauseRequested.value) || controlPending.value) return
  const currentRunId = runId
  const novelId = props.novelId
  controlPending.value = true
  try {
    await api.resumeAutopilot(novelId)
    if (isCurrentRun(currentRunId, novelId) && hasActiveTask.value) {
      invalidateStatusRequests()
      paused.value = false
      pauseRequested.value = false
    }
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId)) message.error((e as Error).message)
  } finally {
    if (isCurrentRun(currentRunId, novelId)) controlPending.value = false
  }
}

async function inject() {
  if (!running.value) return
  if (!directive.value.trim()) {
    message.error('请输入构思')
    return
  }
  const currentRunId = runId
  const novelId = props.novelId
  const submittedDirective = directive.value
  try {
    await api.injectAutopilot(novelId, submittedDirective)
    if (!isCurrentRun(currentRunId, novelId) || !running.value) return
    message.success('已注入构思')
    if (directive.value === submittedDirective) directive.value = ''
  } catch (e) {
    if (isCurrentRun(currentRunId, novelId)) message.error((e as Error).message)
  }
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
  if (chartRef.value) chart = echarts.init(chartRef.value)
  loadCurve()
  loadTaskStatus()
})

onBeforeUnmount(() => {
  unmounted = true
  disconnect()
  window.removeEventListener('beforeunload', handleBeforeUnload)
  chart?.dispose()
  chart = null
})

watch(
  () => props.novelId,
  () => {
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
    statusNotice.value = ''
    cancelDiscard()
    if (props.novelId) {
      loadCurve()
      loadTaskStatus()
    }
  },
)
watch(hasPendingOutput, (dirty) => emit('dirty-change', dirty), { immediate: true })
</script>

<template>
  <div class="autopilot-panel">
    <div class="controls">
      <n-button type="primary" :disabled="hasActiveTask || checkingStatus || !novelId" @click="start">{{ failedDrafts.length ? '重试未完成章节' : '启动自动驾驶' }}</n-button>
      <n-button :disabled="!hasActiveTask || paused || pauseRequested || controlPending" @click="pause">暂停</n-button>
      <n-button :disabled="!hasActiveTask || (!paused && !pauseRequested) || controlPending" @click="resume">{{ pauseRequested && !paused ? '取消暂停' : '恢复' }}</n-button>
      <n-button :disabled="checkingStatus || !novelId" size="small" @click="loadTaskStatus()">核对后台状态</n-button>
      <n-tag v-if="running" :type="paused || pauseRequested ? 'warning' : 'success'" size="small">
        {{ paused ? '已暂停' : pauseRequested ? '等待章节结束后暂停' : '运行中' }}
      </n-tag>
      <n-tag v-else-if="serverRunning" size="small" type="warning">{{ pauseRequested ? '后台已请求暂停（未监控）' : '后台运行中（未监控）' }}</n-tag>
      <n-tag v-else-if="connectionUnconfirmed" size="small" type="warning">完成状态未确认</n-tag>
      <n-tag v-else size="small" type="default">空闲</n-tag>
    </div>

    <div class="inject-row">
      <n-input
        v-model:value="directive"
        placeholder="注入构思（应用到下一章，如：让主角与 B 决裂）"
        style="flex: 1"
      />
      <n-button type="info" :disabled="!running" @click="inject">注入</n-button>
    </div>

    <div class="progress-row">
      <n-progress type="line" :percentage="total ? Math.round((done / total) * 100) : 0" />
      <div class="progress-text">{{ progressText }}</div>
    </div>

    <p v-if="taskNotice && taskNotice !== progressText" class="task-notice">{{ taskNotice }}</p>
    <p v-if="statusNotice" class="task-notice">{{ statusNotice }}</p>
    <p v-if="hasActiveTask || failedDrafts.length" class="output-hint">重试会跳过已有章节并保留下方此前的未完成稿。离开页面会断开本机连接，不能保证后台任务停止；离开前请复制需要保留的输出。</p>

    <section v-for="draft in activeDraftList" :key="draft.nodeId" class="draft-panel active-draft" aria-label="当前章节实时生成稿">
      <div class="draft-heading">生成中 · {{ draft.title }}</div>
      <p class="output-hint">尚未收到该章节的保存确认。</p>
      <textarea :value="draft.text" readonly class="draft-text" :aria-label="`实时生成稿 · ${draft.title}`" placeholder="等待正文输出…"></textarea>
    </section>

    <section v-if="failedDrafts.length" class="failed-drafts" aria-label="自动驾驶未完成稿">
      <div class="draft-heading">未完成稿 · {{ failedDrafts.length }} 份（含此前尝试）</div>
      <article v-for="draft in failedDrafts" :key="draft.id" class="draft-panel">
        <div class="draft-heading">{{ draft.title }} · 尝试 {{ draft.attempt }}</div>
        <n-tag type="warning" size="small">{{ draft.unconfirmed ? '保存状态未确认' : '未完成，未保存' }}</n-tag>
        <p class="output-hint">{{ draft.reason }}</p>
        <p v-if="draft.unconfirmed" class="output-hint">请重新核对章节是否已保存；这些片段暂留此处供比较或复制。</p>
        <div class="draft-actions">
          <n-button size="small" :disabled="!draft.text" @click="copyDraft(draft)">复制未完成稿</n-button>
          <n-button size="small" @click="draft.collapsed = !draft.collapsed">{{ draft.collapsed ? '展开未完成稿' : '收起未完成稿' }}</n-button>
          <n-button size="small" type="warning" @click="requestDiscard(draft)">放弃未完成稿</n-button>
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
      <p>放弃后无法恢复这些片段，请先复制需要保留的内容。</p>
      <template #footer>
        <div class="draft-actions modal-actions">
          <n-button @click="cancelDiscard">继续保留</n-button>
          <n-button type="warning" @click="confirmDiscard">放弃未完成稿</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
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
