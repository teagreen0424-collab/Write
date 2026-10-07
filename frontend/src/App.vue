<script setup lang="ts">
import { defineAsyncComponent, ref } from 'vue'
import ChatView from './components/ChatView.vue'

const WorkbenchView = defineAsyncComponent(() => import('./components/WorkbenchView.vue'))

const view = ref<'chat' | 'workbench'>('chat')
const workbenchNovelId = ref<string | null>(null)
const workbenchTab = ref<'settings' | 'outline' | 'writing'>('settings')
const workbenchChapterId = ref<string | null>(null)

function onEnterWorkbench(novelId: string) {
  workbenchNovelId.value = novelId
  workbenchTab.value = 'settings'
  workbenchChapterId.value = null
  view.value = 'workbench'
}

function onOpenOutline(novelId: string) {
  workbenchNovelId.value = novelId
  workbenchTab.value = 'outline'
  workbenchChapterId.value = null
  view.value = 'workbench'
}

function onOpenWriting(novelId: string, chapterId: string) {
  workbenchNovelId.value = novelId
  workbenchTab.value = 'writing'
  workbenchChapterId.value = chapterId
  view.value = 'workbench'
}

function onBack() {
  view.value = 'chat'
}
</script>

<template>
  <ChatView v-if="view === 'chat'" @enter-workbench="onEnterWorkbench" @open-outline="onOpenOutline" @open-writing="onOpenWriting" />
  <WorkbenchView v-else :initial-novel-id="workbenchNovelId" :initial-tab="workbenchTab" :initial-chapter-id="workbenchChapterId" @back="onBack" />
</template>
