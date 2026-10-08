<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  NTabPane,
  NTabs,
  useDialog,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import type { EntityType, Novel } from '../types'
import { createAsyncScope } from '../utils/asyncScope'
import {
  creationDraft, freshCreationDraft, isSavedProject, projectChanges,
  projectDirty, projectFields, projectOperation, projectText,
} from '../utils/projectDraft'
import type { ProjectFields } from '../utils/projectDraft'

const SettingsPanel = defineAsyncComponent(() => import('./SettingsPanel.vue'))
const KnowledgeGraph = defineAsyncComponent(() => import('./KnowledgeGraph.vue'))
const WritingPanel = defineAsyncComponent(() => import('./WritingPanel.vue'))
const OutlinePanel = defineAsyncComponent(() => import('./OutlinePanel.vue'))
const AutopilotPanel = defineAsyncComponent(() => import('./AutopilotPanel.vue'))
const ForeshadowingTracking = defineAsyncComponent(() => import('./ForeshadowingTracking.vue'))
const BookDeliveryPanel = defineAsyncComponent(() => import('./BookDeliveryPanel.vue'))

const props = defineProps<{
  initialNovelId?: string | null
  initialTab?: 'settings' | 'outline' | 'writing'
  initialChapterId?: string | null
}>()
const emit = defineEmits<{ (e: 'back'): void }>()

const message = useMessage()
const dialog = useDialog()

const novels = ref<Novel[]>([])
const novelId = ref<string | null>(null)
const activeTab = ref<'settings' | 'writing' | 'outline' | 'autopilot' | 'foreshadowing' | 'graph' | 'delivery'>(
  props.initialTab ?? 'settings',
)
const focusTarget = ref<{ type: EntityType; id: string } | null>(null)
const pendingGraphFocus = ref<{ type: EntityType; id: string } | null>(null)
const settingsVisited = ref(activeTab.value === 'settings')
const settingsMounted = ref(false)
watch(activeTab, (tab) => { if (tab === 'settings') settingsVisited.value = true }, { flush: 'sync' })
const refreshKey = ref(0)
const writingDirty = ref(false)
const autopilotDirty = ref(false)
const settingsDirty = ref(false)
const outlineDirty = ref(false)
const showNavigationConfirm = ref(false)
const navigationTarget = ref('')
const navigationWarning = ref('')
const pendingNavigation = ref<(() => void) | null>(null)

const showCreate = ref(false)
const createState = ref(creationDraft())
const createForm = computed({ get: () => createState.value.form, set: (value: ProjectFields) => { createState.value.form = projectFields(value) } })
const creating = computed(() => createState.value.pending !== null)
const projectUnsaved = computed(() => projectDirty(createState.value))
const hasProjectRecord = computed(() => !!createState.value.original || projectUnsaved.value)
const copyFallback = ref('')
const copyFallbackOriginal = ref(false)
const asyncScope = createAsyncScope(() => novelId.value ?? '')
let novelLoadSequence = 0
let createEpoch = 0
let alive = true
watch(novelId, () => { asyncScope.invalidate(); createEpoch += 1 }, { flush: 'sync' })
watch(showCreate, () => { createEpoch += 1 }, { flush: 'sync' })

const novelOptions = computed(() => novels.value.map((n) => ({ label: n.title, value: n.id })))

async function loadNovels(errorPrefix = '', canReport: () => boolean = () => alive, selectInitial = true) {
  const scope = asyncScope.capture()
  const sequence = ++novelLoadSequence
  try {
    const result = await api.listNovels()
    if (!asyncScope.isCurrent(scope) || sequence !== novelLoadSequence) return false
    novels.value = result
    if (selectInitial && !novelId.value && props.initialNovelId && novels.value.some((n) => n.id === props.initialNovelId)) {
      novelId.value = props.initialNovelId
    } else if (selectInitial && !novelId.value && novels.value.length) {
      novelId.value = novels.value[0].id
    }
    return true
  } catch (e) {
    if (asyncScope.isCurrent(scope) && sequence === novelLoadSequence && canReport()) message.error(`${errorPrefix}${(e as Error).message}`)
    return false
  }
}

function openCreate() {
  createEpoch += 1
  createState.value = creationDraft()
  showCreate.value = true
}

async function submitCreate() {
  await saveCreation(false)
}

async function saveProjectChanges() { await saveCreation(true) }

async function saveCreation(changesOnly: boolean) {
  const state = createState.value
  if (!alive || !showCreate.value || state.pending !== null || state.checking !== null || state.phase === 'unknown') return
  if (changesOnly ? !state.savedNovel || state.phase !== 'saved' : !!state.savedNovel) return
  if (!state.form.title.trim()) { message.error('书名不能为空'); return }
  const payload = changesOnly ? projectChanges(state) : projectFields(state.form)
  if (!Object.keys(payload).length) { message.warning('当前没有需要保存的修改。'); return }
  const savedId = changesOnly ? state.savedNovel!.id : null
  const scope = asyncScope.capture()
  const epoch = createEpoch, revision = state.revision, operation = projectOperation()
  const own = () => state.pending === operation
  const current = () => alive && asyncScope.isCurrent(scope) && createEpoch === epoch && showCreate.value && createState.value === state
  state.original = projectFields(state.form)
  state.pending = operation
  state.phase = 'pending'
  state.error = ''
  try {
    const result = savedId ? await api.updateNovel(savedId, payload) : await api.createNovel(payload as ProjectFields)
    if (!own()) return
    if (!isSavedProject(result) || (savedId && result.id !== savedId) || (Object.keys(payload) as (keyof ProjectFields)[]).some(key => result[key] !== payload[key])) throw new Error('保存回应不完整或与原提交不一致，请保留稿件并核对目录。')
    state.savedNovel = result
    state.phase = 'saved'
    if (state.revision === revision) { state.baseline = projectFields(result); state.form = projectFields(result) }
    else state.baseline = { ...(state.baseline ?? projectFields(result)), ...payload }
    if (!current()) return
    // 已保存的事实先投影；后置目录失败不能改成未知或再次创建。
    novelLoadSequence += 1
    novels.value = [result, ...novels.value.filter(item => item.id !== result.id)]
    message.success(savedId ? '当前修改已保存' : '原提交已保存')
    await loadNovels(`${savedId ? '当前修改' : '原提交'}已保存，目录刷新未完成：`, current, false)
  } catch (e) {
    if (!own()) return
    state.phase = 'unknown'
    state.error = (e as Error).message
    if (current()) message.warning(`保存结果待核对：${state.error}。原提交和当前构思在本页保留，不会自动重写。`)
  } finally { if (own()) state.pending = null }
}

function creationNotice() {
  const state = createState.value
  if (state.phase === 'pending') return '正在保存原提交；后来输入另行保留。关闭窗口不会取消后台请求。'
  if (state.phase === 'unknown') return '保存结果待核对。请先复制原提交与当前构思，核对实际目录；同名作品不能证明原创建成功。'
  if (state.savedNovel) return `原提交已保存为「${state.savedNovel.title}」。当前修改需另行明确保存。`
  return '作品构思仅保留在当前页面，完整刷新或关闭前请先复制。'
}

function closeCreate() {
  const state = createState.value, epoch = createEpoch, revision = state.revision
  const close = () => {
    if (!alive || createState.value !== state || !showCreate.value || createEpoch !== epoch) return false
    if (state.revision !== revision) { message.warning('确认期间构思又有修改，请重新核对。'); return false }
    showCreate.value = false
  }
  if (!projectDirty(state)) return close()
  dialog.warning({ title: '关闭并保留构思？', content: '原提交与当前构思在本页面“作品构思”入口保留；关闭不会取消后台保存。完整刷新或关闭页面前请先复制。',
    positiveText: '关闭并保留', negativeText: '继续编辑', onPositiveClick: close })
}

function startFreshCreate() {
  const state = createState.value, epoch = createEpoch, revision = state.revision
  if (!alive || state.pending !== null || state.checking !== null || state.phase === 'unknown') return
  dialog.warning({ title: '明确另建作品？', content: '将开始新的创建意图并替换当前页内原提交与构思，请先复制。已保存作品不会删除，同名同稿仍会单独创建。',
    positiveText: '已保留，另建作品', negativeText: '继续保留', onPositiveClick: () => {
      if (!alive || !showCreate.value || createState.value !== state || createEpoch !== epoch || state.revision !== revision) { if (alive) message.warning('构思已变化，请重新核对。'); return false }
      createState.value = freshCreationDraft()
      createEpoch += 1
    } })
}

async function copyProject(original = false) {
  const state = createState.value, epoch = createEpoch, fields = original ? state.original : state.form
  if (!fields) return
  const text = projectText(fields)
  try { await navigator.clipboard.writeText(text) }
  catch {
    if (!alive || epoch !== createEpoch || state !== createState.value) return
    copyFallback.value = text
    copyFallbackOriginal.value = original
    message.warning('复制不可用，请从只读文本框手动选中并复制。')
  }
}

async function checkProjectDirectory() {
  const state = createState.value, scope = asyncScope.capture(), epoch = createEpoch, operation = projectOperation()
  if (!alive || state.pending !== null || state.checking !== null) return
  state.checking = operation
  const current = () => alive && asyncScope.isCurrent(scope) && epoch === createEpoch && state === createState.value
  try {
    const items = await api.listNovels()
    if (!Array.isArray(items) || !items.every(isSavedProject)) throw new Error('目录回应不完整，请保留稿件后再核对。')
    state.directory = items
    if (state.savedNovel) {
      const saved = items.find(item => item.id === state.savedNovel!.id)
      if (saved) {
        state.savedNovel = saved
        const baseline = state.baseline ?? projectFields(saved)
        state.baseline = { ...baseline }
        if (state.original) for (const key of ['title','genre','summary'] as const) {
          if (state.original[key] !== baseline[key]) state.baseline[key] = saved[key]
        }
        state.phase = 'saved'
        state.error = '已读取此作品当前资料，请人工核对原提交与当前构思后再保存。'
      } else state.error = '原作品当前不在目录中；仅保留稿件，不会重新创建它。'
    }
    if (current()) message.warning('已读取当前目录，仅供人工核对，不按名称认定原创建结果。')
  } catch (e) { if (current()) message.error(`目录读取未完成：${(e as Error).message}`) }
  finally { if (state.checking === operation) state.checking = null }
}

function openSavedProject() {
  if (createState.value.savedNovel) changeNovel(createState.value.savedNovel.id)
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!projectUnsaved.value) return
  event.preventDefault()
  event.returnValue = ''
}

function onGraphEdit(payload: { type: EntityType; id: string }) {
  // 初次懒加载时先挂载设定面板，再触发它的 focusTarget watcher。
  if (settingsMounted.value) focusTarget.value = { ...payload }
  else pendingGraphFocus.value = { ...payload }
  activeTab.value = 'settings'
}

function onSettingsMounted() {
  settingsMounted.value = true
  if (pendingGraphFocus.value) {
    focusTarget.value = { ...pendingGraphFocus.value }
    pendingGraphFocus.value = null
  }
}

function navigateSafely(target: string, navigate: () => void) {
  if (projectUnsaved.value || settingsDirty.value || outlineDirty.value || (activeTab.value === 'writing' && writingDirty.value) || (activeTab.value === 'autopilot' && autopilotDirty.value)) {
    navigationTarget.value = target
    navigationWarning.value = [
      projectUnsaved.value ? '作品构思或待核对原提交在当前页面内保留，完整刷新或关闭前请复制。' : '',
      settingsDirty.value ? '设定或文风有未保存修改，或原提交待核对。原提交、后来稿和等待状态在本页面内保留，不会自动保存；完整刷新或关闭前请复制。' : '',
      outlineDirty.value ? '大纲有未保存编辑、生成预览，或正在保存。' : '',
      activeTab.value === 'writing' && writingDirty.value ? '当前章节有未保存修改、待核对请求或未完成输出。已建立手工保存或删除记录的已有章节原稿与后来稿在本页暂留；离页不会取消已发删除。其他未提交内容离开后可能放弃，完整刷新或关闭前请先复制。' : '',
      activeTab.value === 'autopilot' && autopilotDirty.value
        ? '自动驾驶继续在后台运行，离开只断开本页监控。未提交构思在当前页面内保留，完整刷新或关闭页面前请复制。未得到暂存确认的本页片段离开后可能丢失，请先复制保留；已暂存片段可以重新找回。'
        : '',
    ].filter(Boolean).join('')
    pendingNavigation.value = navigate
    showNavigationConfirm.value = true
  } else {
    navigate()
  }
}

function cancelNavigation() {
  showNavigationConfirm.value = false
  pendingNavigation.value = null
}

function confirmNavigation() {
  const navigate = pendingNavigation.value
  cancelNavigation()
  navigate?.()
}

function changeNovel(value: string | null) {
  if (value === novelId.value) return
  navigateSafely('切换作品', () => {
    focusTarget.value = null
    pendingGraphFocus.value = null
    novelId.value = value
  })
}

function returnToChat() {
  navigateSafely('返回对话', () => emit('back'))
}

function changeTab(value: string) {
  const next = value as typeof activeTab.value
  if (next === activeTab.value) return
  navigateSafely('切换页面', () => { activeTab.value = next })
}

onMounted(() => { loadNovels(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { alive = false; createEpoch += 1; asyncScope.dispose(); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="workbench">
    <header class="header">
      <div class="top-row">
        <div class="left">
          <n-button size="small" quaternary @click="returnToChat">← 返回对话</n-button>
          <span class="brand">灵墨 Lingmo</span>
        </div>
        <div class="novel-bar">
          <n-select
            :value="novelId"
            :options="novelOptions"
            @update:value="changeNovel"
            placeholder="选择作品"
            style="width: 260px"
          />
          <n-button type="primary" size="small" @click="openCreate">新建作品</n-button>
          <n-button v-if="hasProjectRecord" size="small" @click="openCreate" aria-label="页内作品构思">作品构思 · {{ createState.form.title || '未命名' }}</n-button>
        </div>
      </div>
      <n-tabs :value="activeTab" type="line" @update:value="changeTab">
        <n-tab-pane name="settings" tab="设定面板" />
        <n-tab-pane name="writing" tab="写作" />
        <n-tab-pane name="outline" tab="大纲" />
        <n-tab-pane name="autopilot" tab="自动驾驶" />
        <n-tab-pane name="foreshadowing" tab="伏笔追踪" />
        <n-tab-pane name="graph" tab="知识图谱" />
        <n-tab-pane name="delivery" tab="成品交付" />
      </n-tabs>
    </header>

    <main class="content">
      <SettingsPanel
        v-if="settingsVisited"
        v-show="activeTab === 'settings'"
        :novel-id="novelId ?? ''"
        :focus-target="focusTarget"
        :refresh-key="refreshKey"
        @dirty-change="settingsDirty = $event"
        @vue:mounted="onSettingsMounted"
      />
      <WritingPanel
        v-if="activeTab === 'writing'"
        :novel-id="novelId ?? ''"
        :initial-chapter-id="initialChapterId"
        @dirty-change="writingDirty = $event"
      />
      <OutlinePanel
        v-if="activeTab === 'outline'"
        :novel-id="novelId ?? ''"
        :refresh-key="refreshKey"
        @dirty-change="outlineDirty = $event"
      />
      <AutopilotPanel v-if="activeTab === 'autopilot'" :novel-id="novelId ?? ''" @dirty-change="autopilotDirty = $event" />
      <ForeshadowingTracking v-if="activeTab === 'foreshadowing'" :novel-id="novelId ?? ''" />
      <KnowledgeGraph
        v-if="activeTab === 'graph'"
        :novel-id="novelId ?? ''"
        @edit="onGraphEdit"
      />
      <BookDeliveryPanel v-if="activeTab === 'delivery'" :novel-id="novelId ?? ''" />
    </main>

    <n-modal
      :show="showNavigationConfirm"
      preset="card"
      :title="projectUnsaved ? '保留作品构思并离开？' : activeTab === 'settings' ? '保留设定页内稿并离开？' : activeTab === 'autopilot' ? '离开自动驾驶页？' : activeTab === 'writing' ? '离开写作页？' : '放弃未保存修改？'"
      style="width: min(480px, calc(100vw - 32px))"
      @update:show="cancelNavigation"
    >
      <p>{{ navigationWarning }}{{ projectUnsaved ? '作品构思会保留，其他面板的未保存内容仍需按提示核对。确定继续' : activeTab === 'autopilot' || activeTab === 'settings' || activeTab === 'writing' ? '确定' : '确定放弃并' }}{{ navigationTarget }}吗？</p>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="cancelNavigation">继续编辑</n-button>
          <n-button type="warning" @click="confirmNavigation">{{ projectUnsaved ? '保留并继续' : activeTab === 'autopilot' || activeTab === 'settings' || activeTab === 'writing' ? '离开并继续' : '放弃并继续' }}</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal :show="showCreate" preset="card" title="新建作品" style="width: 560px" :mask-closable="false" :close-on-esc="false" @update:show="(value: boolean) => { if (!value) closeCreate() }">
      <section aria-label="作品构思">
      <p role="alert">{{ creationNotice() }}</p>
      <p v-if="createState.error" role="alert">{{ createState.error }}</p>
      <n-form label-placement="top">
        <n-form-item label="书名" required>
          <n-input v-model:value="createForm.title" placeholder="请输入书名" :input-props="{ 'aria-label': '新建作品书名' }" />
        </n-form-item>
        <n-form-item label="题材/类型">
          <n-input v-model:value="createForm.genre" placeholder="如：玄幻 / 都市 / 言情" :input-props="{ 'aria-label': '新建作品题材' }" />
        </n-form-item>
        <n-form-item label="一句话简介">
          <n-input
            v-model:value="createForm.summary"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
            :input-props="{ 'aria-label': '新建作品梗概' }"
          />
        </n-form-item>
      </n-form>
      <n-button size="small" @click="copyProject()">复制当前构思</n-button>
      <template v-if="createState.original">
        <p>原提交（与后来输入分开保留）</p>
        <n-input type="textarea" readonly :value="projectText(createState.original)" :input-props="{ 'aria-label': '原提交构思' }" :autosize="{ minRows: 2, maxRows: 5 }" />
        <n-button size="small" @click="copyProject(true)">复制原提交</n-button>
      </template>
      <template v-if="createState.phase === 'unknown' || createState.directory">
        <n-button :loading="createState.checking !== null" :disabled="creating" @click="checkProjectDirectory">核对作品目录</n-button>
        <p>目录仅供人工核对，不会按同名作品认定原创建已保存或再次创建。</p>
        <ul v-if="createState.directory" aria-label="作品目录核对"><li v-for="item in createState.directory" :key="item.id">{{ item.title }} · {{ item.genre }}<p>{{ item.summary }}</p></li></ul>
      </template>
      </section>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="closeCreate">关闭并保留页内稿件</n-button>
          <n-button :disabled="creating || createState.checking !== null || createState.phase === 'unknown'" @click="startFreshCreate">另建作品</n-button>
          <n-button v-if="createState.savedNovel" @click="openSavedProject">打开已保存作品</n-button>
          <n-button v-if="createState.savedNovel" type="primary" :loading="creating" :disabled="createState.phase !== 'saved' || createState.checking !== null || !Object.keys(projectChanges(createState)).length" @click="saveProjectChanges">保存当前修改到此作品</n-button>
          <n-button v-else type="primary" :loading="creating" :disabled="createState.phase === 'unknown' || createState.checking !== null" @click="submitCreate">创建</n-button>
        </div>
      </template>
    </n-modal>
    <n-modal :show="!!copyFallback" preset="card" title="手动复制构思" style="width: 560px" @update:show="(value: boolean) => { if (!value) copyFallback = '' }">
      <p>剪贴板不可用，请手动选中并复制。关闭此框不会清除页内构思。</p>
      <n-input type="textarea" readonly :value="copyFallback" :input-props="{ 'aria-label': copyFallbackOriginal ? '手动复制原提交' : '当前作品构思' }" :autosize="{ minRows: 4, maxRows: 14 }" />
    </n-modal>
  </div>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
.header {
  border-bottom: 1px solid #e5e5e5;
  padding: 0 16px;
}
.top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
}
.left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand {
  font-size: 18px;
  font-weight: 700;
}
.novel-bar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.content {
  flex: 1;
  min-height: 0;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  flex-wrap: wrap;
}
[aria-label="作品目录核对"] { max-height: 180px; overflow-y: auto; }
</style>
