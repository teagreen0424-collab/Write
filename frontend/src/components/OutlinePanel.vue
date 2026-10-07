<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import { createAsyncScope, type AsyncScopeToken } from '../utils/asyncScope'
import OutlinePlanCard from './OutlinePlanCard.vue'

const props = defineProps<{ novelId: string; refreshKey?: number }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const nodes = ref<OutlineNode[]>([])
const showModal = ref(false)
const showPlanner = ref(false)
const editingId = ref<string | null>(null)
const form = ref({ title: '', level: 'chapter' as OutlineLevel, summary: '', parent_id: '' })
const formBaseline = ref(JSON.stringify(form.value))
const formFingerprint = computed(() => JSON.stringify(form.value))
const saving = ref(false)
const moving = ref(false)
const loading = ref(false)
const asyncScope = createAsyncScope(() => props.novelId)
let loadSequence = 0
let modalRevision = 0
const draggedNodeId = ref<string | null>(null)
let dragScope: AsyncScopeToken | null = null
const dirty = computed(() => (showModal.value && formFingerprint.value !== formBaseline.value) || showPlanner.value || saving.value)
const plannerApplied = ref<() => Promise<void>>(async () => {})
watch(dirty, (value) => emit('dirty-change', value), { immediate: true, flush: 'sync' })

function confirmEditorChange(): boolean {
  return (!dirty.value) || window.confirm('当前大纲有未保存编辑、预览或正在保存，确定放弃本页编辑并继续吗？')
}

function guardUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

function handleModalVisibility(visible: boolean) {
  if (!visible && !confirmEditorChange()) return
  modalRevision += 1
  showModal.value = visible
}

function closePlanner() {
  if (!confirmEditorChange()) return
  modalRevision += 1
  showPlanner.value = false
}

function openPlanner() {
  if (!props.novelId || !confirmEditorChange()) return
  const scope = asyncScope.capture()
  const revision = ++modalRevision
  showModal.value = false
  showPlanner.value = true
  plannerApplied.value = async () => {
    if (!asyncScope.isCurrent(scope) || revision !== modalRevision) return
    showPlanner.value = false
    modalRevision += 1
    await load()
  }
}

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

async function load(reportError = true): Promise<boolean> {
  if (!props.novelId) return false
  const scope = asyncScope.capture()
  const sequence = ++loadSequence
  loading.value = true
  try {
    const result = await api.listOutline(scope.identity)
    if (!asyncScope.isCurrent(scope) || sequence !== loadSequence) return false
    nodes.value = result
    return true
  } catch (error) {
    if (reportError && asyncScope.isCurrent(scope) && sequence === loadSequence) message.error((error as Error).message)
    return false
  } finally {
    if (asyncScope.isCurrent(scope) && sequence === loadSequence) loading.value = false
  }
}

function openCreate(parentId: string, level: OutlineLevel) {
  if (!props.novelId || !confirmEditorChange()) return
  modalRevision += 1
  showPlanner.value = false
  editingId.value = null
  form.value = { title: '', level, summary: '', parent_id: parentId }
  formBaseline.value = formFingerprint.value
  showModal.value = true
}

function openEdit(node: OutlineNode) {
  if (!props.novelId || !nodes.value.some((item) => item.id === node.id) || !confirmEditorChange()) return
  modalRevision += 1
  showPlanner.value = false
  editingId.value = node.id
  form.value = { title: node.title, level: node.level, summary: node.summary, parent_id: node.parent_id }
  formBaseline.value = formFingerprint.value
  showModal.value = true
}

function changeCreateLevel(level: OutlineLevel) {
  if (saving.value || editingId.value) return
  form.value.level = level
}

async function save() {
  if (!props.novelId || !showModal.value || saving.value) return
  if (!form.value.title.trim()) {
    message.error('标题不能为空')
    return
  }
  const scope = asyncScope.capture()
  const revision = modalRevision
  const itemId = editingId.value
  const submittedForm = formFingerprint.value
  const submitted = { ...form.value }
  saving.value = true
  try {
    const saved = itemId
      ? await api.updateOutline(scope.identity, itemId, { title: submitted.title, summary: submitted.summary })
      : await api.createOutline(scope.identity, submitted)
    if (!asyncScope.isCurrent(scope)) return
    if (revision === modalRevision && editingId.value === itemId) {
      editingId.value = saved.id
      formBaseline.value = submittedForm
      if (formFingerprint.value === submittedForm) {
        showModal.value = false
        modalRevision += 1
      }
    }
    await load()
    if (asyncScope.isCurrent(scope)) message.success('已保存')
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) saving.value = false
  }
}

async function remove(node: OutlineNode) {
  if (!props.novelId || saving.value || !nodes.value.some((item) => item.id === node.id)) return
  if (!window.confirm(`确认删除「${node.title}」？`)) return
  const scope = asyncScope.capture()
  saving.value = true
  try {
    await api.deleteOutline(scope.identity, node.id)
    if (!asyncScope.isCurrent(scope)) return
    loadSequence += 1
    const deletedIds = new Set([node.id])
    // 后端拒绝删除仍有子节点的节点；本地子树可能只是未刷新的旧读取结果。
    let foundChild = true
    while (foundChild) {
      foundChild = false
      for (const item of nodes.value) {
        if (!deletedIds.has(item.id) && deletedIds.has(item.parent_id)) {
          deletedIds.add(item.id)
          foundChild = true
        }
      }
    }
    nodes.value = nodes.value.filter((item) => !deletedIds.has(item.id))
    if (draggedNodeId.value && deletedIds.has(draggedNodeId.value)) clearDrag()
    if (editingId.value && deletedIds.has(editingId.value)) {
      editingId.value = null
      if (showModal.value && formFingerprint.value !== formBaseline.value) {
        // 删除发生后，保留作者新输入，但不得继续更新已经不存在的 ID。
        formBaseline.value = '{}'
      } else showModal.value = false
      modalRevision += 1
    }
    const refreshed = await load(false)
    if (asyncScope.isCurrent(scope)) {
      if (refreshed) message.success('已删除')
      else message.warning('大纲已删除，但列表刷新失败；已移除本页节点，请稍后刷新。')
    }
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) saving.value = false
  }
}

async function move(node: OutlineNode, dir: -1 | 1) {
  if (!props.novelId || moving.value || loading.value || saving.value) return
  const siblings = childrenOf(node.parent_id)
  const idx = siblings.findIndex((s) => s.id === node.id)
  const target = idx + dir
  if (idx < 0 || target < 0 || target >= siblings.length) return
  const order = siblings.map((s) => s.id)
  ;[order[idx], order[target]] = [order[target], order[idx]]
  await reorder(order)
}

async function reorder(order: string[]) {
  if (!props.novelId || moving.value || loading.value || saving.value) return
  const scope = asyncScope.capture()
  moving.value = true
  try {
    await api.reorderOutline(scope.identity, order)
    if (asyncScope.isCurrent(scope)) await load()
  } catch (error) {
    if (asyncScope.isCurrent(scope)) message.error((error as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) moving.value = false
  }
}

function clearDrag() {
  draggedNodeId.value = null
  dragScope = null
}

function startDrag(node: OutlineNode, event: DragEvent) {
  if (!props.novelId || moving.value || loading.value || saving.value || !nodes.value.some((item) => item.id === node.id)) {
    event.preventDefault()
    return
  }
  draggedNodeId.value = node.id
  dragScope = asyncScope.capture()
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', node.id)
  }
}

function canDrop(target: OutlineNode): boolean {
  if (!dragScope || !asyncScope.isCurrent(dragScope) || moving.value || loading.value || saving.value) return false
  const source = nodes.value.find((node) => node.id === draggedNodeId.value)
  return !!source && source.id !== target.id && source.parent_id === target.parent_id
    && source.level === target.level && nodes.value.some((node) => node.id === target.id)
}

function dragOver(target: OutlineNode, event: DragEvent) {
  if (!canDrop(target)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
}

async function dropOn(target: OutlineNode, event: DragEvent) {
  if (!canDrop(target)) { clearDrag(); return }
  event.preventDefault()
  const order = childrenOf(target.parent_id).map((node) => node.id)
  const from = order.indexOf(draggedNodeId.value!)
  const to = order.indexOf(target.id)
  const [moved] = order.splice(from, 1)
  order.splice(to, 0, moved)
  clearDrag()
  await reorder(order)
}

watch(
  () => props.novelId,
  () => {
    asyncScope.invalidate()
    modalRevision += 1
    nodes.value = []
    showModal.value = false
    showPlanner.value = false
    editingId.value = null
    saving.value = false
    moving.value = false
    loading.value = false
    clearDrag()
    form.value = { title: '', level: 'chapter', summary: '', parent_id: '' }
    formBaseline.value = formFingerprint.value
    if (props.novelId) load()
  },
  { immediate: true, flush: 'sync' },
)
onMounted(() => window.addEventListener('beforeunload', guardUnload))
onBeforeUnmount(() => {
  asyncScope.dispose()
  clearDrag()
  window.removeEventListener('beforeunload', guardUnload)
  emit('dirty-change', false)
})

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
      <n-button size="small" type="primary" :disabled="!novelId" @click="openCreate('', 'volume')">新增卷</n-button>
      <n-button size="small" :disabled="!novelId" @click="openPlanner">AI 生成一卷</n-button>
    </div>

    <n-empty v-if="!nodes.length" description="暂无大纲，请新增卷或生成一卷预览" />

    <n-modal :show="showPlanner" preset="card" title="AI 大纲预览" style="width: min(800px, 94vw)" @update:show="(visible: boolean) => { if (!visible) closePlanner() }">
      <OutlinePlanCard
        v-if="showPlanner"
        :key="modalRevision"
        :novel-id="novelId"
        @applied="plannerApplied"
        @cancel="closePlanner"
      />
    </n-modal>

    <div v-for="vol in volumes" :key="vol.id" class="node volume">
      <div class="row" :class="{ dragging: draggedNodeId === vol.id, 'drop-target': canDrop(vol) }"
        :draggable="!moving && !loading && !saving" @dragstart="startDrag(vol, $event)" @dragover="dragOver(vol, $event)" @drop="dropOn(vol, $event)" @dragend="clearDrag">
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

      <div v-for="act in childrenOf(vol.id)" :key="act.id" class="node" :class="act.level === 'chapter' ? 'chapter' : 'act'">
        <div class="row" :class="{ dragging: draggedNodeId === act.id, 'drop-target': canDrop(act) }"
          :draggable="!moving && !loading && !saving" @dragstart="startDrag(act, $event)" @dragover="dragOver(act, $event)" @drop="dropOn(act, $event)" @dragend="clearDrag">
          <n-tag :type="act.level === 'act' ? 'success' : 'default'" size="small">{{ LEVEL_LABEL[act.level] }}</n-tag>
          <span class="title">{{ act.title }}</span>
          <span class="ops">
            <n-button size="tiny" @click="move(act, -1)">↑</n-button>
            <n-button size="tiny" @click="move(act, 1)">↓</n-button>
            <n-button v-if="act.level === 'act'" size="tiny" @click="openCreate(act.id, 'chapter')">+章</n-button>
            <n-button size="tiny" @click="openEdit(act)">编辑</n-button>
            <n-button size="tiny" type="error" @click="remove(act)">删除</n-button>
          </span>
        </div>

        <div v-for="ch in (act.level === 'act' ? childrenOf(act.id) : [])" :key="ch.id" class="node chapter">
          <div class="row" :class="{ dragging: draggedNodeId === ch.id, 'drop-target': canDrop(ch) }"
            :draggable="!moving && !loading && !saving" @dragstart="startDrag(ch, $event)" @dragover="dragOver(ch, $event)" @drop="dropOn(ch, $event)" @dragend="clearDrag">
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

    <n-modal :show="showModal" preset="card" :title="editingId ? '编辑节点' : '新建节点'" style="width: 480px" @update:show="handleModalVisibility">
      <n-form label-placement="top">
        <n-form-item label="标题" required>
          <n-input v-model:value="form.title" placeholder="节点标题" />
        </n-form-item>
        <n-form-item v-if="!editingId" label="层级">
          <n-select :value="form.level" :disabled="saving" :options="(LEVEL_OPTIONS as any)" @update:value="changeCreateLevel" />
        </n-form-item>
        <n-form-item label="概要">
          <n-input v-model:value="form.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
        </n-form-item>
      </n-form>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="handleModalVisibility(false)">取消</n-button>
          <n-button type="primary" :loading="saving" @click="save">保存</n-button>
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
.row[draggable="true"] {
  cursor: grab;
}
.row.dragging {
  opacity: 0.55;
}
.row.drop-target {
  outline: 1px dashed #18a058;
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
