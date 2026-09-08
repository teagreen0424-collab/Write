<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NList,
  NListItem,
  NMenu,
  NSelect,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import {
  ENTITY_TYPES,
  getEntityConfig,
  type EntityRecord,
  type EntityType,
} from '../types'

const props = defineProps<{
  novelId: string
  focusTarget?: { type: EntityType; id: string } | null
  refreshKey?: number
}>()

const message = useMessage()

const selectedType = ref<EntityType>('character')
const editingId = ref<string | null>(null)
const loading = ref(false)
const emptyEntities = (): Record<EntityType, EntityRecord[]> => ({
  character: [],
  location: [],
  world_setting: [],
  timeline_event: [],
  foreshadowing: [],
  relationship: [],
})
const allEntities = ref<Record<EntityType, EntityRecord[]>>(emptyEntities())
const form = ref<Record<string, any>>({})

const config = computed(() => getEntityConfig(selectedType.value))
const entities = computed(() => allEntities.value[selectedType.value])
const typeMenuOptions = ENTITY_TYPES.map((t) => ({ label: t.label, key: t.type }))

const NODE_TYPES: EntityType[] = ['character', 'location', 'world_setting', 'timeline_event', 'foreshadowing']

const entityOptions = computed(() => {
  const options: any[] = []
  for (const t of NODE_TYPES) {
    const cfg = getEntityConfig(t)
    for (const e of allEntities.value[t]) {
      options.push({ label: `${e[cfg.nameField]}（${cfg.label}）`, value: e.id })
    }
  }
  return options
})

function findEntityName(id: string): string {
  for (const t of NODE_TYPES) {
    const cfg = getEntityConfig(t)
    const e = allEntities.value[t].find((x) => x.id === id)
    if (e) return e[cfg.nameField]
  }
  return id
}

function displayName(item: EntityRecord): string {
  const cfg = config.value
  if (cfg.type === 'relationship') {
    return `${findEntityName(item.subject_id)} → ${findEntityName(item.object_id)}（${item.relation_type}）`
  }
  return item[cfg.nameField] ?? ''
}

async function loadAll() {
  loading.value = true
  try {
    const result = emptyEntities()
    for (const t of ENTITY_TYPES) {
      result[t.type] = await api.listEntities(props.novelId, t.apiPath)
    }
    allEntities.value = result
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function resetForm() {
  const cfg = config.value
  const next: Record<string, any> = {}
  for (const f of cfg.fields) {
    if (f.type === 'select') next[f.key] = f.options?.[0]?.value ?? ''
    else next[f.key] = ''
  }
  form.value = next
}

function startCreate() {
  editingId.value = null
  resetForm()
}

function startEdit(item: EntityRecord) {
  editingId.value = item.id
  const cfg = config.value
  const next: Record<string, any> = {}
  for (const f of cfg.fields) {
    const v = item[f.key]
    next[f.key] = f.type === 'json' ? (v == null ? '' : JSON.stringify(v, null, 2)) : (v ?? '')
  }
  form.value = next
}

function validate(): string | null {
  const cfg = config.value
  for (const f of cfg.fields) {
    if (!f.required) continue
    const v = form.value[f.key]
    if (v == null || String(v).trim() === '') return `${f.label} 为必填项`
  }
  for (const f of cfg.fields) {
    if (f.type !== 'json') continue
    const raw = form.value[f.key]
    if (raw == null || String(raw).trim() === '') continue
    try {
      JSON.parse(raw)
    } catch {
      return `${f.label} 不是合法 JSON`
    }
  }
  return null
}

function buildPayload(): Record<string, unknown> {
  const cfg = config.value
  const payload: Record<string, unknown> = {}
  for (const f of cfg.fields) {
    const raw = form.value[f.key]
    if (f.type === 'json') {
      if (raw == null || String(raw).trim() === '') continue
      payload[f.key] = JSON.parse(raw)
    } else {
      payload[f.key] = raw
    }
  }
  return payload
}

async function save() {
  const err = validate()
  if (err) {
    message.error(err)
    return
  }
  let payload: Record<string, unknown>
  try {
    payload = buildPayload()
  } catch {
    message.error('JSON 字段格式错误')
    return
  }
  try {
    if (editingId.value) {
      await api.updateEntity(props.novelId, config.value.apiPath, editingId.value, payload)
      message.success('已保存')
    } else {
      await api.createEntity(props.novelId, config.value.apiPath, payload)
      message.success('已创建')
    }
    await loadAll()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function remove() {
  if (!editingId.value) return
  if (!window.confirm('确认删除该记录？')) return
  try {
    await api.deleteEntity(props.novelId, config.value.apiPath, editingId.value)
    message.success('已删除')
    editingId.value = null
    resetForm()
    await loadAll()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function applyFocus(target: { type: EntityType; id: string }) {
  selectedType.value = target.type
  const item = allEntities.value[target.type]?.find((e) => e.id === target.id)
  if (item) startEdit(item)
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) {
      resetForm()
      loadAll()
    }
  },
  { immediate: true },
)

watch(
  () => props.focusTarget,
  (t) => {
    if (t) applyFocus(t)
  },
)

watch(
  () => props.refreshKey,
  () => {
    if (props.novelId) loadAll()
  },
)

onMounted(resetForm)
</script>

<template>
  <div class="settings-panel">
    <div class="type-sidebar">
      <n-menu :options="typeMenuOptions" v-model:value="selectedType" />
    </div>

    <div class="main">
      <div class="list-pane">
        <div class="pane-header">
          <span>{{ config.label }}列表</span>
          <n-button size="small" type="primary" @click="startCreate">新建</n-button>
        </div>
        <n-empty v-if="!loading && !entities.length" description="暂无数据" />
        <n-list v-else clickable hoverable>
          <n-list-item
            v-for="item in entities"
            :key="item.id"
            :class="{ active: item.id === editingId }"
            @click="startEdit(item)"
          >
            {{ displayName(item) }}
          </n-list-item>
        </n-list>
      </div>

      <div class="detail-pane">
        <h3>{{ editingId ? '编辑' : '新建' }} {{ config.label }}</h3>
        <n-form label-placement="top">
          <n-form-item
            v-for="f in config.fields"
            :key="f.key"
            :label="f.label"
            :required="f.required"
          >
            <n-input
              v-if="f.type === 'text'"
              v-model:value="form[f.key]"
              :placeholder="f.placeholder"
            />
            <n-input
              v-else-if="f.type === 'textarea'"
              v-model:value="form[f.key]"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 6 }"
              :placeholder="f.placeholder"
            />
            <n-select
              v-else-if="f.type === 'select'"
              v-model:value="form[f.key]"
              :options="f.options"
            />
            <n-input
              v-else-if="f.type === 'json'"
              v-model:value="form[f.key]"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 4 }"
              :placeholder="f.placeholder"
            />
            <n-select
              v-else-if="f.type === 'entity-select'"
              v-model:value="form[f.key]"
              :options="(entityOptions as any)"
              filterable
              clearable
            />
          </n-form-item>
        </n-form>
        <div class="actions">
          <n-button type="primary" @click="save">保存</n-button>
          <n-button v-if="editingId" type="error" @click="remove">删除</n-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-panel {
  display: flex;
  height: 100%;
}
.type-sidebar {
  width: 140px;
  border-right: 1px solid #e5e5e5;
  padding: 8px;
}
.main {
  flex: 1;
  display: flex;
  min-width: 0;
}
.list-pane {
  width: 280px;
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
.list-pane :deep(.n-list-item.active) {
  background: #e8f4ff;
}
.detail-pane {
  flex: 1;
  padding: 16px 20px;
  overflow: auto;
}
.detail-pane h3 {
  margin-top: 0;
}
.actions {
  margin-top: 16px;
  display: flex;
  gap: 12px;
}
</style>
