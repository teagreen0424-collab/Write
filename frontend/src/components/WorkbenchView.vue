<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  NTabPane,
  NTabs,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import type { EntityType, Novel } from '../types'
import SettingsPanel from './SettingsPanel.vue'
import KnowledgeGraph from './KnowledgeGraph.vue'
import WritingPanel from './WritingPanel.vue'
import OutlinePanel from './OutlinePanel.vue'
import AutopilotPanel from './AutopilotPanel.vue'
import ForeshadowingTracking from './ForeshadowingTracking.vue'

const props = defineProps<{ initialNovelId?: string | null }>()
const emit = defineEmits<{ (e: 'back'): void }>()

const message = useMessage()

const novels = ref<Novel[]>([])
const novelId = ref<string | null>(null)
const activeTab = ref<'settings' | 'writing' | 'outline' | 'autopilot' | 'foreshadowing' | 'graph'>(
  'settings',
)
const focusTarget = ref<{ type: EntityType; id: string } | null>(null)
const refreshKey = ref(0)

const showCreate = ref(false)
const createForm = ref({ title: '', genre: '', summary: '' })

const novelOptions = computed(() => novels.value.map((n) => ({ label: n.title, value: n.id })))

async function loadNovels() {
  try {
    novels.value = await api.listNovels()
    if (props.initialNovelId && novels.value.some((n) => n.id === props.initialNovelId)) {
      novelId.value = props.initialNovelId
    } else if (!novelId.value && novels.value.length) {
      novelId.value = novels.value[0].id
    }
  } catch (e) {
    message.error((e as Error).message)
  }
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
    const novel = await api.createNovel(createForm.value)
    showCreate.value = false
    await loadNovels()
    novelId.value = novel.id
    message.success('已创建作品')
  } catch (e) {
    message.error((e as Error).message)
  }
}

function onGraphEdit(payload: { type: EntityType; id: string }) {
  focusTarget.value = { ...payload }
  activeTab.value = 'settings'
}

onMounted(loadNovels)
</script>

<template>
  <div class="workbench">
    <header class="header">
      <div class="top-row">
        <div class="left">
          <n-button size="small" quaternary @click="emit('back')">← 返回对话</n-button>
          <span class="brand">灵墨 Lingmo</span>
        </div>
        <div class="novel-bar">
          <n-select
            v-model:value="novelId"
            :options="novelOptions"
            placeholder="选择作品"
            style="width: 260px"
          />
          <n-button type="primary" size="small" @click="openCreate">新建作品</n-button>
        </div>
      </div>
      <n-tabs v-model:value="activeTab" type="line">
        <n-tab-pane name="settings" tab="设定面板" />
        <n-tab-pane name="writing" tab="写作" />
        <n-tab-pane name="outline" tab="大纲" />
        <n-tab-pane name="autopilot" tab="自动驾驶" />
        <n-tab-pane name="foreshadowing" tab="伏笔追踪" />
        <n-tab-pane name="graph" tab="知识图谱" />
      </n-tabs>
    </header>

    <main class="content">
      <SettingsPanel
        v-show="activeTab === 'settings'"
        :novel-id="novelId ?? ''"
        :focus-target="focusTarget"
        :refresh-key="refreshKey"
      />
      <WritingPanel v-if="activeTab === 'writing'" :novel-id="novelId ?? ''" />
      <OutlinePanel
        v-if="activeTab === 'outline'"
        :novel-id="novelId ?? ''"
        :refresh-key="refreshKey"
      />
      <AutopilotPanel v-if="activeTab === 'autopilot'" :novel-id="novelId ?? ''" />
      <ForeshadowingTracking v-if="activeTab === 'foreshadowing'" :novel-id="novelId ?? ''" />
      <KnowledgeGraph
        v-if="activeTab === 'graph'"
        :novel-id="novelId ?? ''"
        @edit="onGraphEdit"
      />
    </main>

    <n-modal v-model:show="showCreate" preset="card" title="新建作品" style="width: 480px">
      <n-form label-placement="top">
        <n-form-item label="书名" required>
          <n-input v-model:value="createForm.title" placeholder="请输入书名" />
        </n-form-item>
        <n-form-item label="题材/类型">
          <n-input v-model:value="createForm.genre" placeholder="如：玄幻 / 都市 / 言情" />
        </n-form-item>
        <n-form-item label="一句话简介">
          <n-input
            v-model:value="createForm.summary"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
          />
        </n-form-item>
      </n-form>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showCreate = false">取消</n-button>
          <n-button type="primary" @click="submitCreate">创建</n-button>
        </div>
      </template>
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
}
</style>
