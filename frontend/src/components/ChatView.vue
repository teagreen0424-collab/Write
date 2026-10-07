<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  useDialog,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import type { Conversation, Novel } from '../types'
import ConversationSidebar from './ConversationSidebar.vue'
import ChatWindow from './ChatWindow.vue'
import { removeChatSubmissions } from '../utils/chatSubmission'
import {
  creationDraft, existingProjectDrafts, freshCreationDraft, isSavedProject,
  projectChanges, projectDirty, projectDraftsDirty, projectFields, projectOperation,
  projectText, promotionDraft,
} from '../utils/projectDraft'
import type { ProjectDraft, ProjectFields } from '../utils/projectDraft'

const emit = defineEmits<{
  (e: 'enter-workbench', novelId: string): void
  (e: 'open-outline', novelId: string): void
  (e: 'open-writing', novelId: string, chapterId: string): void
}>()

const message = useMessage()
const dialog = useDialog()

const novels = ref<Novel[]>([])
const conversations = ref<Conversation[]>([])
const selectedId = ref<string | null>(null)
const selected = computed(() => conversations.value.find((c) => c.id === selectedId.value) ?? null)

const renameTarget = ref<Conversation | null>(null)
const renameTitle = ref('')
const promoteTarget = ref<Conversation | null>(null)
const promoteState = ref<ProjectDraft | null>(null)
const promoteForm = computed({
  get: () => promoteState.value?.form ?? { title: '', genre: '', summary: '' },
  set: (fields: ProjectFields) => { if (promoteState.value) promoteState.value.form = projectFields(fields) },
})
const moveTarget = ref<Conversation | null>(null)
const moveNovelId = ref<string | null>(null)
const showCreate = ref(false)
const createState = ref(creationDraft())
const createForm = computed({ get: () => createState.value.form, set: (fields: ProjectFields) => { createState.value.form = projectFields(fields) } })
const projectRecords = computed(existingProjectDrafts)
const projectUnsaved = computed(projectDraftsDirty)
const projectForms = computed(() => {
  const forms: { kind: 'create' | 'promote'; state: ProjectDraft; form: ProjectFields; show: boolean; title: string }[] = [
    { kind: 'create', state: createState.value, form: createForm.value, show: showCreate.value, title: '新建作品' },
  ]
  if (promoteState.value) forms.push({ kind: 'promote', state: promoteState.value, form: promoteForm.value, show: !!promoteTarget.value, title: '转为作品' })
  return forms
})
const copyFallback = ref('')
const copyFallbackOriginal = ref(false)
let createEpoch = 0
let promoteEpoch = 0
const chatDirty = ref(false)
const deletingNovel = ref(false)
let alive = true
let loadId = 0

const novelOptions = computed(() => novels.value.map((n) => ({ label: n.title, value: n.id })))

async function loadAll(errorPrefix = '', canReport: () => boolean = () => alive) {
  const requestId = ++loadId
  try {
    const [n, c] = await Promise.all([api.listNovels(), api.listConversations()])
    if (!alive || requestId !== loadId) return false
    novels.value = n
    conversations.value = c
    if (selectedId.value && !c.some((x) => x.id === selectedId.value)) {
      selectedId.value = c[0]?.id ?? null
    } else if (!selectedId.value && c.length) {
      selectedId.value = c[0].id
    }
    return true
  } catch (e) {
    if (alive && requestId === loadId && canReport()) message.error(`${errorPrefix}${(e as Error).message}`)
    return false
  }
}

function select(id: string) {
  selectedId.value = id
}

async function newGlobal() {
  try {
    const c = await api.createConversation(null)
    await loadAll()
    selectedId.value = c.id
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function newNovelConversation(novelId: string) {
  try {
    const c = await api.createConversation(novelId)
    await loadAll()
    selectedId.value = c.id
  } catch (e) {
    message.error((e as Error).message)
  }
}

function openRename(c: Conversation) {
  renameTarget.value = c
  renameTitle.value = c.title
}

async function submitRename() {
  if (!renameTarget.value) return
  try {
    await api.updateConversation(renameTarget.value.id, { title: renameTitle.value })
    renameTarget.value = null
    await loadAll()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function openPromote(c: Conversation) {
  promoteEpoch += 1
  promoteTarget.value = c
  promoteState.value = promotionDraft(c.id, c.title || '')
}

async function submitPromote() {
  await submitProject('promote')
}

function openMove(c: Conversation) {
  moveTarget.value = c
  moveNovelId.value = novels.value[0]?.id ?? null
}

async function submitMove() {
  if (!moveTarget.value || !moveNovelId.value) return
  try {
    await api.updateConversation(moveTarget.value.id, { novel_id: moveNovelId.value })
    moveTarget.value = null
    await loadAll()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function removeConversation(c: Conversation) {
  dialog.warning({
    title: '删除会话',
    content: `确定删除会话「${c.title || '新对话'}」及其全部消息吗？该会话在本页保留的输入和原发送稿也会移除，请先复制需要保留的内容。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.deleteConversation(c.id)
        removeChatSubmissions([c.id])
        if (!alive) return
        loadId += 1
        conversations.value = conversations.value.filter((item) => item.id !== c.id)
        if (selectedId.value === c.id) selectedId.value = conversations.value[0]?.id ?? null
        await loadAll()
      } catch (e) {
        message.error((e as Error).message)
      }
    },
  })
}

function openCreate() {
  createEpoch += 1
  createState.value = creationDraft()
  showCreate.value = true
}

async function submitCreate() {
  await submitProject('create')
}

type ProjectKind = 'create' | 'promote'
const stateFor = (kind: ProjectKind) => kind === 'create' ? createState.value : promoteState.value
const epochFor = (kind: ProjectKind) => kind === 'create' ? createEpoch : promoteEpoch
function currentProject(kind: ProjectKind, state: ProjectDraft, epoch: number) {
  return alive && epoch === epochFor(kind) && stateFor(kind) === state &&
    (kind === 'create' ? showCreate.value : !!promoteTarget.value)
}
function projectNotice(state: ProjectDraft) {
  if (state.phase === 'pending') return '正在保存原提交；后来输入另行保留。关闭窗口不会取消后台请求。'
  if (state.phase === 'unknown') return '保存结果待核对。原提交和当前构思保留；请核对实际作品目录，不能按同名作品认定原请求已保存。'
  if (state.savedNovel) return `原提交已保存为「${state.savedNovel.title}」。当前修改需另行明确保存。`
  return '构思仅保留在当前页面内，完整刷新或关闭前请先复制。'
}
function rememberProject(result: Novel, conversationId: string) {
  loadId += 1
  novels.value = [result, ...novels.value.filter(item => item.id !== result.id)]
  if (conversationId) conversations.value = conversations.value.map(item => item.id === conversationId ? { ...item, novel_id: result.id } : item)
}
async function submitProject(kind: ProjectKind) {
  const state = stateFor(kind), conversationId = kind === 'promote' ? promoteTarget.value?.id ?? '' : ''
  if (!state || !alive || state.pending !== null || state.checking !== null || state.savedNovel || state.phase === 'unknown') return
  if (kind === 'promote' && (state.sourceExists === false || !conversationId || !conversations.value.some(item => item.id === conversationId && !item.novel_id))) {
    message.warning('请先核对该会话当前是否仍未绑定作品。')
    return
  }
  if (!state.form.title.trim()) { message.error('书名不能为空'); return }
  const original = projectFields(state.form), revision = state.revision, epoch = epochFor(kind), operation = projectOperation()
  state.original = original
  state.pending = operation
  state.phase = 'pending'
  state.error = ''
  const current = () => currentProject(kind, state, epoch)
  try {
    const result = kind === 'create' ? await api.createNovel(original) : await api.promoteConversation(conversationId, original)
    if (!isSavedProject(result) || (['title', 'genre', 'summary'] as const).some(key => result[key] !== original[key])) throw new Error('保存回应缺少完整作品或与原提交不一致，请核对实际目录。')
    state.savedNovel = result
    state.baseline = projectFields(result)
    state.phase = 'saved'
    if (kind === 'promote') { state.sourceExists = true; state.bindingId = result.id }
    if (state.revision === revision) state.form = projectFields(result)
    if (!current()) return
    rememberProject(result, conversationId)
    message.success('原提交已保存')
    await loadAll('原提交已保存，目录刷新未完成：', current)
  } catch (error) {
    state.phase = 'unknown'
    state.error = (error as Error).message
    if (current()) message.warning(`保存结果待核对：${state.error}。原提交和当前构思已在本页保留，请先复制。`)
  } finally { if (state.pending === operation) state.pending = null }
}

async function saveProjectChanges(kind: ProjectKind) {
  const state = stateFor(kind)
  if (!state || !alive || !state.savedNovel || state.phase !== 'saved' || state.pending !== null || state.checking !== null) return
  if (kind === 'promote' && (state.sourceExists === false || (state.sourceExists === true && state.bindingId !== state.savedNovel.id) || !conversations.value.some(item => item.id === promoteTarget.value?.id))) { message.warning('原会话已删除或当前绑定已变化；构思仍可复制，不会重新写入。'); return }
  if (!state.form.title.trim()) { message.error('书名不能为空'); return }
  const payload = projectChanges(state)
  if (!Object.keys(payload).length) { message.warning('当前没有需要保存的修改。'); return }
  const novelId = state.savedNovel.id, epoch = epochFor(kind), revision = state.revision, operation = projectOperation()
  state.original = projectFields(state.form)
  state.pending = operation
  state.phase = 'pending'
  state.error = ''
  const current = () => currentProject(kind, state, epoch)
  try {
    const result = await api.updateNovel(novelId, payload)
    if (!isSavedProject(result) || result.id !== novelId || (Object.keys(payload) as (keyof ProjectFields)[]).some(key => result[key] !== payload[key])) throw new Error('修改回应与本次保存不一致，请核对原作品当前资料。')
    state.savedNovel = result
    state.phase = 'saved'
    if (state.revision === revision) {
      state.baseline = projectFields(result)
      state.form = projectFields(result)
    } else {
      // 未提交字段仍以原有效基线比较，避免旧表单变成下一次作者修改。
      state.baseline = { ...state.baseline!, ...payload }
    }
    if (!current()) return
    rememberProject(result, '')
    message.success('当前修改已保存')
    await loadAll('当前修改已保存，目录刷新未完成：', current)
  } catch (error) {
    state.phase = 'unknown'
    state.error = (error as Error).message
    if (current()) message.warning(`修改保存结果待核对：${state.error}。请先复制，再读取此作品当前资料。`)
  } finally { if (state.pending === operation) state.pending = null }
}

async function checkProjectDirectory(kind: ProjectKind) {
  const state = stateFor(kind)
  if (!alive || !state || state.pending !== null || state.checking !== null) return
  const epoch = epochFor(kind), conversationId = kind === 'promote' ? promoteTarget.value?.id ?? '' : ''
  const operation = projectOperation(), current = () => currentProject(kind, state, epoch)
  state.checking = operation
  try {
    const [items, threads] = await Promise.all([api.listNovels(), api.listConversations()])
    if (!Array.isArray(items) || !items.every(isSavedProject) || !Array.isArray(threads) || !threads.every(item => item && typeof item === 'object' && typeof item.id === 'string' && item.id.length > 0 && typeof item.title === 'string' && typeof item.created_at === 'string' && typeof item.updated_at === 'string' && (item.novel_id === null || (typeof item.novel_id === 'string' && item.novel_id.length > 0)))) throw new Error('目录回应不完整，请保留稿件后再核对。')
    state.directory = items
    if (kind === 'promote') {
      const source = threads.find(item => item.id === conversationId)
      state.sourceExists = !!source
      state.bindingId = source?.novel_id ?? null
    }
    // 已知真实ID的PUT结果可只读核对当前资料，普通创建绝不猜同名结果。
    if (state.savedNovel) {
      const saved = items.find(item => item.id === state.savedNovel!.id)
      if (kind === 'promote' && (!state.sourceExists || state.bindingId !== state.savedNovel.id)) {
        state.phase = 'unknown'
        state.error = '原会话已删除或当前绑定已变化；仅保留构思供复制，不会重新写入。'
      } else if (saved) {
        state.savedNovel = saved
        // 只推进本次实际提交字段；未提交旧值不会成为新意图。
        const baseline = state.baseline ?? projectFields(saved)
        const sent = state.original
        state.baseline = { ...baseline }
        if (sent) for (const key of ['title', 'genre', 'summary'] as const) {
          if (sent[key] !== baseline[key]) state.baseline[key] = saved[key]
        }
        state.phase = 'saved'
        state.error = '已读取此作品当前资料，请人工核对原提交与当前构思后再保存。'
      } else state.error = '原作品当前不在目录中；请保留稿件，不会重新创建它。'
    }
    if (current()) message.warning('已读取当前目录，仅供人工核对；不按作品名称认定原创建结果。')
  } catch (error) { if (current()) message.error(`目录读取未完成：${(error as Error).message}`) }
  finally { if (state.checking === operation) state.checking = null }
}

async function copyProject(kind: ProjectKind, original = false) {
  const state = stateFor(kind)
  if (!state) return
  const fields = original ? state.original : state.form
  if (!fields) return
  const text = projectText(fields), epoch = epochFor(kind)
  try {
    await navigator.clipboard.writeText(text)
    if (currentProject(kind, state, epoch)) message.success(original ? '原提交已复制' : '当前构思已复制')
  } catch {
    if (!currentProject(kind, state, epoch)) return
    copyFallback.value = text
    copyFallbackOriginal.value = original
    message.warning('复制不可用，请从只读文本框手动选中并复制。')
  }
}

function closeProject(kind: ProjectKind) {
  const state = stateFor(kind)
  if (!state) return
  const epoch = epochFor(kind), revision = state.revision
  const close = () => {
    if (!currentProject(kind, state, epoch)) return false
    if (state.revision !== revision) { message.warning('确认期间构思又有修改，请重新核对。'); return false }
    if (kind === 'create') { createEpoch += 1; showCreate.value = false }
    else { promoteEpoch += 1; promoteTarget.value = null }
  }
  if (!projectDirty(state)) return close()
  dialog.warning({ title: '关闭并保留构思？', content: '原提交与当前构思仍在本页面的“作品构思”入口保留。关闭窗口不会取消后台保存；完整刷新或关闭页面前请先复制。',
    positiveText: '关闭并保留', negativeText: '继续编辑', onPositiveClick: close })
}

function reopenProject(record: ReturnType<typeof existingProjectDrafts>[number]) {
  if (record.kind === 'create') return openCreate()
  // 即使原会话已绑定或删除，页内作者稿仍可只读找回；新写入仍检查真实来源。
  openPromote(conversations.value.find(item => item.id === record.conversationId) ?? { id: record.conversationId, title: record.state.form.title, novel_id: null, created_at: '', updated_at: '' })
}

function startFreshCreate() {
  const state = createState.value
  if (state.pending !== null || state.phase === 'unknown' || state.checking !== null) return
  const epoch = createEpoch, revision = state.revision
  const start = () => {
    if (!currentProject('create', state, epoch) || state.revision !== revision) { if (alive) message.warning('构思已变化，请重新核对。'); return false }
    createState.value = freshCreationDraft()
    createEpoch += 1
  }
  dialog.warning({ title: '明确另建作品？', content: '将开始一次新的创建意图。当前页内原提交与构思会替换，请先复制；已保存作品不会删除，同名同稿的新作品也会单独创建。', positiveText: '已保留，另建作品', negativeText: '继续保留', onPositiveClick: start })
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (!projectUnsaved.value) return
  event.preventDefault()
  event.returnValue = ''
}

function enterWorkbench(novelId: string) {
  leaveChat(() => emit('enter-workbench', novelId))
}

function leaveChat(navigate: () => void) {
  if (!chatDirty.value && !projectUnsaved.value) return navigate()
  dialog.warning({
    title: '离开对话？', content: '有未保存输入、待核对原稿、作品构思或未处理回复。当前页面内暂保留，完整刷新或关闭前请先复制；确定继续离开吗？',
    positiveText: '保留并继续', negativeText: '继续编辑', onPositiveClick: navigate,
  })
}

function removeNovel(novel: Novel) {
  if (deletingNovel.value) return
  dialog.warning({
    title: '删除作品',
    content: `确定删除作品「${novel.title}」及全部设定、大纲、章节、版本和作品会话吗？这些会话在本页保留的输入和原发送稿也会移除，请先复制。此操作不可恢复。`,
    positiveText: '删除作品', negativeText: '取消',
    onPositiveClick: async () => {
      if (deletingNovel.value) return false
      deletingNovel.value = true
      const removedConversationIds = conversations.value.filter(item => item.novel_id === novel.id).map(item => item.id)
      try {
        await api.deleteNovel(novel.id)
        removeChatSubmissions(removedConversationIds, novel.id)
        if (!alive) return
        loadId += 1
        novels.value = novels.value.filter((item) => item.id !== novel.id)
        conversations.value = conversations.value.filter((item) => item.novel_id !== novel.id)
        if (!conversations.value.some((item) => item.id === selectedId.value)) selectedId.value = conversations.value[0]?.id ?? null
        await loadAll()
        if (alive) message.success('作品已删除')
      } catch (error) {
        if (alive) message.error((error as Error).message)
        return false
      } finally { deletingNovel.value = false }
    },
  })
}

onMounted(() => { loadAll(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { alive = false; loadId += 1; createEpoch += 1; promoteEpoch += 1; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="chat-view">
    <ConversationSidebar
      :novels="novels"
      :conversations="conversations"
      :selected-id="selectedId"
      @new-global="newGlobal"
      @new-novel-conversation="newNovelConversation"
      @select="select"
      @rename="openRename"
      @delete="removeConversation"
      @promote="openPromote"
      @move="openMove"
      @enter-workbench="enterWorkbench"
      @create-novel="openCreate"
      @delete-novel="removeNovel"
    />
    <div class="chat-content">
    <section v-if="projectRecords.length" class="project-recovery" aria-label="页内作品构思">
      <strong>作品构思</strong>
      <span>仅保留在本页面，完整刷新前请先复制。</span>
      <n-button v-for="record in projectRecords" :key="record.kind + record.conversationId" size="small" @click="reopenProject(record)">
        {{ record.kind === 'create' ? '新建作品构思' : '升级作品构思' }} · {{ record.state.form.title || '未命名' }}
      </n-button>
    </section>
    <ChatWindow
      :conversation="selected"
      :novels="novels"
      :conversation-ids="conversations.map((item) => item.id)"
      @changed="loadAll"
      @promote="selected && openPromote(selected)"
      @move="selected && openMove(selected)"
      @dirty-change="chatDirty = $event"
      @open-outline="(novelId: string) => leaveChat(() => emit('open-outline', novelId))"
      @open-writing="(novelId: string, chapterId: string) => leaveChat(() => emit('open-writing', novelId, chapterId))"
    />
    </div>

    <n-modal
      :show="!!renameTarget"
      preset="card"
      title="重命名会话"
      style="width: 400px"
      @update:show="(v: boolean) => { if (!v) renameTarget = null }"
    >
      <n-input v-model:value="renameTitle" placeholder="会话标题" />
      <template #footer>
        <n-button @click="renameTarget = null">取消</n-button>
        <n-button type="primary" @click="submitRename">确定</n-button>
      </template>
    </n-modal>

    <n-modal
      :show="!!moveTarget"
      preset="card"
      title="移动到作品"
      style="width: 400px"
      @update:show="(v: boolean) => { if (!v) moveTarget = null }"
    >
      <n-select v-model:value="moveNovelId" :options="novelOptions" placeholder="选择目标作品" />
      <template #footer>
        <n-button @click="moveTarget = null">取消</n-button>
        <n-button type="primary" @click="submitMove">确定</n-button>
      </template>
    </n-modal>

    <n-modal v-for="entry in projectForms" :key="entry.kind" :show="entry.show" preset="card" :title="entry.title" style="width: 560px"
      :close-on-esc="false" :mask-closable="false" @update:show="(v: boolean) => { if (!v) closeProject(entry.kind) }">
      <section aria-label="作品构思">
      <p role="alert">{{ projectNotice(entry.state) }}</p>
      <p v-if="entry.state.error" role="alert">{{ entry.state.error }}</p>
      <n-form label-placement="top">
        <n-form-item label="书名" required>
          <n-input v-model:value="entry.form.title" placeholder="请输入书名" :input-props="{ 'aria-label': entry.kind === 'create' ? '新建作品书名' : '升级作品书名' }" />
        </n-form-item>
        <n-form-item label="题材/类型">
          <n-input v-model:value="entry.form.genre" placeholder="如：玄幻 / 都市 / 言情" :input-props="{ 'aria-label': entry.kind === 'create' ? '新建作品题材' : '升级作品题材' }" />
        </n-form-item>
        <n-form-item label="一句话简介">
          <n-input v-model:value="entry.form.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" :input-props="{ 'aria-label': entry.kind === 'create' ? '新建作品梗概' : '升级作品梗概' }" />
        </n-form-item>
      </n-form>
      <n-button size="small" @click="copyProject(entry.kind)">复制当前构思</n-button>
      <template v-if="entry.state.original">
        <p>原提交（与后来输入分开保留）</p>
        <n-input type="textarea" readonly :value="projectText(entry.state.original)" :input-props="{ 'aria-label': '原提交构思' }" :autosize="{ minRows: 2, maxRows: 5 }" />
        <n-button size="small" @click="copyProject(entry.kind, true)">复制原提交</n-button>
      </template>
      <template v-if="entry.state.phase === 'unknown' || entry.state.directory">
        <n-button :loading="entry.state.checking !== null" :disabled="entry.state.pending !== null" @click="checkProjectDirectory(entry.kind)">核对作品目录</n-button>
        <p>目录只供人工核对，不会自动认定原创建已成功或再次创建。</p>
        <p v-if="entry.kind === 'promote' && entry.state.directory">当前会话绑定：{{ entry.state.bindingId ? entry.state.directory.find(item => item.id === entry.state.bindingId)?.title || '目录中未找到该作品' : '未绑定或会话已不在目录' }}。这只说明当前绑定，不是原提交回执。</p>
        <ul v-if="entry.state.directory" aria-label="作品目录核对"><li v-for="item in entry.state.directory" :key="item.id">{{ item.title }} · {{ item.genre }}<p>{{ item.summary }}</p></li></ul>
      </template>
      </section>
      <template #footer>
        <n-button @click="closeProject(entry.kind)">关闭并保留页内稿件</n-button>
        <n-button v-if="entry.kind === 'create'" :disabled="entry.state.pending !== null || entry.state.checking !== null || entry.state.phase === 'unknown'" @click="startFreshCreate">另建作品</n-button>
        <n-button v-if="entry.state.savedNovel" type="primary" :loading="entry.state.pending !== null" :disabled="entry.state.phase !== 'saved' || entry.state.checking !== null || !Object.keys(projectChanges(entry.state)).length" @click="saveProjectChanges(entry.kind)">保存当前修改到此作品</n-button>
        <n-button v-else type="primary" :loading="entry.state.pending !== null" :disabled="entry.state.phase === 'unknown' || entry.state.checking !== null" @click="entry.kind === 'create' ? submitCreate() : submitPromote()">{{ entry.kind === 'create' ? '创建' : '创建作品' }}</n-button>
      </template>
    </n-modal>
    <n-modal :show="!!copyFallback" preset="card" title="手动复制构思" style="width: 560px" @update:show="(v: boolean) => { if (!v) copyFallback = '' }">
      <p>剪贴板不可用，请手动选中并复制。关闭此框不会清除页内稿件。</p>
      <n-input type="textarea" readonly :value="copyFallback" :input-props="{ 'aria-label': copyFallbackOriginal ? '手动复制原提交' : '当前作品构思' }" :autosize="{ minRows: 4, maxRows: 14 }" />
    </n-modal>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  height: 100vh;
}
.chat-content { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.project-recovery { padding: 8px 16px; border-bottom: 1px solid #eee; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; max-height: 120px; overflow-y: auto; }
.project-recovery span { color: #777; font-size: 12px; }
[aria-label="作品目录核对"] { max-height: 180px; overflow-y: auto; }
</style>
