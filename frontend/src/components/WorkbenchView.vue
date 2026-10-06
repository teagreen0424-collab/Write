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
import BookDeliveryPanel from './BookDeliveryPanel.vue'

const props = defineProps<{
  initialNovelId?: string | null
  initialTab?: 'settings' | 'outline' | 'writing'
  initialChapterId?: string | null
}>()
const emit = defineEmits<{ (e: 'back'): void }>()

const message = useMessage()

const novels = ref<Novel[]>([])
const novelId = ref<string | null>(null)
const activeTab = ref<'settings' | 'writing' | 'outline' | 'autopilot' | 'foreshadowing' | 'graph' | 'delivery'>(
  props.initialTab ?? 'settings',
)
const focusTarget = ref<{ type: EntityType; id: string } | null>(null)
const refreshKey = ref(0)
const writingDirty = ref(false)
const autopilotDirty = ref(false)
const showNavigationConfirm = ref(false)
const navigationTarget = ref('')
const navigationWarning = ref('')
const pendingNavigation = ref<(() => void) | null>(null)

const showCreate = ref(false)
const createForm = ref({ title: '', genre: '', summary: '' })

const novelOptions = computed(() => novels.value.map((n) => ({ label: n.title, value: n.id })))

async function loadNovels() {
  try {
    novels.value = await api.listNovels()
    if (!novelId.value && props.initialNovelId && novels.value.some((n) => n.id === props.initialNovelId)) {
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
    changeNovel(novel.id)
    message.success('已创建作品')
  } catch (e) {
    message.error((e as Error).message)
  }
}

function onGraphEdit(payload: { type: EntityType; id: string }) {
  focusTarget.value = { ...payload }
  activeTab.value = 'settings'
}

function navigateSafely(target: string, navigate: () => void) {
  if ((activeTab.value === 'writing' && writingDirty.value) || (activeTab.value === 'autopilot' && autopilotDirty.value)) {
    navigationTarget.value = target
    navigationWarning.value = activeTab.value === 'autopilot'
      ? '离开会断开自动驾驶的本机监控连接，不能保证后台任务停止。已自动暂存的片段可以重新找回；未得到暂存确认的内容离开后可能丢失，请先复制保留。'
      : '当前章节有未保存修改或未完成输出。'
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
  navigateSafely('切换作品', () => { novelId.value = value })
}

function returnToChat() {
  navigateSafely('返回对话', () => emit('back'))
}

function changeTab(value: string) {
  const next = value as typeof activeTab.value
  if (next === activeTab.value) return
  navigateSafely(activeTab.value === 'autopilot' ? '离开自动驾驶页' : '离开写作页', () => { activeTab.value = next })
}

onMounted(loadNovels)
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
        v-show="activeTab === 'settings'"
        :novel-id="novelId ?? ''"
        :focus-target="focusTarget"
        :refresh-key="refreshKey"
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
      :title="activeTab === 'autopilot' ? '离开自动驾驶页？' : '放弃未保存修改？'"
      style="width: min(480px, calc(100vw - 32px))"
      @update:show="cancelNavigation"
    >
      <p>{{ navigationWarning }}{{ activeTab === 'autopilot' ? '确定' : '确定放弃并' }}{{ navigationTarget }}吗？</p>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="cancelNavigation">继续编辑</n-button>
          <n-button type="warning" @click="confirmNavigation">{{ activeTab === 'autopilot' ? '离开并继续' : '放弃并继续' }}</n-button>
        </div>
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
