<script setup lang="ts">
import { ref } from 'vue'
import ChatView from './components/ChatView.vue'
import WorkbenchView from './components/WorkbenchView.vue'

const view = ref<'chat' | 'workbench'>('chat')
const workbenchNovelId = ref<string | null>(null)

function onEnterWorkbench(novelId: string) {
  workbenchNovelId.value = novelId
  view.value = 'workbench'
}

function onBack() {
  view.value = 'chat'
}
</script>

<template>
  <ChatView v-if="view === 'chat'" @enter-workbench="onEnterWorkbench" />
  <WorkbenchView v-else :initial-novel-id="workbenchNovelId" @back="onBack" />
</template>
