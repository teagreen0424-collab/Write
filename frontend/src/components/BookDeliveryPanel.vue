<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NCheckbox, NModal, NSelect, NSwitch, useMessage } from 'naive-ui'
import { api } from '../api'
import { createAsyncScope, type AsyncScopeToken } from '../utils/asyncScope'
import type {
  BookSummary, DeliveryFormat, DeliveryPreflight, DeliveryRequest, DeliveryScope,
} from '../types'

const props = defineProps<{ novelId: string }>()
const message = useMessage()
const summary = ref<BookSummary | null>(null)
const loading = ref(false)
const checking = ref(false)
const downloading = ref(false)
const scope = ref<DeliveryScope>('all')
const format = ref<DeliveryFormat>('docx')
const volumeId = ref('')
const chapterIds = ref<string[]>([])
const includeDrafts = ref(false)
const report = ref<DeliveryPreflight | null>(null)
const showConfirm = ref(false)
const asyncScope = createAsyncScope(() => props.novelId)
let summarySequence = 0
let requestRevision = 0
let checkSequence = 0
let downloadSequence = 0
let reportScope: AsyncScopeToken | null = null
let reportRequest = ''

const request = computed<DeliveryRequest>(() => ({
  format: format.value,
  scope: scope.value,
  volume_id: volumeId.value,
  chapter_ids: chapterIds.value,
  include_drafts: includeDrafts.value,
}))
const volumeOptions = computed(() =>
  (summary.value?.volumes ?? []).map((v) => ({ label: v.title, value: v.id })),
)
const formatLabel = computed(() => ({ docx: 'DOCX', markdown: 'Markdown', txt: 'TXT' })[format.value])
const requestFingerprint = computed(() => JSON.stringify(request.value))

function clearReport() {
  requestRevision += 1
  report.value = null
  reportScope = null
  reportRequest = ''
  showConfirm.value = false
  checking.value = false
  downloading.value = false
}

function currentReport(): boolean {
  return !!reportScope && asyncScope.isCurrent(reportScope) && reportRequest === requestFingerprint.value
}

watch(requestFingerprint, clearReport, { flush: 'sync' })
watch(() => props.novelId, async (novelId) => {
  asyncScope.invalidate()
  clearReport()
  const scopeToken = asyncScope.capture()
  const sequence = ++summarySequence
  summary.value = null
  loading.value = false
  scope.value = 'all'
  volumeId.value = ''
  chapterIds.value = []
  includeDrafts.value = false
  if (!novelId) return
  loading.value = true
  try {
    const result = await api.getBookSummary(novelId)
    if (asyncScope.isCurrent(scopeToken) && sequence === summarySequence) summary.value = result
  } catch (e) {
    if (asyncScope.isCurrent(scopeToken) && sequence === summarySequence) message.error((e as Error).message)
  } finally {
    if (asyncScope.isCurrent(scopeToken) && sequence === summarySequence) loading.value = false
  }
}, { immediate: true, flush: 'sync' })

function toggleChapter(id: string, checked: boolean) {
  chapterIds.value = checked
    ? [...chapterIds.value, id]
    : chapterIds.value.filter((item) => item !== id)
}

function validateSelection(): boolean {
  if (scope.value === 'volume' && !volumeId.value) {
    message.warning('请先选择要导出的卷')
    return false
  }
  if (scope.value === 'chapters' && chapterIds.value.length === 0) {
    message.warning('请至少选择一个章节')
    return false
  }
  return true
}

async function runPreflight() {
  if (!props.novelId || checking.value || !validateSelection()) return
  const scopeToken = asyncScope.capture()
  const revision = requestRevision
  const sequence = ++checkSequence
  const fingerprint = requestFingerprint.value
  const submitted: DeliveryRequest = JSON.parse(fingerprint)
  const isCurrent = () => asyncScope.isCurrent(scopeToken) && revision === requestRevision && sequence === checkSequence
  checking.value = true
  report.value = null
  reportScope = null
  showConfirm.value = false
  try {
    const result = await api.preflightExport(scopeToken.identity, submitted)
    if (!isCurrent()) return
    report.value = result
    reportScope = scopeToken
    reportRequest = fingerprint
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  } finally {
    if (isCurrent()) checking.value = false
  }
}

async function confirmDownload() {
  if (!summary.value || !report.value?.chapter_count || !currentReport() || downloading.value) return
  const scopeToken = asyncScope.capture()
  const revision = requestRevision
  const sequence = ++downloadSequence
  const submitted: DeliveryRequest = JSON.parse(requestFingerprint.value)
  const filename = `${summary.value.title}.${submitted.format === 'markdown' ? 'md' : submitted.format}`
  const isCurrent = () => asyncScope.isCurrent(scopeToken) && revision === requestRevision && sequence === downloadSequence
  downloading.value = true
  try {
    const blob = await api.downloadBookFile(scopeToken.identity, submitted)
    if (!isCurrent()) return
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    showConfirm.value = false
    message.success('文件已开始下载')
  } catch (e) {
    if (isCurrent()) message.error((e as Error).message)
  } finally {
    if (isCurrent()) downloading.value = false
  }
}
onBeforeUnmount(() => asyncScope.dispose())
</script>

<template>
  <div class="delivery">
    <p v-if="loading">正在读取作品目录…</p>
    <p v-else-if="!summary">请选择作品后查看成品交付。</p>
    <template v-else>
      <div class="stats">
        <div><strong>{{ summary.chapter_count }}</strong><span>总章数</span></div>
        <div><strong>{{ summary.word_count.toLocaleString('zh-CN') }}</strong><span>总字数</span></div>
        <div><strong>{{ summary.draft_count }}</strong><span>草稿</span></div>
        <div><strong>{{ summary.finalized_count }}</strong><span>已定稿</span></div>
      </div>

      <section class="section">
        <h2>全书目录</h2>
        <div class="table-scroll">
          <table>
            <thead><tr><th>序号</th><th>章节</th><th>所属卷</th><th>状态</th><th>字数</th><th>张力</th><th>漂移</th></tr></thead>
            <tbody>
              <tr v-for="chapter in summary.chapters" :key="chapter.id">
                <td>{{ chapter.seq }}</td><td>{{ chapter.title }}</td>
                <td>{{ chapter.volume_title || '未归卷' }}</td>
                <td>{{ chapter.status === 'finalized' ? '已定稿' : '草稿' }}</td>
                <td>{{ chapter.word_count }}</td>
                <td>{{ chapter.tension_score ?? '—' }}</td>
                <td>{{ chapter.drift_score ?? '—' }}</td>
              </tr>
              <tr v-if="!summary.chapters.length"><td colspan="7">暂无章节</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="section controls">
        <h2>导出设置</h2>
        <div class="control-row">
          <label>范围
            <n-select v-model:value="scope" :options="[
              { label: '整本', value: 'all' },
              { label: '按卷', value: 'volume' },
              { label: '选中章节', value: 'chapters' },
            ]" style="width: 170px" />
          </label>
          <label v-if="scope === 'volume'">选择卷
            <n-select v-model:value="volumeId" :options="volumeOptions" placeholder="请选择卷" style="width: 200px" />
          </label>
          <label>格式
            <n-select v-model:value="format" :options="[
              { label: 'DOCX', value: 'docx' },
              { label: 'Markdown', value: 'markdown' },
              { label: 'TXT', value: 'txt' },
            ]" style="width: 170px" />
          </label>
          <label class="draft-toggle">包含草稿 <n-switch v-model:value="includeDrafts" /></label>
        </div>
        <div v-if="scope === 'chapters'" class="chapter-choices">
          <n-checkbox v-for="chapter in summary.chapters" :key="chapter.id"
            :checked="chapterIds.includes(chapter.id)"
            @update:checked="toggleChapter(chapter.id, $event)">
            第{{ chapter.seq }}章 {{ chapter.title }}（{{ chapter.status === 'draft' ? '草稿' : '已定稿' }}）
          </n-checkbox>
        </div>
        <p class="hint">默认仅导出已定稿章节。预检查只运行规则一致性，不调用语义深度检查。</p>
        <n-button type="primary" :loading="checking" @click="runPreflight">导出前检查</n-button>
      </section>

      <section v-if="report" class="section report">
        <h2>检查结果</h2>
        <p>将导出 {{ report.chapter_count }} 章、{{ report.word_count.toLocaleString('zh-CN') }} 字。
          {{ report.ready ? '未发现草稿或规则冲突。' : '请查看下方提醒。' }}</p>
        <p v-if="!report.chapter_count" class="warning">当前范围没有可导出的章节，请调整范围或开启“包含草稿”。</p>
        <div v-if="report.draft_chapters.length" class="finding">
          <strong>范围内草稿（{{ report.draft_chapters.length }}）</strong>
          <p>{{ includeDrafts ? '已选择包含草稿，请确认这些章节适合交付。' : '默认不会导出；如需包含，请开启“包含草稿”并重新检查。' }}</p>
          <ul><li v-for="item in report.draft_chapters" :key="item.id">第{{ item.seq }}章 {{ item.title }}</li></ul>
        </div>
        <div v-if="report.unbound_chapters.length" class="finding">
          <strong>未归卷章节（{{ report.unbound_chapters.length }}）</strong>
          <p>整本导出会包含它们，按卷导出不会包含。</p>
          <ul><li v-for="item in report.unbound_chapters" :key="item.id">第{{ item.seq }}章 {{ item.title }}</li></ul>
        </div>
        <div v-if="report.unresolved_foreshadowings.length" class="finding">
          <strong>未闭合伏笔（{{ report.unresolved_foreshadowings.length }}）</strong>
          <p>仅供提醒，不阻止导出。</p>
          <ul><li v-for="item in report.unresolved_foreshadowings" :key="item.id">{{ item.content }}</li></ul>
        </div>
        <div v-if="report.rule_conflicts.length" class="finding">
          <strong>规则冲突（{{ report.rule_conflicts.length }}）</strong>
          <ul><li v-for="(item, index) in report.rule_conflicts" :key="`${item.chapter_id}-${index}`">
            第{{ item.seq }}章 {{ item.title }}：{{ item.message }}
          </li></ul>
        </div>
        <n-button :disabled="!report.chapter_count" @click="showConfirm = true">确认并下载 {{ formatLabel }}</n-button>
      </section>
    </template>

    <n-modal v-model:show="showConfirm" preset="card" title="确认导出" style="width: min(480px, 94vw)">
      <p>将下载 {{ formatLabel }} 文件：{{ report?.chapter_count }} 章、{{ report?.word_count }} 字。</p>
      <p v-if="report && !report.ready" class="warning">检查中发现草稿或规则冲突，仍要继续导出吗？</p>
      <p v-if="report?.unresolved_foreshadowings.length">作品还有未闭合伏笔，请确认已了解。</p>
      <div class="modal-actions">
        <n-button @click="showConfirm = false">取消</n-button>
        <n-button type="primary" :loading="downloading" @click="confirmDownload">下载文件</n-button>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.delivery { height: 100%; overflow: auto; padding: 20px; box-sizing: border-box; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 12px; max-width: 950px; }
.stats > div { border: 1px solid #e3e8e6; border-radius: 8px; padding: 12px 16px; display: flex; flex-direction: column; }
.stats strong { font-size: 24px; }
.stats span, .hint { color: #666; }
.section { margin-top: 22px; }
.section h2 { font-size: 17px; margin: 0 0 12px; }
.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 750px; text-align: left; }
th, td { border-bottom: 1px solid #e9eceb; padding: 9px 12px; }
th { background: #f5f7f6; }
.control-row { display: flex; flex-wrap: wrap; gap: 16px; align-items: end; }
.control-row label { display: flex; flex-direction: column; gap: 6px; }
.control-row .draft-toggle { flex-direction: row; align-items: center; padding-bottom: 5px; }
.chapter-choices { margin-top: 12px; display: grid; gap: 8px; max-height: 220px; overflow: auto; }
.finding { border-left: 3px solid #e2ae50; padding: 5px 12px; margin: 14px 0; background: #fffbf4; }
.finding p { margin: 5px 0; }
.finding ul { margin: 6px 0; padding-left: 20px; }
.warning { color: #b35c1e; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
@media (max-width: 600px) { .stats { grid-template-columns: repeat(2, 1fr); } }
</style>
