<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api } from '../api'
import {
  ENTITY_TYPES,
  getEntityConfig,
  type EntityRecord,
  type EntityType,
  type Relationship,
} from '../types'

echarts.use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'edit', payload: { type: EntityType; id: string }): void }>()

const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let requestId = 0
let alive = true
const loadError = ref('')

const NODE_TYPES: EntityType[] = ['character', 'location', 'world_setting', 'timeline_event', 'foreshadowing']

async function loadAndRender() {
  const currentRequest = ++requestId
  const novelId = props.novelId
  const currentChart = chart
  loadError.value = ''
  currentChart?.clear()
  if (!novelId || !currentChart || !alive) return
  const entities = {
    character: [] as EntityRecord[],
    location: [] as EntityRecord[],
    world_setting: [] as EntityRecord[],
    timeline_event: [] as EntityRecord[],
    foreshadowing: [] as EntityRecord[],
    relationship: [] as EntityRecord[],
  }
  try {
    const loaded = await Promise.all(ENTITY_TYPES.map((t) => api.listEntities(novelId, t.apiPath)))
    if (!alive || currentRequest !== requestId || novelId !== props.novelId || chart !== currentChart) return
    ENTITY_TYPES.forEach((t, index) => { entities[t.type] = loaded[index] })
  } catch (error) {
    if (alive && currentRequest === requestId) loadError.value = (error as Error).message
    return
  }
  const relationships = entities.relationship as unknown as Relationship[]

  const categories = NODE_TYPES.map((t) => ({ name: getEntityConfig(t).label }))
  const nodes: any[] = []
  for (const t of NODE_TYPES) {
    const cfg = getEntityConfig(t)
    for (const e of entities[t]) {
      nodes.push({
        id: e.id,
        name: e[cfg.nameField] ?? '',
        category: NODE_TYPES.indexOf(t),
        type: t,
        typeLabel: cfg.label,
      })
    }
  }
  const nodeIds = new Set(nodes.map((node) => node.id))
  const links = relationships.filter((r) => nodeIds.has(r.subject_id) && nodeIds.has(r.object_id)).map((r) => ({
    source: r.subject_id,
    target: r.object_id,
    label: { show: true, formatter: r.relation_type },
  }))

  currentChart.setOption(
    {
      tooltip: {},
      legend: [{ data: categories.map((c) => c.name) }],
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          categories,
          data: nodes,
          links,
          force: { repulsion: 300, edgeLength: 140, gravity: 0.1 },
          label: { show: true, position: 'right' },
          emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
          lineStyle: { color: 'source', curveness: 0.2 },
        },
      ],
    },
    true,
  )
}

function handleResize() {
  chart?.resize()
}

function initializeChart() {
  if (chartRef.value && !chart) {
    chart = echarts.init(chartRef.value)
    chart.on('click', (params: any) => {
      if (params.dataType === 'node' && params.data?.type && params.data?.id) {
        emit('edit', { type: params.data.type, id: params.data.id })
      }
    })
    window.addEventListener('resize', handleResize)
  }
}

watch(
  [() => props.novelId, chartRef],
  () => {
    if (!chartRef.value) { chart?.dispose(); chart = null }
    initializeChart()
    void loadAndRender()
  },
  { flush: 'post' },
)

onBeforeUnmount(() => {
  alive = false
  requestId += 1
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="graph-wrap">
    <div v-if="!novelId" class="hint">请先选择或创建作品</div>
    <div v-if="loadError" class="hint">图谱加载失败：{{ loadError }} <button @click="loadAndRender">重试</button></div>
    <div v-if="novelId" ref="chartRef" class="chart"></div>
  </div>
</template>

<style scoped>
.graph-wrap {
  width: 100%;
  height: 100%;
}
.chart {
  width: 100%;
  height: 100%;
}
.hint {
  padding: 24px;
  color: #888;
}
</style>
