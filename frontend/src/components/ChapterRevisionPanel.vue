<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NEmpty, NModal, NSpin, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import { countCharacters } from '../utils/chapter'
import { revisionSourceLabel } from '../types'
import type { Chapter, ChapterRevision, ChapterRevisionMeta } from '../types'

const props = defineProps<{
  show: boolean
  novelId: string
  chapterId: string | null
  chapterTitle: string
  currentContent: string
  currentStatus: string
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'restored', chapter: Chapter): void
}>()

const message = useMessage()

const revisions = ref<ChapterRevisionMeta[]>([])
const detail = ref<ChapterRevision | null>(null)
const listLoading = ref(false)
const detailLoading = ref(false)
const restoring = ref(false)
const showRestoreConfirm = ref(false)
let epoch = 0
let disposed = false
let listRequest = 0
let detailRequest = 0
let restoreRequest = 0
interface RevisionScope { novelId: string; chapterId: string | null; epoch: number }
const restoreContext = ref<(RevisionScope & { revisionId: string }) | null>(null)

function captureScope(): RevisionScope {
  return { novelId: props.novelId, chapterId: props.chapterId, epoch }
}

function isCurrent(scope: RevisionScope): boolean {
  return !disposed && props.show && scope.epoch === epoch
    && scope.novelId === props.novelId && scope.chapterId === props.chapterId
}

function invalidate() {
  epoch += 1
  listRequest += 1
  detailRequest += 1
  restoreRequest += 1
  revisions.value = []
  detail.value = null
  restoreContext.value = null
  showRestoreConfirm.value = false
  listLoading.value = false
  detailLoading.value = false
  restoring.value = false
}

const currentWords = computed(() => countCharacters(props.currentContent))

function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** 相对当前正文的字数变化：正数表示历史版本更长 */
function deltaOf(revision: ChapterRevisionMeta): number {
  return revision.word_count - currentWords.value
}

function deltaLabel(revision: ChapterRevisionMeta): string {
  const delta = deltaOf(revision)
  if (delta === 0) return '与当前相同'
  return delta > 0 ? `比当前多 ${delta} 字` : `比当前少 ${-delta} 字`
}

async function loadRevisions(reportError = true): Promise<boolean> {
  if (!props.show || !props.novelId || !props.chapterId || disposed) {
    revisions.value = []
    return false
  }
  const scope = captureScope()
  const request = ++listRequest
  const current = () => isCurrent(scope) && request === listRequest
  listLoading.value = true
  try {
    const result = await api.listChapterRevisions(scope.novelId, scope.chapterId!)
    if (!current()) return false
    revisions.value = result
    return true
  } catch (e) {
    if (current() && reportError) message.error((e as Error).message)
    return false
  } finally {
    if (current()) listLoading.value = false
  }
}

async function openDetail(revision: ChapterRevisionMeta) {
  if (!props.show || !props.chapterId || disposed || restoring.value
    || !revisions.value.some((item) => item.id === revision.id)) return
  const scope = captureScope()
  const request = ++detailRequest
  const current = () => isCurrent(scope) && request === detailRequest
  detail.value = null
  showRestoreConfirm.value = false
  restoreContext.value = null
  detailLoading.value = true
  try {
    const result = await api.getChapterRevision(scope.novelId, scope.chapterId!, revision.id)
    if (!current()) return
    if (result.id !== revision.id) { message.warning('历史版本响应不一致，请重新读取'); return }
    detail.value = result
  } catch (e) {
    if (current()) message.error((e as Error).message)
  } finally {
    if (current()) detailLoading.value = false
  }
}

function close() {
  if (restoring.value) return
  invalidate()
  emit('update:show', false)
}

function askRestore() {
  if (!props.show || !detail.value || detailLoading.value || restoring.value) return
  restoreContext.value = { ...captureScope(), revisionId: detail.value.id }
  showRestoreConfirm.value = true
}

async function confirmRestore() {
  const scope = restoreContext.value
  if (!scope?.chapterId || !detail.value || restoring.value || !showRestoreConfirm.value
    || !isCurrent(scope) || scope.revisionId !== detail.value.id) return
  const request = ++restoreRequest
  const current = () => isCurrent(scope) && request === restoreRequest
  restoring.value = true
  try {
    const chapter = await api.restoreChapterRevision(
      scope.novelId,
      scope.chapterId,
      scope.revisionId,
    )
    if (!current()) return
    if (chapter.novel_id !== scope.novelId || chapter.id !== scope.chapterId) {
      message.warning('恢复响应与原章节不一致，请刷新核对保存结果')
      return
    }
    showRestoreConfirm.value = false
    restoreContext.value = null
    detailRequest += 1
    detail.value = null
    // 正文已经恢复，先通知原编辑器；后置目录故障不能把成功写入误报成失败。
    emit('restored', chapter)
    const refreshed = await loadRevisions(false)
    if (!current()) return
    if (refreshed) message.success('已恢复到所选版本，章节已回到草稿')
    else message.warning('版本已恢复，章节已回到草稿；历史目录刷新失败，请重新打开核对')
  } catch (e) {
    if (current()) message.error((e as Error).message)
  } finally {
    if (current()) restoring.value = false
  }
}

watch(
  () => [props.show, props.novelId, props.chapterId] as const,
  ([show]) => {
    invalidate()
    if (!show) return
    // 每次打开都重新读取，避免关闭期间保存的新版本没有出现在列表中。
    void loadRevisions()
  },
  { immediate: true, flush: 'sync' },
)
onBeforeUnmount(() => { disposed = true; invalidate() })
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="版本历史"
    style="width: 1040px; max-width: 94vw"
    :closable="!restoring"
    :mask-closable="!restoring"
    :close-on-esc="!restoring"
    @update:show="close"
  >
    <div class="revision-head">
      <div class="current-version">
        <span class="label">当前版本</span>
        <span class="current-title">{{ chapterTitle || '未命名章节' }}</span>
        <n-tag size="tiny" :type="currentStatus === 'finalized' ? 'success' : 'default'">
          {{ currentStatus === 'finalized' ? '已定稿' : '草稿' }}
        </n-tag>
        <span class="current-words">{{ currentWords }} 字</span>
      </div>
      <div class="head-hint">历史版本按版本号从新到旧排列，版本记录不可编辑。</div>
    </div>

    <div class="revision-body">
      <div class="history-col">
        <div class="col-title">历史版本（{{ revisions.length }}）</div>
        <n-spin :show="listLoading">
          <n-empty v-if="!revisions.length" description="暂无历史版本，覆盖正文前会自动保存" />
          <ul v-else class="history-list">
            <li
              v-for="revision in revisions"
              :key="revision.id"
              class="history-item"
              :class="{ active: detail?.id === revision.id }"
              @click="openDetail(revision)"
            >
              <div class="history-line">
                <span class="revision-no">v{{ revision.revision_no }}</span>
                <n-tag size="tiny" type="info">{{ revisionSourceLabel(revision.source) }}</n-tag>
                <span class="history-time">{{ formatTime(revision.created_at) }}</span>
              </div>
              <div class="history-line secondary">
                <span>{{ revision.word_count }} 字</span>
                <span :class="{ shrink: deltaOf(revision) < 0 }">{{ deltaLabel(revision) }}</span>
              </div>
              <div v-if="revision.title" class="history-title">{{ revision.title }}</div>
            </li>
          </ul>
        </n-spin>
      </div>

      <div class="compare-col">
        <div class="col-title">
          内容对比
          <span v-if="detail" class="compare-hint">左为历史 v{{ detail.revision_no }}，右为当前正文</span>
        </div>
        <div v-if="!detail" class="compare-empty">
          <n-empty description="点击左侧历史版本查看该版本正文" />
        </div>
        <div v-else class="compare-cols">
          <div class="compare-col-item">
            <div class="col-subtitle">历史 v{{ detail.revision_no }} · {{ detail.word_count }} 字</div>
            <div class="col-meta">
              来源：{{ revisionSourceLabel(detail.source) }} · {{ formatTime(detail.created_at) }}
            </div>
            <div class="col-meta">当时状态：{{ detail.status === 'finalized' ? '已定稿' : '草稿' }}</div>
            <div v-if="detail.directive" class="col-directive">章前指令：{{ detail.directive }}</div>
            <div class="content-box" :class="{ loading: detailLoading }">{{ detail.content }}</div>
          </div>
          <div class="compare-col-item">
            <div class="col-subtitle">当前正文 · {{ currentWords }} 字</div>
            <div class="col-meta">当前已保存的正文</div>
            <div v-if="detail.directive" class="col-directive placeholder">章前指令以编辑区为准</div>
            <div class="content-box">{{ currentContent }}</div>
          </div>
        </div>
        <div v-if="detail" class="restore-row">
          <n-button type="warning" :loading="restoring" @click="askRestore">恢复此版本</n-button>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="modal-actions">
        <n-button :disabled="restoring" @click="close">关闭</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showRestoreConfirm" :closable="!restoring" :mask-closable="!restoring" :close-on-esc="!restoring" preset="card" title="确认恢复此版本" style="width: 520px">
    <p v-if="detail">
      将把本章恢复到 v{{ detail.revision_no }}（{{ revisionSourceLabel(detail.source) }} ·
      {{ detail.word_count }} 字），当前正文会先另存为一条新版本，可再次恢复回来。
    </p>
    <ul class="restore-warnings">
      <li>恢复后章节回到<b>草稿</b>，需要重新执行「定稿沉淀」。</li>
      <li>摘要、张力分、漂移分与本章旧记忆会被清空，需要重新生成。</li>
      <li>不会回滚已经确认进入 Story Bible 的设定、伏笔、时间线、对话或大纲。</li>
    </ul>
    <template #footer>
      <div class="modal-actions">
        <n-button :disabled="restoring" @click="showRestoreConfirm = false">取消</n-button>
        <n-button type="warning" :loading="restoring" @click="confirmRestore">确认恢复</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.revision-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 10px;
  border-bottom: 1px solid #e5e5e5;
  margin-bottom: 12px;
}
.current-version {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.current-version .label {
  color: #888;
  font-weight: 400;
  font-size: 12px;
}
.current-words {
  color: #666;
  font-weight: 400;
}
.head-hint {
  color: #999;
  font-size: 12px;
}
.revision-body {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.history-col {
  width: 280px;
  flex: none;
  max-height: 460px;
  overflow: auto;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 8px;
}
.col-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.compare-hint {
  color: #999;
  font-weight: 400;
  font-size: 12px;
  margin-left: 8px;
}
.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.history-item {
  border: 1px solid #eee;
  border-radius: 4px;
  padding: 8px;
  margin-bottom: 8px;
  cursor: pointer;
}
.history-item:hover {
  border-color: #b8d8ff;
}
.history-item.active {
  border-color: #2080f0;
  background: #f0f7ff;
}
.history-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.history-line.secondary {
  color: #666;
  font-size: 12px;
  margin-top: 4px;
}
.history-line.secondary .shrink {
  color: #d03050;
}
.revision-no {
  font-weight: 600;
}
.history-time {
  color: #999;
  font-size: 12px;
}
.history-title {
  color: #666;
  font-size: 12px;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.compare-col {
  flex: 1;
  min-width: 0;
}
.compare-empty {
  border: 1px dashed #e5e5e5;
  border-radius: 4px;
  padding: 40px;
}
.compare-cols {
  display: flex;
  gap: 12px;
}
.compare-col-item {
  flex: 1;
  min-width: 0;
}
.col-subtitle {
  font-weight: 600;
  margin-bottom: 4px;
}
.col-meta {
  color: #999;
  font-size: 12px;
  margin-bottom: 2px;
}
.col-directive {
  color: #666;
  font-size: 12px;
  margin: 6px 0;
  padding: 6px;
  background: #fafafa;
  border-radius: 4px;
}
.col-directive.placeholder {
  color: #bbb;
}
.content-box {
  white-space: pre-wrap;
  line-height: 1.7;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 8px;
  max-height: 320px;
  overflow: auto;
  margin-top: 6px;
}
.content-box.loading {
  opacity: 0.5;
}
.restore-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.restore-warnings {
  margin: 8px 0 0;
  padding-left: 20px;
  color: #666;
  line-height: 1.8;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
