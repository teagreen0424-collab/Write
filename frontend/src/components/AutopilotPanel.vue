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
const tensionList = ref<{ seq: number; score: number }[]>([])

const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

function renderChart() {
  if (!chart) return
  chart.setOption({
    title: { text: '张力曲线', left: 'center', textStyle: { fontSize: 14 } },
    xAxis: { type: 'category', data: tensionList.value.map((t) => `第${t.seq}章`) },
    yAxis: { type: 'value', min: 0, max: 10 },
    series: [
      { type: 'line', data: tensionList.value.map((t) => t.score), smooth: true, areaStyle: {} },
    ],
  })
}

async function loadTension() {
  if (!props.novelId) return
  const chapters = await api.listChapters(props.novelId)
  tensionList.value = chapters
    .filter((c) => c.tension_score != null)
    .map((c) => ({ seq: c.seq, score: c.tension_score as number }))
  renderChart()
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
      tensionList.value.push({ seq: ev.seq, score: ev.score })
      renderChart()
      break
    case 'drift':
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
  tensionList.value = []
  progressText.value = '启动中…'
  loadTension()
  streamEvents(`/api/v1/novels/${props.novelId}/autopilot/sse`, handleEvent).finally(() => {
    running.value = false
    loadTension()
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
  loadTension()
})

onBeforeUnmount(() => {
  chart?.dispose()
  chart = null
})

watch(
  () => props.novelId,
  () => {
    if (props.novelId) loadTension()
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
