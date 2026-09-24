<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import type { OutlineLevel, OutlineNode } from '../types'
import OutlinePlanCard from './OutlinePlanCard.vue'

const props = defineProps<{ novelId: string; refreshKey?: number }>()

const message = useMessage()

const nodes = ref<OutlineNode[]>([])
const showModal = ref(false)
const showPlanner = ref(false)
const editingId = ref<string | null>(null)
const form = ref({ title: '', level: 'chapter' as OutlineLevel, summary: '', parent_id: '' })

const LEVEL_LABEL: Record<OutlineLevel, string> = { volume: '卷', act: '幕', chapter: '章' }
const LEVEL_OPTIONS = [
  { label: '卷', value: 'volume' },
  { label: '幕', value: 'act' },
  { label: '章', value: 'chapter' },
]

const childrenMap = computed(() => {
  const m: Record<string, OutlineNode[]> = {}
  for (const n of nodes.value) {
    ;(m[n.parent_id] ||= []).push(n)
  }
  for (const k of Object.keys(m)) m[k].sort((a, b) => a.sort_order - b.sort_order)
  return m
})

function childrenOf(id: string): OutlineNode[] {
  return childrenMap.value[id] ?? []
}

const volumes = computed(() => childrenOf(''))

async function load() {
  if (!props.novelId) return
  nodes.value = await api.listOutline(props.novelId)
}

function openCreate(parentId: string, level: OutlineLevel) {
  editingId.value = null
  form.value = { title: '', level, summary: '', parent_id: parentId }
  showModal.value = true
}

function openEdit(node: OutlineNode) {
  editingId.value = node.id
  form.value = { title: node.title, level: node.level, summary: node.summary, parent_id: node.parent_id }
  showModal.value = true
}

async function save() {
  if (!form.value.title.trim()) {
    message.error('标题不能为空')
    return
  }
  try {
    if (editingId.value) {
      await api.updateOutline(props.novelId, editingId.value, {
        title: form.value.title,
        summary: form.value.summary,
      })
    } else {
      await api.createOutline(props.novelId, {
        title: form.value.title,
        level: form.value.level,
        summary: form.value.summary,
        parent_id: form.value.parent_id,
      })
    }
    showModal.value = false
    await load()
    message.success('已保存')
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function remove(node: OutlineNode) {
  if (!window.confirm(`确认删除「${node.title}」？`)) return
  try {
    await api.deleteOutline(props.novelId, node.id)
    await load()
    message.success('已删除')
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function move(node: OutlineNode, dir: -1 | 1) {
  const siblings = childrenOf(node.parent_id)
  const idx = siblings.findIndex((s) => s.id === node.id)
  const target = idx + dir
  if (target < 0 || target >= siblings.length) return
  const order = siblings.map((s) => s.id)
  ;[order[idx], order[target]] = [order[target], order[idx]]
  await api.reorderOutline(props.novelId, order)
  await load()
}

async function onPlanApplied() {
  showPlanner.value = false
  await load()
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) load()
  },
  { immediate: true },
)

watch(
  () => props.refreshKey,
  () => {
    if (props.novelId) load()
  },
)
</script>

<template>
  <div class="outline-panel">
    <div class="toolbar">
      <n-button size="small" type="primary" @click="openCreate('', 'volume')">新增卷</n-button>
      <n-button size="small" @click="showPlanner = true">AI 生成一卷</n-button>
    </div>

    <n-empty v-if="!nodes.length" description="暂无大纲，请新增卷或生成一卷预览" />

    <n-modal v-model:show="showPlanner" preset="card" title="AI 大纲预览" style="width: min(800px, 94vw)">
      <OutlinePlanCard
        v-if="showPlanner"
        :novel-id="novelId"
        @applied="onPlanApplied"
        @cancel="showPlanner = false"
      />
    </n-modal>

    <div v-for="vol in volumes" :key="vol.id" class="node volume">
      <div class="row">
        <n-tag type="info" size="small">{{ LEVEL_LABEL[vol.level] }}</n-tag>
        <span class="title">{{ vol.title }}</span>
        <span class="ops">
          <n-button size="tiny" @click="move(vol, -1)">↑</n-button>
          <n-button size="tiny" @click="move(vol, 1)">↓</n-button>
          <n-button size="tiny" @click="openCreate(vol.id, 'act')">+幕</n-button>
          <n-button size="tiny" @click="openCreate(vol.id, 'chapter')">+章</n-button>
          <n-button size="tiny" @click="openEdit(vol)">编辑</n-button>
          <n-button size="tiny" type="error" @click="remove(vol)">删除</n-button>
        </span>
      </div>

      <div v-for="act in childrenOf(vol.id)" :key="act.id" class="node act">
        <div class="row">
          <n-tag type="success" size="small">{{ LEVEL_LABEL[act.level] }}</n-tag>
          <span class="title">{{ act.title }}</span>
          <span class="ops">
            <n-button size="tiny" @click="move(act, -1)">↑</n-button>
            <n-button size="tiny" @click="move(act, 1)">↓</n-button>
            <n-button size="tiny" @click="openCreate(act.id, 'chapter')">+章</n-button>
            <n-button size="tiny" @click="openEdit(act)">编辑</n-button>
            <n-button size="tiny" type="error" @click="remove(act)">删除</n-button>
          </span>
        </div>

        <div v-for="ch in childrenOf(act.id)" :key="ch.id" class="node chapter">
          <div class="row">
            <n-tag size="small">{{ LEVEL_LABEL[ch.level] }}</n-tag>
            <span class="title">{{ ch.title }}</span>
            <span class="ops">
              <n-button size="tiny" @click="move(ch, -1)">↑</n-button>
              <n-button size="tiny" @click="move(ch, 1)">↓</n-button>
              <n-button size="tiny" @click="openEdit(ch)">编辑</n-button>
              <n-button size="tiny" type="error" @click="remove(ch)">删除</n-button>
            </span>
          </div>
        </div>
      </div>
    </div>

    <n-modal v-model:show="showModal" preset="card" :title="editingId ? '编辑节点' : '新建节点'" style="width: 480px">
      <n-form label-placement="top">
        <n-form-item label="标题" required>
          <n-input v-model:value="form.title" placeholder="节点标题" />
        </n-form-item>
        <n-form-item v-if="!editingId" label="层级">
          <n-select v-model:value="form.level" :options="(LEVEL_OPTIONS as any)" />
        </n-form-item>
        <n-form-item label="概要">
          <n-input v-model:value="form.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
        </n-form-item>
      </n-form>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showModal = false">取消</n-button>
          <n-button type="primary" @click="save">保存</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.outline-panel {
  padding: 12px;
  height: 100%;
  overflow: auto;
}
.toolbar {
  margin-bottom: 12px;
}
.node {
  margin-bottom: 4px;
}
.node.act {
  margin-left: 24px;
}
.node.chapter {
  margin-left: 48px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 4px;
}
.row:hover {
  background: #f5f5f5;
}
.title {
  font-weight: 500;
}
.ops {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
