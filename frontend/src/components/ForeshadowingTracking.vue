<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NEmpty, NSelect, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import { chapterLabel } from '../utils/chapter'
import type { Chapter, ForeshadowingTrack, ForeshadowingTracking } from '../types'

const props = defineProps<{ novelId: string }>()

const message = useMessage()

const chapters = ref<Chapter[]>([])
const tracking = ref<ForeshadowingTracking | null>(null)
const currentChapterId = ref<string | null>(null)
const loading = ref(false)

const chapterOptions = computed(() =>
  chapters.value.map((c) => ({ label: chapterLabel(c), value: c.id })),
)

// 三个分组：已悬置（需重点关注）排前
const GROUPS = [
  { key: 'suspended', label: '已悬置' },
  { key: 'planted', label: '已埋' },
  { key: 'consumed', label: '已消费' },
] as const

function groupItems(key: 'planted' | 'suspended' | 'consumed'): ForeshadowingTrack[] {
  return tracking.value?.groups?.[key] ?? []
}

const thresholdText = computed(() => {
  const t = tracking.value?.long_pending_threshold
  if (!t) return ''
  return `长期未闭合判定：悬置 ≥ ${t.chapters} 章，或 ≥ ${Math.round(t.ratio * 100)}% 已写章节（当前 ${tracking.value?.chapter_count ?? 0} 章），先到者为准`
})

async function load() {
  if (!props.novelId) return
  loading.value = true
  try {
    chapters.value = await api.listChapters(props.novelId)
    if (
      !currentChapterId.value ||
      !chapters.value.some((c) => c.id === currentChapterId.value)
    ) {
      const last = chapters.value[chapters.value.length - 1]
      currentChapterId.value = last ? last.id : null
    }
    tracking.value = await api.getForeshadowings(props.novelId)
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function setPlanted(item: ForeshadowingTrack, chapterId: string | null) {
  try {
    await api.updateEntity(props.novelId, 'foreshadowings', item.id, {
      planted_chapter_id: chapterId || '',
    })
    message.success('已更新埋设章节')
    await load()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function markConsumed(item: ForeshadowingTrack) {
  if (!currentChapterId.value) {
    message.error('请先选择顶部「当前章节」')
    return
  }
  try {
    await api.updateEntity(props.novelId, 'foreshadowings', item.id, {
      status: 'consumed',
      consumed_chapter_id: currentChapterId.value,
    })
    message.success('已标记为消费')
    await load()
  } catch (e) {
    message.error((e as Error).message)
  }
}

function plantedText(item: ForeshadowingTrack): string {
  return item.planted_seq == null ? '埋设：未指定' : `埋设：第${item.planted_seq}章`
}

function suspendedText(item: ForeshadowingTrack): string {
  return item.suspended_chapters == null ? '悬置：—' : `悬置：${item.suspended_chapters} 章`
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) load()
  },
  { immediate: true },
)
</script>

<template>
  <div class="foreshadowing-tracking">
    <div class="topbar">
      <div class="current-chapter">
        <span class="label">当前章节</span>
        <n-select
          v-model:value="currentChapterId"
          :options="chapterOptions"
          placeholder="选择章节（用于「标记已消费」）"
          style="width: 260px"
          clearable
        />
      </div>
      <n-button size="small" :loading="loading" @click="load">刷新</n-button>
    </div>
    <div v-if="thresholdText" class="threshold-hint">{{ thresholdText }}</div>

    <div v-if="tracking" class="groups">
      <div v-for="g in GROUPS" :key="g.key" class="group">
        <div class="group-header">
          <span>{{ g.label }}</span>
          <n-tag size="tiny" :type="g.key === 'suspended' ? 'warning' : 'default'">
            {{ groupItems(g.key).length }}
          </n-tag>
        </div>
        <n-empty v-if="!groupItems(g.key).length" size="small" description="暂无" />
        <div v-else class="item-list">
          <div
            v-for="item in groupItems(g.key)"
            :key="item.id"
            class="item"
            :class="{ pinned: item.long_pending }"
          >
            <div class="item-main">
              <n-tag v-if="item.long_pending" type="error" size="tiny">长期未闭合</n-tag>
              <span class="content">{{ item.content }}</span>
            </div>
            <div class="item-meta">
              <span>{{ plantedText(item) }}</span>
              <span>{{ suspendedText(item) }}</span>
            </div>
            <div class="item-actions">
              <n-select
                :value="item.planted_chapter_id || null"
                :options="chapterOptions"
                size="tiny"
                placeholder="指定埋设章节"
                style="width: 200px"
                clearable
                @update:value="(v: string | null) => setPlanted(item, v)"
              />
              <n-button
                v-if="item.status !== 'consumed'"
                size="tiny"
                type="primary"
                quaternary
                @click="markConsumed(item)"
              >
                标记已消费
              </n-button>
            </div>
          </div>
        </div>
      </div>
    </div>
    <n-empty v-else description="暂无伏笔数据" />
  </div>
</template>

<style scoped>
.foreshadowing-tracking {
  padding: 16px;
  height: 100%;
  overflow: auto;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.current-chapter {
  display: flex;
  align-items: center;
  gap: 8px;
}
.current-chapter .label {
  color: #666;
}
.threshold-hint {
  color: #888;
  font-size: 12px;
  margin-bottom: 12px;
}
.groups {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 8px;
}
.item-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.item {
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 10px 12px;
}
.item.pinned {
  border-color: #f0a0a0;
  background: #fff7f7;
}
.item-main {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.item-main .content {
  flex: 1;
  min-width: 0;
}
.item-meta {
  display: flex;
  gap: 16px;
  color: #888;
  font-size: 12px;
  margin-bottom: 8px;
}
.item-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
