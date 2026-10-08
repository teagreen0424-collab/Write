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
  NModal,
  NSelect,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { chapterLabel } from '../utils/chapter'
import { createAsyncScope } from '../utils/asyncScope'
import { anchorDirty, anchorOperation, isAnchorNovel, styleAnchorDraft, styleAnchorDrafts, type StyleAnchorDraft } from '../utils/styleAnchorDraft'
import { advanceEntityFacts, bindEntityId, cloneEntityForm, entityCreation, entityDraftDirty, entityDrafts,
  entityFactVersion, entityForm as recordForm, entityFormDirty, entityOperation, entityReadVersions, entityRecord,
  fieldValue, isEntityRead, matchesEntityPayload, refreshEntityDrafts, sameFieldValue, type EntityDraft } from '../utils/entityDraft'
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
const activeEntity = ref<EntityDraft | null>(null)
const entityState = computed(() => activeEntity.value ?? entityCreation(props.novelId, selectedType.value))
const editingId = computed(() => entityState.value.exists ? entityState.value.recordId : null)
const loading = ref(false)
const rebuildingIndex = ref(false)
const saving = computed(() => entityState.value.pending !== null)
const anchorState = computed(() => styleAnchorDraft(props.novelId))
const savingAnchor = computed(() => anchorState.value.pending !== null)
const asyncScope = createAsyncScope(() => props.novelId)
let entityLoadSequence = 0
let metaLoadSequence = 0
let selectionRevision = 0
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
const form = computed({ get: () => entityState.value.draft, set: (value: Record<string, any>) => { entityState.value.draft = value } })
const formBaseline = computed({ get: () => JSON.stringify(entityState.value.baseline), set: (value: string) => { entityState.value.baseline = JSON.parse(value) } })
const formFingerprint = computed(() => JSON.stringify(form.value))
const entityDirty = computed(() => entityFormDirty(entityState.value))

// 文风基准：决定定稿时是否计算文风漂移分
const styleAnchor = computed({ get: () => anchorState.value.draft, set: (value: string) => { anchorState.value.draft = value } })
const anchorBaseline = computed(() => anchorState.value.baseline)
const anchorChapterId = ref<string | null>(null)
const chapters = ref<ChapterMetadata[]>([])
const loadingAnchor = ref(false)
const anchorSet = computed(() => styleAnchor.value.trim().length > 0)
const chapterOptions = computed(() =>
  chapters.value.map((c) => ({ label: chapterLabel(c), value: c.id })),
)
const dirty = computed(() => entityDraftDirty(entityState.value) || anchorDirty(anchorState.value))
watch(dirty, (value) => emit('dirty-change', value), { immediate: true, flush: 'sync' })
const showAnchorCopy = ref(false)
const anchorCopy = ref('')
const anchorCopyOriginal = ref(false)
const otherAnchorDrafts = computed(() => styleAnchorDrafts().filter(state => state !== anchorState.value))
const otherEntityDrafts = computed(() => entityDrafts().filter(state => state !== entityState.value))
const showEntityCopy = ref(false)
const entityCopy = ref('')
const entityCopyOriginal = ref(false)
const entityText = (value: Record<string, any>) => JSON.stringify(value, null, 2)

function confirmEntityChange(): boolean {
  return !entityDraftDirty(entityState.value) || window.confirm('当前设定有未保存修改或原提交待回应。离开编辑器后，原提交与页内稿仍可复制；不会自动保存。继续切换？')
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
  const state = anchorState.value
  const revision = state.revision, saveRevision = state.version
  const isCurrent = () => asyncScope.isCurrent(scope) && sequence === anchorLoadSequence
    && anchorChapterId.value === id && state === anchorState.value
    && revision === state.revision && saveRevision === state.version
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
  const state = anchorState.value
  if (!props.novelId || state.pending !== null || state.checking !== null || state.phase === 'unknown' || !state.exists) return
  const scope = asyncScope.capture()
  const submitted = state.draft
  const operation = anchorOperation()
  state.version += 1
  state.original = submitted
  state.saved = null
  state.observed = null
  state.error = ''
  state.phase = 'pending'
  state.pending = operation
  anchorLoadSequence += 1
  loadingAnchor.value = false
  const current = () => asyncScope.isCurrent(scope) && anchorState.value === state
  try {
    const saved = await api.updateNovel(scope.identity, { style_anchor: submitted })
    if (state.pending !== operation) return
    if (!isAnchorNovel(saved) || saved.id !== scope.identity || saved.style_anchor !== submitted) {
      state.phase = 'unknown'
      state.error = '未收到与原提交一致的完整保存确认，请先复制并核对当前文风。'
      if (current()) message.warning(state.error)
      return
    }
    state.saved = { ...saved }
    state.title = saved.title
    state.initialized = true
    state.baseline = submitted
    state.phase = 'saved'
    if (current()) message.success(state.draft === submitted ? '已保存文风基准' : '原提交文风已保存，当前修改仍未保存')
  } catch (e) {
    if (state.pending !== operation) return
    state.phase = 'unknown'
    state.error = `文风保存结果待核对：${(e as Error).message}`
    if (current()) message.error(state.error)
  } finally {
    if (state.pending === operation) {
      state.pending = null
      state.version += 1
    }
  }
}

async function checkAnchor() {
  const state = anchorState.value
  if (!props.novelId || state.pending !== null || state.checking !== null) return
  const scope = asyncScope.capture()
  const operation = anchorOperation()
  const version = ++state.version
  state.checking = operation
  const current = () => asyncScope.isCurrent(scope) && anchorState.value === state
  try {
    const novels = await api.listNovels()
    if (state.checking !== operation || state.version !== version) return
    const novel = novels.find(item => item.id === state.novelId)
    state.exists = !!novel
    if (!novel) {
      state.observed = null
      state.error = '当前目录已无此作品；保留稿件供复制，不再向此作品保存。'
    } else if (!isAnchorNovel(novel)) {
      state.observed = null
      state.error = '当前作品资料不完整，尚不能继续编辑；原提交与当前稿已保留。'
    } else {
      state.observed = { ...novel }
      state.title = novel.title
      state.error = '这是当前文风资料，不能证明原请求结果；核对后可明确采用它继续编辑。'
    }
    if (current()) message.warning(state.error)
  } catch (e) {
    if (state.checking !== operation || state.version !== version) return
    state.error = `当前文风读取失败：${(e as Error).message}`
    if (current()) message.error(state.error)
  } finally {
    if (state.checking === operation) {
      state.checking = null
      state.version += 1
    }
  }
}

function adoptCurrentAnchor() {
  const state = anchorState.value, observed = state.observed
  if (state.pending !== null || state.checking !== null || !state.exists || !observed) return
  const revision = state.revision, version = state.version
  if (!window.confirm('当前资料不能证明原请求结果；旧请求仍可能迟到提交。采用当前文风作为后续编辑基线？当前输入会保留，之后仍需明确点击保存。')) return
  if (state !== anchorState.value || state.revision !== revision || state.version !== version || state.observed !== observed) return
  state.version += 1
  state.initialized = true
  state.baseline = observed.style_anchor
  state.saved = null
  state.phase = 'idle'
  state.error = '已采用当前文风资料，原请求结果仍未知；当前输入须由你明确保存。'
}

async function copyAnchor(original = false, state: StyleAnchorDraft = anchorState.value) {
  const hostState = anchorState.value
  const text = original ? state.original : state.draft
  if (text === null) return
  const scope = asyncScope.capture()
  const current = () => asyncScope.isCurrent(scope) && anchorState.value === hostState
  try {
    await navigator.clipboard.writeText(text)
    if (current()) message.success(original ? '已复制原提交文风' : '已复制当前文风')
  } catch {
    if (!current()) return
    anchorCopy.value = text
    anchorCopyOriginal.value = original
    showAnchorCopy.value = true
    message.warning('复制不可用，请从只读文风框手动复制。')
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
  const state = anchorState.value
  const expectedAnchorRevision = state.revision
  const expectedSaveRevision = state.version
  try {
    const [novels, loadedChapters] = await Promise.all([api.listNovels(), api.listChapterMetadata(scope.identity)])
    if (!asyncScope.isCurrent(scope) || sequence !== metaLoadSequence) return
    const novel = novels.find((n) => n.id === scope.identity)
    if (state.version === expectedSaveRevision && state.pending === null && state.checking === null && state.phase !== 'unknown') {
      state.exists = !!novel
      if (novel && typeof novel.style_anchor === 'string') {
        const untouched = state.revision === expectedAnchorRevision && state.draft === state.baseline
        if (typeof novel.title === 'string') state.title = novel.title
        state.baseline = novel.style_anchor
        state.initialized = true
        if (untouched) state.draft = state.baseline
      }
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
  const facts = entityFactVersion(scope.identity)
  const versions = entityReadVersions(scope.identity)
  loading.value = true
  try {
    const result = emptyEntities()
    const lists = await Promise.all(ENTITY_TYPES.map((t) => api.listEntities(scope.identity, t.apiPath)))
    if (!asyncScope.isCurrent(scope) || sequence !== entityLoadSequence || facts !== entityFactVersion(scope.identity)) return false
    ENTITY_TYPES.forEach((t, index) => { result[t.type] = lists[index] })
    allEntities.value = result
    refreshEntityDrafts(scope.identity, result, versions)
    if (pendingFocus) applyFocus(pendingFocus)
    return true
  } catch (e) {
    if (reportError && asyncScope.isCurrent(scope) && sequence === entityLoadSequence) message.error((e as Error).message)
    return false
  } finally {
    if (asyncScope.isCurrent(scope) && sequence === entityLoadSequence) loading.value = false
  }
}

function resetForm(fresh = false) {
  activeEntity.value = entityCreation(props.novelId, selectedType.value, fresh)
}

function startCreate() {
  if (!props.novelId || !confirmEntityChange()) return
  const state = entityState.value
  if (state.recordId === null && (state.pending !== null || state.phase === 'unknown')
    && !window.confirm('原创建请求可能已经保存或仍会迟到提交；同名列表不能确认其结果。请先人工核对。确定以独立新意图另建记录？原提交和后来稿会保留供复制，不会自动重发。')) return
  selectionRevision += 1
  pendingFocus = null
  resetForm(true)
}

function changeType(value: string) {
  if (value === selectedType.value || !confirmEntityChange()) return
  selectionRevision += 1
  pendingFocus = null
  selectedType.value = value as EntityType
  resetForm()
}

function startEdit(item: EntityRecord) {
  if (!allEntities.value[selectedType.value].some((entity) => entity.id === item.id) || !confirmEntityChange()) return
  populateForm(item)
}

function populateForm(item: EntityRecord) {
  selectionRevision += 1
  pendingFocus = null
  activeEntity.value = entityRecord(props.novelId, selectedType.value, item)
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
      const value = JSON.parse(raw)
      if (f.key === 'extras' ? value === null || typeof value !== 'object' || Array.isArray(value) : !Array.isArray(value)) {
        return `${f.label} 必须是${f.key === 'extras' ? 'JSON 对象' : 'JSON 数组'}；清空输入可清除该字段`
      }
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
    payload[f.key] = fieldValue(f, form.value[f.key])
  }
  return payload
}

async function save() {
  const state = entityState.value
  if (!props.novelId || state.pending !== null || state.checking !== null || state.phase === 'unknown' || !state.exists) return
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
  const entityType = state.type
  const apiPath = getEntityConfig(entityType).apiPath
  const itemId = state.recordId
  const selection = selectionRevision
  const submittedForm = formFingerprint.value
  const revision = state.revision
  const submitted = JSON.parse(submittedForm)
  const baseline = JSON.parse(formBaseline.value)
  if (itemId) {
    payload = Object.fromEntries(config.value.fields
      .filter((field) => !sameFieldValue(field, submitted[field.key], baseline[field.key]))
      .map((field) => [field.key, payload[field.key]]))
    if (!Object.keys(payload).length) {
      message.info('当前设定没有新的修改，未提交保存')
      return
    }
  }
  const currentEditor = () => asyncScope.isCurrent(scope) && selection === selectionRevision
    && entityState.value === state
  const operation = entityOperation()
  state.version += 1
  advanceEntityFacts(scope.identity)
  state.original = cloneEntityForm(submitted)
  state.payload = structuredClone(payload)
  state.saved = null
  state.observed = null
  state.error = ''
  state.phase = 'pending'
  state.pending = operation
  try {
    const saved = itemId
      ? await api.updateEntity(scope.identity, apiPath, itemId, payload)
      : await api.createEntity(scope.identity, apiPath, payload)
    if (state.pending !== operation) return
    if (!isEntityRead(saved, scope.identity, entityType, itemId) || !matchesEntityPayload(saved, payload)) {
      state.phase = 'unknown'
      state.error = '未收到与原身份、原提交一致的完整保存确认；原表单与当前稿已保留，请先核对。'
      if (currentEditor()) message.warning(state.error)
      return
    }
    state.saved = structuredClone(saved)
    state.phase = 'saved'
    bindEntityId(state, saved.id)
    const untouched = state.revision === revision && JSON.stringify(state.draft) === submittedForm
    if (untouched) {
      const next = recordForm(entityType, saved)
      state.draft = cloneEntityForm(next)
      state.baseline = next
    } else {
      const next = { ...baseline }
      for (const key of Object.keys(payload)) next[key] = submitted[key]
      state.baseline = next
    }
    if (!currentEditor()) return
    // 先使保存前的读取失效，并保留真实保存回应；刷新失败不回退已确认事实。
    entityLoadSequence += 1
    loading.value = false
    const existing = allEntities.value[entityType].find((item) => item.id === saved.id)
    const confirmed = { ...existing, ...saved }
    allEntities.value[entityType] = [...allEntities.value[entityType].filter((item) => item.id !== saved.id), confirmed]
    message.success(untouched ? (itemId ? '已保存' : '已创建') : '原提交已保存，当前修改仍须另行保存')
    const refreshed = await loadAll(false)
    if (!refreshed && asyncScope.isCurrent(scope) && selection === selectionRevision && editingId.value === saved.id) {
      message.warning('设定已保存，但列表刷新失败；已确认的记录保留，请稍后刷新核对。')
    }
  } catch (e) {
    if (state.pending !== operation) return
    const error = e as Error & { status?: number }
    const rejected = error.status !== undefined && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)
    state.phase = rejected ? 'idle' : 'unknown'
    if (itemId && error.status === 404) state.exists = false
    state.error = rejected ? error.message : `原设定保存结果待核对：${error.message}。原提交与当前稿已保留，不会自动重发。`
    if (currentEditor()) message.error(state.error)
  } finally {
    if (state.pending === operation) {
      state.pending = null
      state.version += 1
      advanceEntityFacts(scope.identity)
    }
  }
}

async function checkEntity() {
  const state = entityState.value
  if (!props.novelId || state.pending !== null || state.checking !== null || !state.exists) return
  const scope = asyncScope.capture(), selection = selectionRevision
  const current = () => asyncScope.isCurrent(scope) && entityState.value === state && selection === selectionRevision
  const operation = entityOperation(), version = ++state.version
  state.checking = operation
  advanceEntityFacts(scope.identity)
  try {
    const [rows, novels] = await Promise.all([api.listEntities(state.novelId, getEntityConfig(state.type).apiPath),
      state.recordId ? Promise.resolve(null) : api.listNovels()])
    if (state.checking !== operation || state.version !== version) return
    state.observed = null
    if (!state.recordId) {
      if (!novels?.some(novel => novel.id === state.novelId)) state.exists = false
      state.error = state.exists
        ? '已读取当前列表。列表或同名记录不能证明原创建请求结果；请人工核对。另建记录须明确开启独立新建意图。'
        : '当前目录已无原作品；原创建表单与当前稿仅供复制。'
    } else {
      const row = rows.find(item => item.id === state.recordId)
      if (!row) { state.exists = false; state.error = '当前列表已无原记录；原提交与当前稿仅供复制，不再保存或复活。' }
      else if (!isEntityRead(row, state.novelId, state.type, state.recordId)) state.error = '当前资料不完整，不能采用；原提交与当前稿已保留。'
      else { state.observed = structuredClone(row); state.error = '仅供核对的当前资料，不能证明原请求结果；采用后也不会自动保存。' }
    }
    if (current()) message.warning(state.error)
  } catch (e) {
    if (state.checking !== operation || state.version !== version) return
    state.error = `当前设定读取失败：${(e as Error).message}`
    if (current()) message.error(state.error)
  } finally {
    if (state.checking === operation) { state.checking = null; state.version += 1; advanceEntityFacts(scope.identity) }
  }
}

function adoptCurrentEntity() {
  const state = entityState.value, observed = state.observed
  if (!state.recordId || !state.exists || state.pending !== null || state.checking !== null || !observed) return
  const revision = state.revision, version = state.version
  if (!window.confirm('当前资料不能证明原请求结果；旧请求仍可能迟到提交。以当前设定资料作为后续编辑基线？原提交和当前输入会保留，之后仍需明确点击保存。')) return
  if (state !== entityState.value || state.revision !== revision || state.version !== version || state.observed !== observed) return
  const baseline = state.baseline, next = recordForm(state.type, observed)
  for (const field of getEntityConfig(state.type).fields) {
    // 未发送且作者未改的旧显示值不能因采用新事实而成为下一次的作者意图。
    if (!Object.prototype.hasOwnProperty.call(state.payload ?? {}, field.key)
      && sameFieldValue(field, state.draft[field.key], baseline[field.key])) next[field.key] = baseline[field.key]
  }
  state.baseline = next
  state.phase = 'idle'
  state.saved = null
  state.version += 1
  advanceEntityFacts(state.novelId)
  state.error = '已采用当前资料，原请求结果仍未知；当前稿须由你明确保存。'
}

async function copyEntity(original = false, state: EntityDraft = entityState.value) {
  const value = original ? state.original : state.draft
  if (value === null) return
  const text = entityText(value), host = entityState.value, scope = asyncScope.capture()
  const current = () => asyncScope.isCurrent(scope) && entityState.value === host
  try { await navigator.clipboard.writeText(text); if (current()) message.success(original ? '已复制原提交设定' : '已复制当前设定') }
  catch {
    if (!current()) return
    entityCopy.value = text; entityCopyOriginal.value = original; showEntityCopy.value = true
    message.warning('剪贴板不可用，请从只读表单手动复制；JSON 字符串保留字段原文。')
  }
}

async function remove() {
  const state = entityState.value
  if (!props.novelId || !state.recordId || state.pending !== null || state.checking !== null || state.phase === 'unknown' || !state.exists) return
  if (!window.confirm('确认删除该记录？')) return
  const scope = asyncScope.capture()
  const itemId = state.recordId
  const apiPath = config.value.apiPath
  const entityType = selectedType.value
  const selection = selectionRevision
  const submittedForm = formFingerprint.value
  const operation = entityOperation()
  state.pending = operation
  state.version += 1
  advanceEntityFacts(scope.identity)
  try {
    await api.deleteEntity(scope.identity, apiPath, itemId)
    if (state.pending !== operation) return
    state.exists = false
    state.observed = null
    state.error = '原记录已删除，页内稿仅供复制。'
    if (!asyncScope.isCurrent(scope)) return
    entityLoadSequence += 1
    allEntities.value[entityType] = allEntities.value[entityType].filter((item) => item.id !== itemId)
    if (pendingFocus?.type === entityType && pendingFocus.id === itemId) pendingFocus = null
    if (selectedType.value === entityType && entityState.value === state) {
      if (selection === selectionRevision && formFingerprint.value === submittedForm) resetForm(true)
    }
    const refreshed = await loadAll(false)
    if (asyncScope.isCurrent(scope)) {
      if (refreshed) message.success('已删除')
      else message.warning('记录已删除，但列表刷新失败；已移除本页记录，请稍后刷新。')
    }
  } catch (e) {
    if (asyncScope.isCurrent(scope)) message.error((e as Error).message)
  } finally {
    if (state.pending === operation) { state.pending = null; state.version += 1; advanceEntityFacts(scope.identity) }
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
    allEntities.value = emptyEntities()
    loading.value = false
    rebuildingIndex.value = false
    showAnchorCopy.value = false
    showEntityCopy.value = false
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
        <n-button size="small" type="primary" :disabled="!novelId || savingAnchor || anchorState.checking !== null || anchorState.phase === 'unknown' || !anchorState.exists" :loading="savingAnchor" @click="saveAnchor">保存基准</n-button>
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
        <span v-if="savingAnchor" class="anchor-hint">原提交文风正在保存；当前新输入仍保留。</span>
        <span v-else-if="anchorState.phase === 'unknown'" class="anchor-hint">文风保存结果待核对；请先复制并读取当前文风。</span>
        <span v-else-if="!anchorState.exists" class="anchor-hint">当前目录已无此作品，稿件仍可复制。</span>
        <span v-else-if="anchorState.phase === 'idle' && anchorState.original !== null" class="anchor-hint">已采用当前资料，原请求结果仍未知；当前输入须明确保存。</span>
        <span v-else-if="!anchorSet" class="anchor-hint">
          未设置文风基准：点击“定稿沉淀”时不计算文风漂移分，质量曲线无漂移线。
        </span>
        <span v-else class="anchor-hint ok">
          {{ styleAnchor === anchorBaseline ? '已保存基准' : '基准尚未保存' }}（{{ styleAnchor.length }} 字）：保存后，点击“定稿沉淀”时比对并记录漂移分。
        </span>
      </div>
      <n-input
        v-model:value="styleAnchor"
        type="textarea"
        :input-props="{ 'aria-label': '当前文风基准' }"
        :autosize="{ minRows: 2, maxRows: 4 }"
        placeholder="粘贴一段代表本书文风的文字（如开篇前 800 字）"
      />
      <div class="anchor-tools">
        <n-button size="small" @click="copyAnchor()">复制当前文风</n-button>
        <n-button v-if="anchorState.original !== null" size="small" @click="copyAnchor(true)">复制原提交</n-button>
        <n-button v-if="anchorState.original !== null" size="small" :disabled="savingAnchor || anchorState.checking !== null" :loading="anchorState.checking !== null" @click="checkAnchor">核对当前文风</n-button>
        <n-button v-if="anchorState.phase === 'unknown' && anchorState.observed && anchorState.exists" size="small" :disabled="savingAnchor || anchorState.checking !== null" @click="adoptCurrentAnchor">以当前文风继续编辑</n-button>
      </div>
      <template v-if="anchorState.original !== null">
        <div class="anchor-hint">{{ anchorState.saved ? '原提交已收到完整保存确认；当前修改仍须另行保存。' : '原提交文风独立保留，读取当前资料不能证明原请求结果。' }}</div>
        <n-input type="textarea" readonly :value="anchorState.original" :input-props="{ 'aria-label': '原提交文风基准' }" :autosize="{ minRows: 1, maxRows: 2 }" />
      </template>
      <div v-if="anchorState.observed" class="anchor-hint">当前作品文风（仅供人工核对）：</div>
      <n-input v-if="anchorState.observed" type="textarea" readonly :value="anchorState.observed.style_anchor" :input-props="{ 'aria-label': '核对所得当前文风' }" :autosize="{ minRows: 1, maxRows: 2 }" />
      <div v-if="anchorState.error" class="anchor-hint">{{ anchorState.error }}</div>
      <div class="anchor-hint">文风原提交、后来稿和等待状态仅保留在本页面；完整刷新或关闭前请复制。其他设定表单仍按离页提示处理。</div>
      <details v-if="otherAnchorDrafts.length" class="anchor-recovery">
        <summary>其他作品页内文风稿（{{ otherAnchorDrafts.length }}）</summary>
        <div v-for="state in otherAnchorDrafts" :key="state.novelId" class="anchor-record">
          <strong>{{ state.title }}</strong>
          <span class="anchor-hint">{{ state.pending !== null ? '原提交等待中' : state.phase === 'unknown' ? '原保存结果未知' : state.saved ? '原提交已确认' : '页内稿件' }}；此处只供复制，请在原作品核对后编辑。</span>
          <n-input type="textarea" readonly :value="state.draft" :input-props="{ 'aria-label': `其他作品当前文风 · ${state.novelId}` }" :autosize="{ minRows: 1, maxRows: 2 }" />
          <n-input v-if="state.original !== null" type="textarea" readonly :value="state.original" :input-props="{ 'aria-label': `其他作品原提交文风 · ${state.novelId}` }" :autosize="{ minRows: 1, maxRows: 2 }" />
          <div class="anchor-tools">
            <n-button size="small" @click="copyAnchor(false, state)">复制此稿</n-button>
            <n-button v-if="state.original !== null" size="small" @click="copyAnchor(true, state)">复制此原提交</n-button>
          </div>
        </div>
      </details>
    </div>
    <n-modal :show="showAnchorCopy" preset="card" title="手动复制文风" style="width: 560px" @update:show="(value: boolean) => { showAnchorCopy = value }">
      <p>剪贴板不可用，请手动选取并复制以下只读文本。</p>
      <n-input type="textarea" readonly :value="anchorCopy" :input-props="{ 'aria-label': anchorCopyOriginal ? '手动复制原提交文风' : '手动复制当前文风' }" :autosize="{ minRows: 3, maxRows: 12 }" />
    </n-modal>

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
        <h3>{{ !entityState.exists ? '原记录副本（仅供复制）' : editingId ? '编辑' : '新建' }} {{ config.label }}</h3>
        <p v-if="saving" class="anchor-hint">原提交待回应；后来输入保留，同一目标不会重复保存。</p>
        <p v-else-if="entityState.phase === 'unknown'" class="anchor-hint">原保存结果待核对；请复制原提交与当前稿，不会盲重发。</p>
        <p v-if="entityState.error" class="anchor-hint">{{ entityState.error }}</p>
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
              :input-props="{ 'aria-label': `当前设定表单 · ${f.key}` }"
              :placeholder="f.placeholder || `请输入${f.label}`"
            />
            <n-input
              v-else-if="f.type === 'textarea'"
              v-model:value="form[f.key]"
              type="textarea"
              :input-props="{ 'aria-label': `当前设定表单 · ${f.key}` }"
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
              :input-props="{ 'aria-label': `当前设定表单 · ${f.key}` }"
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
          <n-button type="primary" :disabled="!novelId || saving || entityState.checking !== null || entityState.phase === 'unknown' || !entityState.exists || (!!editingId && !entityDirty)" :loading="saving" @click="save">保存</n-button>
          <n-button v-if="editingId" type="error" :disabled="saving || entityState.checking !== null || entityState.phase === 'unknown'" @click="remove">删除</n-button>
          <n-button @click="copyEntity()">复制当前设定</n-button>
          <n-button v-if="entityState.original" @click="copyEntity(true)">复制原提交设定</n-button>
          <n-button v-if="entityState.original && entityState.exists" :disabled="saving || entityState.checking !== null" :loading="entityState.checking !== null" @click="checkEntity">核对当前设定</n-button>
          <n-button v-if="entityState.phase === 'unknown' && entityState.observed && entityState.exists" :disabled="saving || entityState.checking !== null" @click="adoptCurrentEntity">以当前设定继续编辑</n-button>
        </div>
        <template v-if="entityState.original">
          <p class="anchor-hint">{{ entityState.saved ? '原提交已收到完整保存确认；当前稿仍须另行保存。' : '原提交表单保留，当前读取不能证明原请求结果。' }}</p>
          <n-input type="textarea" readonly :value="entityText(entityState.original)" :input-props="{ 'aria-label': '原提交设定表单' }" :autosize="{ minRows: 2, maxRows: 5 }" />
        </template>
        <n-input v-if="entityState.observed" type="textarea" readonly :value="entityText(recordForm(entityState.type, entityState.observed))" :input-props="{ 'aria-label': '核对所得当前设定' }" :autosize="{ minRows: 2, maxRows: 5 }" />
        <p class="anchor-hint">设定原提交、后来稿与等待状态仅在本页面保留；切换编辑器不自动保存，完整刷新或关闭前请复制。新建按钮开启独立创建意图。</p>
        <details v-if="otherEntityDrafts.length" class="anchor-recovery">
          <summary>其他页内设定稿（{{ otherEntityDrafts.length }}）</summary>
          <div v-for="state in otherEntityDrafts" :key="state.key" class="anchor-record">
            <strong>{{ state.novelId }} · {{ getEntityConfig(state.type).label }} · {{ state.recordId || '独立创建稿' }}</strong>
            <p class="anchor-hint">{{ !state.exists ? '来源已无，仅供复制' : state.pending !== null ? '原提交等待中' : state.phase === 'unknown' ? '原结果未知，请在原来源核对' : state.saved ? '原提交已确认' : '未保存页内稿' }}；此处只读，不会写入或改绑当前记录。</p>
            <n-input type="textarea" readonly :value="entityText(state.draft)" :input-props="{ 'aria-label': `其他源当前设定 · ${state.key}` }" :autosize="{ minRows: 2, maxRows: 4 }" />
            <n-input v-if="state.original" type="textarea" readonly :value="entityText(state.original)" :input-props="{ 'aria-label': `其他源原提交设定 · ${state.key}` }" :autosize="{ minRows: 2, maxRows: 4 }" />
            <n-button size="small" @click="copyEntity(false, state)">复制此设定稿</n-button>
            <n-button v-if="state.original" size="small" @click="copyEntity(true, state)">复制此设定原提交</n-button>
          </div>
        </details>
      </div>
      </div>
    </div>
    <n-modal :show="showEntityCopy" preset="card" title="手动复制设定" style="width: 560px" @update:show="(value: boolean) => { showEntityCopy = value }">
      <p>请手动选取并复制只读 JSON 表单；其中的字符串保留每个字段的原始文本。</p>
      <n-input type="textarea" readonly :value="entityCopy" :input-props="{ 'aria-label': entityCopyOriginal ? '手动复制原提交设定' : '手动复制当前设定' }" :autosize="{ minRows: 3, maxRows: 14 }" />
    </n-modal>
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
.anchor-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}
.anchor-recovery {
  margin-top: 8px;
  max-height: 240px;
  overflow: auto;
}
.anchor-record {
  margin-top: 8px;
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
