<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NDropdown, NEmpty } from 'naive-ui'
import type { Conversation, Novel } from '../types'

const props = defineProps<{
  novels: Novel[]
  conversations: Conversation[]
  selectedId: string | null
}>()

const emit = defineEmits<{
  (e: 'new-global'): void
  (e: 'new-novel-conversation', novelId: string): void
  (e: 'select', id: string): void
  (e: 'rename', c: Conversation): void
  (e: 'delete', c: Conversation): void
  (e: 'promote', c: Conversation): void
  (e: 'move', c: Conversation): void
  (e: 'enter-workbench', novelId: string): void
  (e: 'create-novel'): void
}>()

const expanded = ref<Set<string>>(new Set())

function globalConversations() {
  return props.conversations.filter((c) => !c.novel_id)
}
function novelConversations(novelId: string) {
  return props.conversations.filter((c) => c.novel_id === novelId)
}
function toggle(novelId: string) {
  const next = new Set(expanded.value)
  if (next.has(novelId)) next.delete(novelId)
  else next.add(novelId)
  expanded.value = next
}

function optionsFor(c: Conversation) {
  const opts: { label: string; key: string }[] = [
    { label: '重命名', key: 'rename' },
    { label: '删除', key: 'delete' },
  ]
  if (!c.novel_id) {
    opts.push({ label: '转为作品', key: 'promote' })
    opts.push({ label: '移动到作品', key: 'move' })
  }
  return opts
}
function onAction(key: string, c: Conversation) {
  if (key === 'rename') emit('rename', c)
  else if (key === 'delete') emit('delete', c)
  else if (key === 'promote') emit('promote', c)
  else if (key === 'move') emit('move', c)
}
</script>

<template>
  <div class="sidebar">
    <div class="section">
      <div class="section-head">
        <span class="section-title">全局对话</span>
        <n-button size="tiny" @click="emit('new-global')">+ 新对话</n-button>
      </div>
      <div class="list">
        <div
          v-for="c in globalConversations()"
          :key="c.id"
          class="conv-item"
          :class="{ active: c.id === selectedId }"
          @click="emit('select', c.id)"
        >
          <span class="conv-title">{{ c.title || '新对话' }}</span>
          <span class="menu" @click.stop>
            <n-dropdown trigger="click" :options="optionsFor(c)" @select="(k: string) => onAction(k, c)">
              <n-button size="tiny" quaternary>…</n-button>
            </n-dropdown>
          </span>
        </div>
        <n-empty v-if="!globalConversations().length" size="small" description="暂无全局会话" />
      </div>
    </div>

    <div class="section grow">
      <div class="section-head">
        <span class="section-title">作品</span>
      </div>
      <div class="novel-list">
        <div v-for="n in novels" :key="n.id" class="novel-group">
          <div class="novel-head">
            <span class="novel-title" @click="toggle(n.id)">
              {{ expanded.has(n.id) ? '▾' : '▸' }} {{ n.title }}
            </span>
            <div class="novel-actions">
              <n-button size="tiny" quaternary title="新会话" @click="emit('new-novel-conversation', n.id)">
                +
              </n-button>
              <n-button size="tiny" quaternary title="进入工作台" @click="emit('enter-workbench', n.id)">
                工作台
              </n-button>
            </div>
          </div>
          <div v-if="expanded.has(n.id)" class="conv-sublist">
            <div
              v-for="c in novelConversations(n.id)"
              :key="c.id"
              class="conv-item"
              :class="{ active: c.id === selectedId }"
              @click="emit('select', c.id)"
            >
              <span class="conv-title">{{ c.title || '新对话' }}</span>
              <span class="menu" @click.stop>
                <n-dropdown trigger="click" :options="optionsFor(c)" @select="(k: string) => onAction(k, c)">
                  <n-button size="tiny" quaternary>…</n-button>
                </n-dropdown>
              </span>
            </div>
            <div v-if="!novelConversations(n.id).length" class="hint">暂无会话</div>
          </div>
        </div>
        <n-empty v-if="!novels.length" size="small" description="暂无作品" />
      </div>
    </div>

    <div class="create-novel">
      <n-button size="tiny" quaternary block @click="emit('create-novel')">+ 新建作品</n-button>
    </div>
  </div>
</template>

<style scoped>
.sidebar {
  width: 280px;
  flex-shrink: 0;
  border-right: 1px solid #e5e5e5;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.section {
  padding: 12px;
  border-bottom: 1px solid #eee;
}
.section.grow {
  flex: 1;
  overflow-y: auto;
  border-bottom: none;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.section-title {
  font-weight: 600;
  color: #555;
}
.list,
.novel-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.conv-item:hover {
  background: #f3f4f6;
}
.conv-item.active {
  background: #e0e7ff;
}
.conv-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}
.menu {
  flex-shrink: 0;
  opacity: 0;
}
.conv-item:hover .menu {
  opacity: 1;
}
.novel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 4px;
}
.novel-title {
  flex: 1;
  cursor: pointer;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.novel-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.conv-sublist {
  padding-left: 14px;
}
.hint {
  color: #bbb;
  font-size: 12px;
  padding: 4px 8px;
}
.create-novel {
  padding: 10px 12px;
  border-top: 1px solid #eee;
}
</style>
