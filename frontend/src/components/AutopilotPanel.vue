<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { NButton, NInput, NProgress, NTag, useMessage } from 'naive-ui'
import { api, streamEvents } from '../api'
import type { AutopilotEvent } from '../types'

const props = defineProps<{ novelId: string }>()

const message = useMessage()

const running = ref(false)
const paused = ref(false)
const progressText = ref('')
const done = ref(0)
const total = ref(0)
const directive = ref('')
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

async function loadCurve() {
  if (!props.novelId) return
  try {
    const res = await api.getQualityCurve(props.novelId)
    driftThreshold.value = res.drift_threshold
    anchorSet.value = res.anchor_set
    curvePoints.value = res.chapters.map((c) => ({
      seq: c.seq,
      tension: c.tension_score,
      drift: c.drift_score,
    }))
    renderChart()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function handleEvent(ev: AutopilotEvent) {
  switch (ev.type) {
    case 'progress':
      progressText.value = ev.message || ''
      if (ev.done != null) done.value = ev.done
      if (ev.total != null) total.value = ev.total
      break
    case 'chapter_done':
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
      progressText.value = ev.message || '已暂停'
      break
    case 'error':
      message.error(ev.message || '发生错误')
      break
    case 'done':
      progressText.value = ev.message || '完成'
      break
  }
}

function start() {
  if (running.value) return
  running.value = true
  paused.value = false
  done.value = 0
  total.value = 0
  driftWarnings.value = []
  curvePoints.value = []
  progressText.value = '启动中…'
  loadCurve()
  streamEvents(`/api/v1/novels/${props.novelId}/autopilot/sse`, handleEvent).finally(() => {
    running.value = false
    loadCurve()
  })
}

async function pause() {
  try {
    await api.pauseAutopilot(props.novelId)
    paused.value = true
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function resume() {
  try {
    await api.resumeAutopilot(props.novelId)
    paused.value = false
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function inject() {
  if (!directive.value.trim()) {
    message.error('请输入构思')
    return
  }
  try {
    await api.injectAutopilot(props.novelId, directive.value)
    message.success('已注入构思')
    directive.value = ''
  } catch (e) {
    message.error((e as Error).message)
  }
}

onMounted(() => {
  if (chartRef.value) chart = echarts.init(chartRef.value)
  loadCurve()
})

onBeforeUnmount(() => {
  chart?.dispose()
  chart = null
})

watch(
  () => props.novelId,
  () => {
    if (props.novelId) loadCurve()
  },
)
</script>

<template>
  <div class="autopilot-panel">
    <div class="controls">
      <n-button type="primary" :disabled="running" @click="start">启动自动驾驶</n-button>
      <n-button :disabled="!running || paused" @click="pause">暂停</n-button>
      <n-button :disabled="!running || !paused" @click="resume">恢复</n-button>
      <n-tag v-if="running" :type="paused ? 'warning' : 'success'" size="small">
        {{ paused ? '已暂停' : '运行中' }}
      </n-tag>
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
</style>
