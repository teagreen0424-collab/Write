<script lang="ts">
import { ref as sharedRef } from 'vue'

interface Completion { version: number; authorJson: string | null }
// 工作台切换面板会卸载组件；页面内的在途写入与核对事实必须继续由下一实例看见。
const foreshadowingWrites = {
  locks: sharedRef(new Map<string, number>()),
  completions: sharedRef(new Map<string, Completion>()),
  latest: new Map<string, number>(),
  sequence: 0,
}
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NEmpty, NSelect, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import { chapterLabel } from '../utils/chapter'
import type { ChapterMetadata, EntityRecord, ForeshadowingTrack, ForeshadowingTracking } from '../types'

const props = defineProps<{ novelId: string }>()

const message = useMessage()

const chapters = ref<ChapterMetadata[]>([])
const tracking = ref<ForeshadowingTracking | null>(null)
const currentChapterId = ref<string | null>(null)
const loading = ref(false)
const writeLocks = foreshadowingWrites.locks
const pendingDerivedIds = ref(new Set<string>())
const uncertainIds = ref(new Set<string>())
const SAVED_FIELDS = ['content', 'status', 'related_characters', 'planted_chapter_id', 'consumed_chapter_id'] as const
const completedWrites = foreshadowingWrites.completions
const observedCompletions = new Map<string, number>()
interface Scope { novelId: string; epoch: number }
let scopeEpoch = 0
let readSequence = 0
const latestOperations = foreshadowingWrites.latest
let selectionRevision = 0
let alive = true
let loadedScope: Scope | null = null

const captureScope = (): Scope => ({ novelId: props.novelId, epoch: scopeEpoch })
const isCurrent = (scope: Scope) => alive && scope.epoch === scopeEpoch && scope.novelId === props.novelId
const itemKey = (novelId: string, itemId: string) => JSON.stringify([novelId, itemId])
const itemSaving = (item: ForeshadowingTrack) => writeLocks.value.has(itemKey(props.novelId, item.id))
const authorJson = (value: ForeshadowingTrack | EntityRecord) => JSON.stringify(SAVED_FIELDS.map((field) => value[field]))
const itemNeedsRefresh = (item: ForeshadowingTrack) => {
  const key = itemKey(props.novelId, item.id), completed = completedWrites.value.get(key)
  return uncertainIds.value.has(item.id) || !!completed && (observedCompletions.get(key) ?? 0) < completed.version
    && (completed.authorJson === null || completed.authorJson !== authorJson(item))
}

function recordCompletion(key: string, updated: EntityRecord | null): Completion {
  const completed = { version: (completedWrites.value.get(key)?.version ?? 0) + 1,
    authorJson: updated ? authorJson(updated) : null }
  completedWrites.value.set(key, completed)
  return completed
}

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

async function readAll(): Promise<boolean> {
  const scope = captureScope()
  if (!alive || !scope.novelId) return false
  const request = ++readSequence, selection = selectionRevision
  // 读取起点决定它已覆盖哪些完成事实；迟到的提交前快照不能解除写门禁。
  const completionAtStart = new Map([...completedWrites.value].map(([key, value]) => [key, value.version]))
  const current = () => isCurrent(scope) && request === readSequence
  loading.value = true
  try {
    const [metadata, result] = await Promise.all([
      api.listChapterMetadata(scope.novelId), api.getForeshadowings(scope.novelId),
    ])
    if (!current()) return false
    if (!Array.isArray(metadata) || metadata.some((chapter) => !chapter || chapter.novel_id !== scope.novelId || typeof chapter.id !== 'string' || !chapter.id)
      || !result || GROUPS.some((group) => !Array.isArray(result.groups?.[group.key])
        || result.groups[group.key].some((item) => !item || typeof item.id !== 'string' || !item.id || item.status !== group.key))) {
      throw new Error('伏笔或章节读取结果与当前作品不一致，请刷新核对')
    }
    chapters.value = metadata
    tracking.value = result
    loadedScope = scope
    observedCompletions.clear()
    for (const group of GROUPS) for (const item of result.groups[group.key]) {
      const key = itemKey(scope.novelId, item.id)
      observedCompletions.set(key, completionAtStart.get(key) ?? 0)
    }
    pendingDerivedIds.value.clear()
    uncertainIds.value.clear()
    const validSelection = metadata.some((chapter) => chapter.id === currentChapterId.value)
    if (selection === selectionRevision && !validSelection) currentChapterId.value = metadata[metadata.length - 1]?.id ?? null
    else if (currentChapterId.value && !validSelection) currentChapterId.value = null
    return true
  } catch (e) {
    if (!current()) return false
    throw e
  } finally {
    if (current()) loading.value = false
  }
}

async function load() {
  const scope = captureScope()
  try { await readAll() }
  catch (e) { if (isCurrent(scope)) message.error((e as Error).message) }
}

function currentItem(item: ForeshadowingTrack): boolean {
  return !!props.novelId && !!loadedScope && isCurrent(loadedScope)
    && GROUPS.some((group) => groupItems(group.key).some((row) => row === item))
}

function validChapter(chapterId: string): boolean {
  return chapters.value.some((chapter) => chapter.id === chapterId && chapter.novel_id === props.novelId)
}

function savedEntity(value: EntityRecord, scope: Scope, itemId: string): boolean {
  return !!value && value.id === itemId && value.novel_id === scope.novelId
    && typeof value.content === 'string' && ['planted', 'suspended', 'consumed'].includes(value.status as string)
    && Array.isArray(value.related_characters) && typeof value.planted_chapter_id === 'string' && typeof value.consumed_chapter_id === 'string'
}

function projectSaved(value: EntityRecord) {
  if (!tracking.value) return
  const item: ForeshadowingTrack = { id: value.id as string, content: value.content as string,
    status: value.status as ForeshadowingTrack['status'], related_characters: value.related_characters as unknown[],
    planted_chapter_id: value.planted_chapter_id as string, consumed_chapter_id: value.consumed_chapter_id as string,
    planted_seq: null, suspended_chapters: null, long_pending: false }
  for (const group of GROUPS) tracking.value.groups[group.key] = groupItems(group.key).filter((row) => row.id !== item.id)
  tracking.value.groups[item.status].push(item)
  pendingDerivedIds.value.add(item.id)
  uncertainIds.value.delete(item.id)
}

async function saveItem(item: ForeshadowingTrack, payload: Record<string, unknown>, successText: string) {
  if (!currentItem(item) || itemSaving(item) || itemNeedsRefresh(item)) return
  const scope = captureScope(), key = itemKey(scope.novelId, item.id), operation = ++foreshadowingWrites.sequence
  const itemId = item.id, submitted = { ...payload }
  // 写锁跨作品往返保留到自身操作完成，不能只忽略旧UI回应却放行乱序写库。
  writeLocks.value.set(key, operation)
  latestOperations.set(key, operation)
  const current = () => isCurrent(scope) && latestOperations.get(key) === operation
  let completed: Completion | null = null
  try {
    const updated = await api.updateEntity(scope.novelId, 'foreshadowings', itemId, submitted)
    const valid = savedEntity(updated, scope, itemId)
    // 先记录请求已完成/可能已提交，再判断UI来源；作品往返不能复用提交前的旧快照。
    completed = recordCompletion(key, valid ? updated : null)
    if (!current()) return
    readSequence += 1
    loading.value = false
    if (!valid) {
      uncertainIds.value.add(itemId)
      message.warning('未收到完整的伏笔保存确认，请刷新核对结果；本次不会重复提交。')
      return
    }
    projectSaved(updated)
    observedCompletions.set(key, completed.version)
    message.success(successText)
    // API 已确认且本地采用后即可接受作者下一次关联操作；迟到刷新仍由 readSequence 隔离。
    if (writeLocks.value.get(key) === operation) writeLocks.value.delete(key)
    try { await readAll() }
    catch (e) { if (current()) message.warning(`已保存，刷新未完成：${(e as Error).message}；已确认的伏笔状态保留，悬置统计待刷新。`) }
  } catch (e) {
    const failure = e as Error & { status?: number }
    const unknown = !failure.status || failure.status >= 500 || failure.status === 408 || failure.status === 429
    if (unknown && !completed) completed = recordCompletion(key, null)
    if (!current()) return
    if (unknown) {
      readSequence += 1
      loading.value = false
      uncertainIds.value.add(itemId)
      message.warning(`伏笔保存结果尚未核对：${failure.message}；请先刷新确认，再继续操作。`)
    } else message.error(failure.message)
  } finally {
    if (writeLocks.value.get(key) === operation) writeLocks.value.delete(key)
  }
}

async function setPlanted(item: ForeshadowingTrack, chapterId: string | null) {
  if (!currentItem(item) || itemSaving(item) || itemNeedsRefresh(item)) return
  if (chapterId && !validChapter(chapterId)) { message.warning('埋设章节不属于当前作品目录，请刷新后重新选择'); return }
  if ((chapterId ?? '') === item.planted_chapter_id) return
  await saveItem(item, { planted_chapter_id: chapterId || '' }, '已保存埋设章节')
}

async function markConsumed(item: ForeshadowingTrack) {
  if (!currentItem(item) || itemSaving(item) || itemNeedsRefresh(item) || item.status === 'consumed') return
  const chapterId = currentChapterId.value
  if (!chapterId) { message.error('请先选择顶部「当前章节」'); return }
  if (!validChapter(chapterId)) { message.warning('当前章节不属于本作品目录，请刷新后重新选择'); return }
  await saveItem(item, { status: 'consumed', consumed_chapter_id: chapterId }, '已保存消费状态')
}

function plantedText(item: ForeshadowingTrack): string {
  if (pendingDerivedIds.value.has(item.id) && item.planted_chapter_id) return '埋设：已指定，序号待刷新'
  return item.planted_seq == null ? '埋设：未指定' : `埋设：第${item.planted_seq}章`
}

function suspendedText(item: ForeshadowingTrack): string {
  if (pendingDerivedIds.value.has(item.id)) return '悬置：待刷新'
  return item.suspended_chapters == null ? '悬置：—' : `悬置：${item.suspended_chapters} 章`
}

watch(currentChapterId, () => { selectionRevision += 1 }, { flush: 'sync' })

watch(
  () => props.novelId,
  () => {
    scopeEpoch += 1
    readSequence += 1
    loadedScope = null
    observedCompletions.clear()
    chapters.value = []
    tracking.value = null
    currentChapterId.value = null
    pendingDerivedIds.value.clear()
    uncertainIds.value.clear()
    loading.value = false
    if (props.novelId) load()
  },
  { immediate: true, flush: 'sync' },
)

onBeforeUnmount(() => { alive = false; scopeEpoch += 1; readSequence += 1 })
</script>

<template>
  <section class="foreshadowing-tracking" aria-label="伏笔追踪">
    <div class="topbar">
      <div class="current-chapter">
        <span class="label">当前章节</span>
        <n-select
          v-model:value="currentChapterId"
          :options="chapterOptions"
          :disabled="!novelId || !chapters.length"
          placeholder="选择章节（用于「标记已消费」）"
          style="width: 260px"
          clearable
        />
      </div>
      <n-button size="small" :disabled="!novelId" :loading="loading" @click="load">刷新</n-button>
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
              <span v-if="pendingDerivedIds.has(item.id)">章节关联已保存，悬置统计待刷新</span>
              <span v-if="itemNeedsRefresh(item)">保存结果待核对，请先刷新</span>
            </div>
            <div class="item-actions">
              <n-select
                :value="item.planted_chapter_id || null"
                :options="chapterOptions"
                :disabled="itemSaving(item) || itemNeedsRefresh(item)"
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
                :disabled="itemSaving(item) || itemNeedsRefresh(item)"
                :loading="itemSaving(item)"
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
  </section>
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
