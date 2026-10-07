<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import { chapterLabel } from '../utils/chapter'
import { createAsyncScope } from '../utils/asyncScope'
import {
  ENTITY_TYPES,
  getEntityConfig,
  type ChapterMetadata,
  type EntityRecord,
  type EntityType,
} from '../types'

const props = defineProps<{
  novelId: string
  focusTarget?: { type: EntityType; id: string } | null
  refreshKey?: number
}>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const selectedType = ref<EntityType>('character')
const editingId = ref<string | null>(null)
const loading = ref(false)
const rebuildingIndex = ref(false)
const saving = ref(false)
const savingAnchor = ref(false)
const asyncScope = createAsyncScope(() => props.novelId)
let entityLoadSequence = 0
let metaLoadSequence = 0
let selectionRevision = 0
let anchorRevision = 0
let anchorSaveRevision = 0
let anchorLoadSequence = 0
let pendingFocus: { type: EntityType; id: string } | null = null
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
const formBaseline = ref('{}')
const formFingerprint = computed(() => JSON.stringify(form.value))
const entityDirty = computed(() => formFingerprint.value !== formBaseline.value)

// 文风基准：决定定稿时是否计算文风漂移分
const styleAnchor = ref('')
const anchorBaseline = ref('')
const anchorChapterId = ref<string | null>(null)
const chapters = ref<ChapterMetadata[]>([])
const loadingAnchor = ref(false)
const anchorSet = computed(() => styleAnchor.value.trim().length > 0)
const chapterOptions = computed(() =>
  chapters.value.map((c) => ({ label: chapterLabel(c), value: c.id })),
)
const dirty = computed(() => entityDirty.value || styleAnchor.value !== anchorBaseline.value || saving.value || savingAnchor.value)
watch(dirty, (value) => emit('dirty-change', value), { immediate: true, flush: 'sync' })
watch(styleAnchor, () => { anchorRevision += 1 }, { flush: 'sync' })

function confirmEntityChange(): boolean {
  return (!entityDirty.value && !saving.value) || window.confirm('当前设定有未保存修改或正在保存，确定放弃本页编辑并继续吗？')
}

function guardUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 从选中章节取正文前 800 字作为基准
async function pickAnchorChapter(id: string | null) {
  anchorChapterId.value = id
  const sequence = ++anchorLoadSequence
  loadingAnchor.value = false
  if (!id || !props.novelId) return
  const scope = asyncScope.capture()
  const revision = anchorRevision, saveRevision = anchorSaveRevision
  const isCurrent = () => asyncScope.isCurrent(scope) && sequence === anchorLoadSequence
    && anchorChapterId.value === id && revision === anchorRevision && saveRevision === anchorSaveRevision
  loadingAnchor.value = true
  try {
    const chapter = await api.getChapter(scope.identity, id)
    if (!isCurrent()) return
    if (chapter.id !== id || chapter.novel_id !== scope.identity) {
      message.warning('章节读取响应与目标不一致，当前文风基准已保留')
      return
    }
    styleAnchor.value = (chapter.content || '').slice(0, 800)
  } catch (error) {
    if (isCurrent()) message.error(`文风章节读取失败，当前基准已保留：${(error as Error).message}`)
  } finally {
    if (asyncScope.isCurrent(scope) && sequence === anchorLoadSequence) loadingAnchor.value = false
  }
}

async function saveAnchor() {
  if (!props.novelId || savingAnchor.value) return
  const scope = asyncScope.capture()
  const submitted = styleAnchor.value
  anchorSaveRevision += 1
  anchorLoadSequence += 1
  loadingAnchor.value = false
  savingAnchor.value = true
  try {
    await api.updateNovel(scope.identity, { style_anchor: submitted })
    if (!asyncScope.isCurrent(scope)) return
    anchorBaseline.value = submitted
    message.success('已保存文风基准')
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) savingAnchor.value = false
  }
}

async function rebuildIndex() {
  if (!props.novelId || !window.confirm('为本作品重建设定检索索引？不会修改设定或章节正文。')) return
  if (rebuildingIndex.value) return
  const scope = asyncScope.capture()
  rebuildingIndex.value = true
  try {
    const result = await api.rebuildSettingIndex(scope.identity)
    if (!asyncScope.isCurrent(scope)) return
    if (result.failed) {
      message.warning(`已索引 ${result.indexed} 条，${result.failed} 条失败；可稍后重试`)
    } else {
      message.success(`已重建 ${result.indexed} 条设定索引`)
    }
  } catch (error) {
    if (asyncScope.isCurrent(scope)) message.error((error as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) rebuildingIndex.value = false
  }
}

async function loadNovelMeta() {
  if (!props.novelId) return
  const scope = asyncScope.capture()
  const sequence = ++metaLoadSequence
  const expectedAnchorRevision = anchorRevision
  const expectedSaveRevision = anchorSaveRevision
  try {
    const [novels, loadedChapters] = await Promise.all([api.listNovels(), api.listChapterMetadata(scope.identity)])
    if (!asyncScope.isCurrent(scope) || sequence !== metaLoadSequence) return
    const novel = novels.find((n) => n.id === scope.identity)
    if (anchorSaveRevision === expectedSaveRevision && !savingAnchor.value) {
      const untouched = anchorRevision === expectedAnchorRevision && styleAnchor.value === anchorBaseline.value
      anchorBaseline.value = novel?.style_anchor ?? ''
      if (untouched) styleAnchor.value = anchorBaseline.value
    }
    chapters.value = loadedChapters
  } catch (e) {
    if (asyncScope.isCurrent(scope) && sequence === metaLoadSequence) message.error((e as Error).message)
  }
}

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

async function loadAll(reportError = true): Promise<boolean> {
  if (!props.novelId) return false
  const scope = asyncScope.capture()
  const sequence = ++entityLoadSequence
  loading.value = true
  try {
    const result = emptyEntities()
    const lists = await Promise.all(ENTITY_TYPES.map((t) => api.listEntities(scope.identity, t.apiPath)))
    if (!asyncScope.isCurrent(scope) || sequence !== entityLoadSequence) return false
    ENTITY_TYPES.forEach((t, index) => { result[t.type] = lists[index] })
    allEntities.value = result
    if (pendingFocus) applyFocus(pendingFocus)
    return true
  } catch (e) {
    if (reportError && asyncScope.isCurrent(scope) && sequence === entityLoadSequence) message.error((e as Error).message)
    return false
  } finally {
    if (asyncScope.isCurrent(scope) && sequence === entityLoadSequence) loading.value = false
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
  formBaseline.value = JSON.stringify(next)
}

function startCreate() {
  if (!props.novelId || !confirmEntityChange()) return
  selectionRevision += 1
  pendingFocus = null
  editingId.value = null
  resetForm()
}

function changeType(value: string) {
  if (value === selectedType.value || !confirmEntityChange()) return
  selectionRevision += 1
  pendingFocus = null
  selectedType.value = value as EntityType
  editingId.value = null
  resetForm()
}

function startEdit(item: EntityRecord) {
  if (!allEntities.value[selectedType.value].some((entity) => entity.id === item.id) || !confirmEntityChange()) return
  populateForm(item)
}

function populateForm(item: EntityRecord) {
  selectionRevision += 1
  pendingFocus = null
  editingId.value = item.id
  const cfg = config.value
  const next: Record<string, any> = {}
  for (const f of cfg.fields) {
    const v = item[f.key]
    next[f.key] = f.type === 'json' ? (v == null ? '' : JSON.stringify(v, null, 2)) : (v ?? '')
  }
  form.value = next
  formBaseline.value = JSON.stringify(next)
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
      if (raw == null || String(raw).trim() === '') {
        // 清空输入表示清空已存数据；省略字段会让旧标签/关联继续保留。
        payload[f.key] = f.key === 'extras' ? {} : []
        continue
      }
      payload[f.key] = JSON.parse(raw)
    } else {
      payload[f.key] = raw
    }
  }
  return payload
}

async function save() {
  if (!props.novelId || saving.value) return
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
  const scope = asyncScope.capture()
  const entityType = selectedType.value
  const apiPath = config.value.apiPath
  const itemId = editingId.value
  const selection = selectionRevision
  const submittedForm = formFingerprint.value
  saving.value = true
  try {
    const saved = itemId
      ? await api.updateEntity(scope.identity, apiPath, itemId, payload)
      : await api.createEntity(scope.identity, apiPath, payload)
    if (!asyncScope.isCurrent(scope)) return
    if (selection === selectionRevision && selectedType.value === entityType && editingId.value === itemId) {
      editingId.value = saved.id
      formBaseline.value = submittedForm
    }
    message.success(itemId ? '已保存' : '已创建')
    await loadAll()
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) saving.value = false
  }
}

async function remove() {
  if (!props.novelId || !editingId.value || saving.value) return
  if (!window.confirm('确认删除该记录？')) return
  const scope = asyncScope.capture()
  const itemId = editingId.value
  const apiPath = config.value.apiPath
  const entityType = selectedType.value
  const selection = selectionRevision
  const submittedForm = formFingerprint.value
  saving.value = true
  try {
    await api.deleteEntity(scope.identity, apiPath, itemId)
    if (!asyncScope.isCurrent(scope)) return
    entityLoadSequence += 1
    allEntities.value[entityType] = allEntities.value[entityType].filter((item) => item.id !== itemId)
    if (pendingFocus?.type === entityType && pendingFocus.id === itemId) pendingFocus = null
    if (selectedType.value === entityType && editingId.value === itemId) {
      editingId.value = null
      if (selection === selectionRevision && formFingerprint.value === submittedForm) resetForm()
      else formBaseline.value = '{}'
    }
    const refreshed = await loadAll(false)
    if (asyncScope.isCurrent(scope)) {
      if (refreshed) message.success('已删除')
      else message.warning('记录已删除，但列表刷新失败；已移除本页记录，请稍后刷新。')
    }
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scope)) saving.value = false
  }
}

function applyFocus(target: { type: EntityType; id: string }) {
  const item = allEntities.value[target.type]?.find((e) => e.id === target.id)
  if (!item) { pendingFocus = target; return }
  pendingFocus = null
  if (!confirmEntityChange()) return
  selectedType.value = target.type
  populateForm(item)
}

watch(
  () => props.novelId,
  () => {
    asyncScope.invalidate()
    selectionRevision += 1
    pendingFocus = null
    editingId.value = null
    allEntities.value = emptyEntities()
    loading.value = false
    saving.value = false
    savingAnchor.value = false
    rebuildingIndex.value = false
    styleAnchor.value = ''
    anchorBaseline.value = ''
    chapters.value = []
    resetForm()
    anchorChapterId.value = null
    anchorLoadSequence += 1
    loadingAnchor.value = false
    if (props.novelId) {
      loadAll()
      loadNovelMeta()
    }
  },
  { immediate: true, flush: 'sync' },
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

onMounted(() => window.addEventListener('beforeunload', guardUnload))
onBeforeUnmount(() => {
  anchorLoadSequence += 1
  loadingAnchor.value = false
  asyncScope.dispose()
  window.removeEventListener('beforeunload', guardUnload)
  emit('dirty-change', false)
})
</script>

<template>
  <div class="settings-panel">
    <div class="style-anchor">
      <div class="anchor-header">
        <span>文风基准</span>
        <n-button size="small" type="primary" :disabled="!novelId" :loading="savingAnchor" @click="saveAnchor">保存基准</n-button>
      </div>
      <div class="anchor-row">
        <n-select
          :value="anchorChapterId"
          :options="chapterOptions"
          :loading="loadingAnchor"
          placeholder="从章节取基准（取该章正文前 800 字）"
          style="width: 320px"
          clearable
          @update:value="pickAnchorChapter"
        />
        <span v-if="!anchorSet" class="anchor-hint">
          未设置文风基准：点击“定稿沉淀”时不计算文风漂移分，质量曲线无漂移线。
        </span>
        <span v-else class="anchor-hint ok">
          {{ styleAnchor === anchorBaseline ? '已保存基准' : '基准尚未保存' }}（{{ styleAnchor.length }} 字）：保存后，点击“定稿沉淀”时比对并记录漂移分。
        </span>
      </div>
      <n-input
        v-model:value="styleAnchor"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 4 }"
        placeholder="粘贴一段代表本书文风的文字（如开篇前 800 字）"
      />
    </div>

    <div class="index-tools">
      <span>旧作品设定可显式重建语义检索索引，不修改 Story Bible。</span>
      <n-button size="small" :loading="rebuildingIndex" @click="rebuildIndex">重建设定索引</n-button>
    </div>

    <div class="body">
      <div class="type-sidebar">
        <n-menu
          :options="typeMenuOptions"
          :value="selectedType"
          @update:value="changeType"
        />
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
              :placeholder="f.placeholder || `请输入${f.label}`"
            />
            <n-input
              v-else-if="f.type === 'textarea'"
              v-model:value="form[f.key]"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 6 }"
              :placeholder="f.placeholder || `请输入${f.label}`"
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
          <n-button type="primary" :disabled="!novelId" :loading="saving" @click="save">保存</n-button>
          <n-button v-if="editingId" type="error" :disabled="saving" @click="remove">删除</n-button>
        </div>
      </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.style-anchor {
  border-bottom: 1px solid #e5e5e5;
  padding: 12px 16px;
}
.anchor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-weight: 600;
}
.anchor-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.anchor-hint {
  color: #d07830;
  font-size: 12px;
}
.anchor-hint.ok {
  color: #18a058;
}
.index-tools {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid #e5e5e5;
  color: #666;
  font-size: 12px;
}
.body {
  flex: 1;
  display: flex;
  min-height: 0;
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
