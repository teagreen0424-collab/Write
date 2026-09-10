<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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

const emit = defineEmits<{ (e: 'enter-workbench', novelId: string): void }>()

const message = useMessage()
const dialog = useDialog()

const novels = ref<Novel[]>([])
const conversations = ref<Conversation[]>([])
const selectedId = ref<string | null>(null)
const selected = computed(() => conversations.value.find((c) => c.id === selectedId.value) ?? null)

const renameTarget = ref<Conversation | null>(null)
const renameTitle = ref('')
const promoteTarget = ref<Conversation | null>(null)
const promoteForm = ref({ title: '', genre: '', summary: '' })
const moveTarget = ref<Conversation | null>(null)
const moveNovelId = ref<string | null>(null)
const showCreate = ref(false)
const createForm = ref({ title: '', genre: '', summary: '' })

const novelOptions = computed(() => novels.value.map((n) => ({ label: n.title, value: n.id })))

async function loadAll() {
  try {
    const [n, c] = await Promise.all([api.listNovels(), api.listConversations()])
    novels.value = n
    conversations.value = c
    if (selectedId.value && !c.some((x) => x.id === selectedId.value)) {
      selectedId.value = c[0]?.id ?? null
    } else if (!selectedId.value && c.length) {
      selectedId.value = c[0].id
    }
  } catch (e) {
    message.error((e as Error).message)
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
  promoteTarget.value = c
  promoteForm.value = { title: c.title || '', genre: '', summary: '' }
}

async function submitPromote() {
  if (!promoteTarget.value) return
  if (!promoteForm.value.title.trim()) {
    message.error('书名不能为空')
    return
  }
  try {
    await api.promoteConversation(promoteTarget.value.id, promoteForm.value)
    promoteTarget.value = null
    await loadAll()
  } catch (e) {
    message.error((e as Error).message)
  }
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
    content: `确定删除会话「${c.title || '新对话'}」及其全部消息吗？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.deleteConversation(c.id)
        if (selectedId.value === c.id) selectedId.value = null
        await loadAll()
      } catch (e) {
        message.error((e as Error).message)
      }
    },
  })
}

function openCreate() {
  createForm.value = { title: '', genre: '', summary: '' }
  showCreate.value = true
}

async function submitCreate() {
  if (!createForm.value.title.trim()) {
    message.error('书名不能为空')
    return
  }
  try {
    await api.createNovel(createForm.value)
    showCreate.value = false
    await loadAll()
    message.success('已创建作品')
  } catch (e) {
    message.error((e as Error).message)
  }
}

function enterWorkbench(novelId: string) {
  emit('enter-workbench', novelId)
}

onMounted(loadAll)
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
    />
    <ChatWindow
      :conversation="selected"
      :novels="novels"
      @changed="loadAll"
      @promote="selected && openPromote(selected)"
      @move="selected && openMove(selected)"
    />

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
      :show="!!promoteTarget"
      preset="card"
      title="转为作品"
      style="width: 480px"
      @update:show="(v: boolean) => { if (!v) promoteTarget = null }"
    >
      <n-form label-placement="top">
        <n-form-item label="书名" required>
          <n-input v-model:value="promoteForm.title" placeholder="请输入书名" />
        </n-form-item>
        <n-form-item label="题材/类型">
          <n-input v-model:value="promoteForm.genre" placeholder="如：玄幻 / 都市 / 言情" />
        </n-form-item>
        <n-form-item label="一句话简介">
          <n-input v-model:value="promoteForm.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-button @click="promoteTarget = null">取消</n-button>
        <n-button type="primary" @click="submitPromote">创建作品</n-button>
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

    <n-modal v-model:show="showCreate" preset="card" title="新建作品" style="width: 480px">
      <n-form label-placement="top">
        <n-form-item label="书名" required>
          <n-input v-model:value="createForm.title" placeholder="请输入书名" />
        </n-form-item>
        <n-form-item label="题材/类型">
          <n-input v-model:value="createForm.genre" placeholder="如：玄幻 / 都市 / 言情" />
        </n-form-item>
        <n-form-item label="一句话简介">
          <n-input v-model:value="createForm.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-button @click="showCreate = false">取消</n-button>
        <n-button type="primary" @click="submitCreate">创建</n-button>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  height: 100vh;
}
</style>
