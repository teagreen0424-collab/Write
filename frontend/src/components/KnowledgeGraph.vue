<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api'
import {
  ENTITY_TYPES,
  getEntityConfig,
  type EntityRecord,
  type EntityType,
  type Relationship,
} from '../types'

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'edit', payload: { type: EntityType; id: string }): void }>()

const chartRef = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

const NODE_TYPES: EntityType[] = ['character', 'location', 'world_setting', 'timeline_event', 'foreshadowing']

async function loadAndRender() {
  if (!props.novelId || !chart) return
  const entities = {
    character: [] as EntityRecord[],
    location: [] as EntityRecord[],
    world_setting: [] as EntityRecord[],
    timeline_event: [] as EntityRecord[],
    foreshadowing: [] as EntityRecord[],
    relationship: [] as EntityRecord[],
  }
  for (const t of ENTITY_TYPES) {
    entities[t.type] = await api.listEntities(props.novelId, t.apiPath)
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
  const links = relationships.map((r) => ({
    source: r.subject_id,
    target: r.object_id,
    label: { show: true, formatter: r.relation_type },
  }))

  chart.setOption(
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

onMounted(() => {
  if (chartRef.value) {
    chart = echarts.init(chartRef.value)
    chart.on('click', (params: any) => {
      if (params.dataType === 'node' && params.data?.type && params.data?.id) {
        emit('edit', { type: params.data.type, id: params.data.id })
      }
    })
    window.addEventListener('resize', handleResize)
    loadAndRender()
  }
})

watch(
  () => props.novelId,
  () => {
    if (props.novelId) loadAndRender()
  },
)

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="graph-wrap">
    <div v-if="!novelId" class="hint">请先选择或创建作品</div>
    <div v-else ref="chartRef" class="chart"></div>
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
