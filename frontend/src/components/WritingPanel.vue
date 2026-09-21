<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NCheckbox,
  NEmpty,
  NInput,
  NList,
  NListItem,
  NModal,
  NSelect,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, streamSSE } from '../api'
import { chapterLabel, countCharacters } from '../utils/chapter'
import type {
  Chapter,
  ClicheHit,
  ClicheResult,
  Conflict,
  DeepCheckConflict,
  Segment,
  Suggestion,
} from '../types'

const props = defineProps<{ novelId: string }>()
const emit = defineEmits<{ (e: 'dirty-change', dirty: boolean): void }>()

const message = useMessage()

const chapters = ref<Chapter[]>([])
const selectedId = ref<string | null>(null)
const title = ref('')
const directive = ref('')
const content = ref('')
const contentRef = ref<HTMLTextAreaElement | null>(null)
const savedSnapshot = ref({ title: '', directive: '', content: '' })

const streaming = ref(false)
const streamText = ref('')
const conflicts = ref<Conflict[]>([])
const rewriteIntent = ref('去AI味')

// 去 AI 味扫描与语义深度检查
const clicheHits = ref<ClicheHit[]>([])
const cliche = ref<ClicheResult | null>(null)
const deepConflicts = ref<DeepCheckConflict[]>([])
const deepMeta = ref<{
  chunks_checked: number
  chunks_skipped: number
  truncated: boolean
} | null>(null)
const deepLoading = ref(false)

// 改写预览（不落库，确认后才应用）
const showPreview = ref(false)
const previewPassage = ref('')
const previewNew = ref('')
const previewFullContent = ref('')

const showSettle = ref(false)
const showDelete = ref(false)
const showWriteNext = ref(false)
const deleting = ref(false)
const nextTitle = ref('')
const nextDirective = ref('')
const settleSummary = ref('')
const suggestions = ref<Suggestion[]>([])
const checkedSuggestion = ref<boolean[]>([])

const REWRITE_INTENT_OPTIONS = [
  { label: '更细腻', value: '更细腻' },
  { label: '更爽', value: '更爽' },
  { label: '去 AI 味', value: '去AI味' },
  { label: '压缩', value: '压缩' },
]

const selectedChapter = computed(() => chapters.value.find((c) => c.id === selectedId.value) ?? null)
const isLatestChapter = computed(
  () => !!selectedId.value && chapters.value[chapters.value.length - 1]?.id === selectedId.value,
)
const chapterWords = computed(() => countCharacters(content.value))
const totalWords = computed(() =>
  chapters.value.reduce((total, chapter) => {
    const chapterContent = chapter.id === selectedId.value ? content.value : chapter.content
    return total + countCharacters(chapterContent)
  }, 0),
)
const isDirty = computed(
  () =>
    !!selectedId.value &&
    (title.value !== savedSnapshot.value.title ||
      directive.value !== savedSnapshot.value.directive ||
      content.value !== savedSnapshot.value.content),
)

function rememberSaved() {
  savedSnapshot.value = {
    title: title.value,
    directive: directive.value,
    content: content.value,
  }
}

function confirmDiscard(): boolean {
  return !isDirty.value || window.confirm('当前章节有未保存修改，确定放弃并离开吗？')
}

function requireSaved(action: string): boolean {
  if (!isDirty.value) return true
  message.warning(`请先保存当前修改，再${action}`)
  return false
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!isDirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

const segments = computed<Segment[]>(() => {
  const text = content.value
  if (!text) return [{ text, kind: 'plain' }]
  const marks: { start: number; end: number; kind: 'conflict' | 'cliche' }[] = []
  for (const c of conflicts.value) marks.push({ start: c.start, end: c.end, kind: 'conflict' })
  for (const h of clicheHits.value) marks.push({ start: h.start, end: h.end, kind: 'cliche' })
  if (!marks.length) return [{ text, kind: 'plain' }]
  marks.sort((a, b) => {
    if (a.start !== b.start) return a.start - b.start
    if (a.kind !== b.kind) return a.kind === 'conflict' ? -1 : 1
    return b.end - a.end
  })
  const out: Segment[] = []
  let pos = 0
  for (const m of marks) {
    if (m.start < pos) continue // 与上一标记重叠，冲突优先
    if (m.start > pos) out.push({ text: text.slice(pos, m.start), kind: 'plain' })
    out.push({ text: text.slice(m.start, m.end), kind: m.kind })
    pos = m.end
  }
  if (pos < text.length) out.push({ text: text.slice(pos), kind: 'plain' })
  return out
})

// 四类分布展示用（顺序固定）
const CLICHE_CATEGORIES = ['万能副词', '套路动作', '面部套话', '心理填充']

async function loadChapters() {
  if (!props.novelId) return
  chapters.value = await api.listChapters(props.novelId)
}

function clearMarks() {
  conflicts.value = []
  clicheHits.value = []
  cliche.value = null
  deepConflicts.value = []
  deepMeta.value = null
}

function selectChapter(c: Chapter, force = false) {
  if (!force && c.id !== selectedId.value && !confirmDiscard()) return
  selectedId.value = c.id
  title.value = c.title
  content.value = c.content
  directive.value = c.directive
  rememberSaved()
  clearMarks()
}

function startNew(force = false) {
  if (!force && !confirmDiscard()) return
  selectedId.value = null
  title.value = ''
  directive.value = ''
  content.value = ''
  rememberSaved()
  clearMarks()
  streamText.value = ''
}

async function reloadAndReselect() {
  await loadChapters()
  const c = chapters.value.find((x) => x.id === selectedId.value)
  if (c) {
    content.value = c.content
    title.value = c.title
    directive.value = c.directive
    rememberSaved()
    clearMarks()
  }
}

async function generateChapter(
  previousChapterId = '',
  chapterTitle = title.value,
  chapterDirective = directive.value,
) {
  if (streaming.value) return
  streaming.value = true
  streamText.value = ''
  clearMarks()
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/write`,
    {
      title: chapterTitle,
      directive: chapterDirective,
      previous_chapter_id: previousChapterId,
    },
    {
      onToken: (t) => (streamText.value += t),
      onDone: async () => {
        streaming.value = false
        await loadChapters()
        const last = chapters.value[chapters.value.length - 1]
        if (last) selectChapter(last, true)
        streamText.value = ''
        message.success('已生成新章')
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

async function doWrite() {
  await generateChapter()
}

async function doWriteNext() {
  if (!selectedId.value || !requireSaved('生成下一章')) return
  if (!isLatestChapter.value) {
    message.warning('只能从最后一章继续生成，请先选择章节列表中的最后一章')
    return
  }
  nextTitle.value = ''
  nextDirective.value = ''
  showWriteNext.value = true
}

async function confirmWriteNext() {
  if (!selectedId.value || streaming.value) return
  showWriteNext.value = false
  await generateChapter(selectedId.value, nextTitle.value, nextDirective.value)
}

async function doContinue() {
  if (!selectedId.value || streaming.value || !requireSaved('续写本章')) return
  streaming.value = true
  streamText.value = ''
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/continue`,
    { directive: directive.value },
    {
      onToken: (t) => (streamText.value += t),
      onDone: async () => {
        streaming.value = false
        await reloadAndReselect()
        streamText.value = ''
        message.success('已续写')
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

function getSelection(): string {
  const el = contentRef.value
  if (!el) return ''
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  return end > start ? content.value.slice(start, end) : ''
}

async function doRewrite() {
  if (!selectedId.value || streaming.value || !requireSaved('改写选中内容')) return
  const passage = getSelection().trim()
  if (!passage) {
    message.error('请先在正文中选中要改写的段落')
    return
  }
  streaming.value = true
  streamText.value = ''
  previewPassage.value = passage
  previewNew.value = ''
  previewFullContent.value = ''
  await streamSSE(
    `/api/v1/novels/${props.novelId}/chapters/${selectedId.value}/rewrite`,
    { passage, intent: rewriteIntent.value, preview: true },
    {
      onToken: (t) => (streamText.value += t),
      onDone: (obj) => {
        streaming.value = false
        // 流式内容即改写后的段落；done 事件带拼好的整章内容，供「应用」落库
        previewNew.value = streamText.value
        previewFullContent.value = obj.content ?? ''
        streamText.value = ''
        showPreview.value = true
      },
      onError: (m) => {
        streaming.value = false
        message.error(m)
      },
    },
  )
}

// 应用改写：仅此时才落库
async function applyPreview() {
  if (!selectedId.value) return
  try {
    await api.updateChapter(props.novelId, selectedId.value, {
      content: previewFullContent.value,
    })
    content.value = previewFullContent.value
    rememberSaved()
    showPreview.value = false
    await loadChapters()
    message.success('已应用改写')
  } catch (e) {
    message.error((e as Error).message)
  }
}

function discardPreview() {
  showPreview.value = false
  previewPassage.value = ''
  previewNew.value = ''
  previewFullContent.value = ''
  message.info('已放弃本次改写')
}

// 扫描套路表达，命中与冲突共用高亮
async function doScanCliche() {
  if (!content.value.trim()) return
  try {
    const res = await api.scanCliche(props.novelId, content.value)
    cliche.value = res
    clicheHits.value = res.hits
    if (res.total) message.warning(`命中 ${res.total} 处套路表达，密度 ${res.density}/千字`)
    else message.success('未命中套路表达')
  } catch (e) {
    message.error((e as Error).message)
  }
}

// 语义深度检查（每个有效分块一次模型调用，最多 8 次，结果不落库）
async function doDeepCheck() {
  if (!content.value.trim()) return
  deepLoading.value = true
  try {
    const res = await api.checkConsistencyDeep(props.novelId, content.value)
    deepConflicts.value = res.conflicts
    deepMeta.value = {
      chunks_checked: res.chunks_checked,
      chunks_skipped: res.chunks_skipped,
      truncated: res.truncated,
    }
    if (res.conflicts.length) message.warning(`语义层发现 ${res.conflicts.length} 处冲突`)
    else message.success('语义层未发现冲突')
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deepLoading.value = false
  }
}

// 点击深度检查结果，滚动定位到正文片段
function locateExcerpt(excerpt: string) {
  if (!excerpt) return
  const idx = content.value.indexOf(excerpt)
  const el = contentRef.value
  if (idx === -1) {
    message.warning('正文中未找到该片段')
    return
  }
  if (!el) return
  el.focus()
  el.setSelectionRange(idx, idx + excerpt.length)
  const ratio = idx / Math.max(content.value.length, 1)
  el.scrollTop = ratio * el.scrollHeight
}

async function saveContent() {
  if (!selectedId.value) return
  try {
    const updated = await api.updateChapter(props.novelId, selectedId.value, {
      title: title.value,
      content: content.value,
      directive: directive.value,
    })
    title.value = updated.title
    directive.value = updated.directive
    content.value = updated.content
    rememberSaved()
    message.success('已保存')
    await loadChapters()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function deleteChapter() {
  if (!selectedId.value || deleting.value || streaming.value) return
  const deletedIndex = chapters.value.findIndex((chapter) => chapter.id === selectedId.value)
  deleting.value = true
  try {
    await api.deleteChapter(props.novelId, selectedId.value)
    await loadChapters()
    const next = chapters.value[Math.min(deletedIndex, chapters.value.length - 1)]
    if (next) selectChapter(next, true)
    else startNew(true)
    showDelete.value = false
    message.success('章节已删除')
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deleting.value = false
  }
}

async function doConsistency() {
  if (!content.value.trim()) return
  try {
    const res = await api.checkConsistency(props.novelId, content.value)
    conflicts.value = res.conflicts
    if (res.conflicts.length) message.warning(`发现 ${res.conflicts.length} 处冲突`)
    else message.success('未发现冲突')
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function doSettle() {
  if (!selectedId.value || !requireSaved('定稿沉淀')) return
  try {
    const res = await api.settleChapter(props.novelId, selectedId.value)
    settleSummary.value = res.summary
    suggestions.value = res.suggestions
    checkedSuggestion.value = res.suggestions.map(() => true)
    showSettle.value = true
    await reloadAndReselect()
  } catch (e) {
    message.error((e as Error).message)
  }
}

async function confirmSettle() {
  if (!selectedId.value) return
  const picked = suggestions.value.filter((_, i) => checkedSuggestion.value[i])
  try {
    const res = await api.settleApply(props.novelId, selectedId.value, picked)
    message.success(`已落库 ${res.created.length} 条设定变更`)
    showSettle.value = false
  } catch (e) {
    message.error((e as Error).message)
  }
}

function suggestionLabel(s: Suggestion): string {
  const labels: Record<string, string> = {
    character: '人物',
    timeline_event: '事件',
    foreshadowing: '伏笔',
  }
  const name =
    (s.data.name as string) || (s.data.title as string) || (s.data.content as string) || ''
  return `[${labels[s.type] ?? s.type}] ${name}`
}

async function doExport(kind: 'markdown' | 'txt') {
  try {
    const text = kind === 'markdown' ? await api.exportMarkdown(props.novelId) : await api.exportTxt(props.novelId)
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = kind === 'markdown' ? 'novel.md' : 'novel.txt'
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    message.error((e as Error).message)
  }
}

watch(
  () => props.novelId,
  () => {
    if (props.novelId) {
      startNew(true)
      loadChapters()
    }
  },
  { immediate: true },
)

watch(isDirty, (dirty) => emit('dirty-change', dirty), { immediate: true })

onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', handleBeforeUnload))
</script>

<template>
  <div class="writing-panel">
    <div class="chapter-sidebar">
      <div class="pane-header">
        <div>
          <div>章节</div>
          <div class="book-stats">{{ chapters.length }} 章 · {{ totalWords }} 字</div>
        </div>
        <n-button size="small" type="primary" @click="startNew">新章</n-button>
      </div>
      <n-empty v-if="!chapters.length" description="暂无章节" />
      <n-list v-else clickable hoverable>
        <n-list-item
          v-for="c in chapters"
          :key="c.id"
          :class="{ active: c.id === selectedId }"
          @click="selectChapter(c)"
        >
          <div class="chapter-item">
            <span>{{ chapterLabel(c) }}</span>
            <n-tag size="tiny" :type="c.status === 'finalized' ? 'success' : 'default'">
              {{ c.status === 'finalized' ? '已定稿' : '草稿' }}
            </n-tag>
          </div>
        </n-list-item>
      </n-list>
    </div>

    <div class="editor">
      <div class="editor-toolbar">
        <n-input v-model:value="title" placeholder="章节标题" class="title-input" />
        <n-input v-model:value="directive" placeholder="章前指令（如：让 A 与 B 决裂）" class="directive-input" />
        <div class="toolbar-groups">
        <div class="btn-row primary-actions">
          <n-button type="primary" :loading="streaming" @click="doWrite">写新章</n-button>
          <n-button
            :disabled="!selectedId || !isLatestChapter"
            :loading="streaming"
            :title="isLatestChapter ? '从当前最后一章继续生成' : '请选择章节列表中的最后一章'"
            @click="doWriteNext"
          >
            生成下一章
          </n-button>
          <n-button :disabled="!selectedId" :loading="streaming" @click="doContinue">
            续写本章
          </n-button>
          <n-button :disabled="!selectedId || !isDirty" @click="saveContent">保存</n-button>
          <span v-if="selectedId" class="save-state" :class="{ dirty: isDirty }">
            {{ isDirty ? '未保存' : '已保存' }}
          </span>
        </div>
        <div class="btn-row edit-actions">
          <n-select
            v-model:value="rewriteIntent"
            :options="REWRITE_INTENT_OPTIONS"
            placeholder="改写意图"
            style="width: 120px"
            clearable
          />
          <n-button :disabled="!selectedId" @click="doRewrite">改写选中</n-button>
        </div>
        <div class="btn-row quality-actions">
          <n-button :disabled="!selectedId" @click="doScanCliche">套路扫描</n-button>
          <n-button :disabled="!selectedId" @click="doConsistency">一致性检查</n-button>
          <n-button :disabled="!selectedId" :loading="deepLoading" @click="doDeepCheck">
            深度检查
          </n-button>
          <span class="cost-hint">按有效分块调用模型，最多 8 次</span>
          <n-button type="success" :disabled="!selectedId" @click="doSettle">定稿沉淀</n-button>
        </div>
        <div class="btn-row file-actions">
          <n-button @click="doExport('markdown')">导出 MD</n-button>
          <n-button @click="doExport('txt')">导出 TXT</n-button>
          <n-button
            type="error"
            secondary
            :disabled="!selectedId || streaming"
            @click="showDelete = true"
          >
            删除章节
          </n-button>
        </div>
        </div>
        <div v-if="selectedId" class="next-chapter-hint">
          <template v-if="isLatestChapter">
            “生成下一章”会携带上一章章首、结尾、最近剧情和作品设定；“续写本章”会追加到当前正文。
          </template>
          <template v-else>如需生成下一章，请先选择章节列表中的最后一章。</template>
        </div>
      </div>

      <div v-if="streaming" class="stream-box">
        <div class="stream-title">生成中…</div>
        <div class="stream-text">{{ streamText }}</div>
      </div>

      <div v-else-if="selectedId" class="editor-body">
        <textarea
          ref="contentRef"
          v-model="content"
          class="content-editor"
          placeholder="在此写作，或点击上方按钮生成"
        ></textarea>
        <div class="editor-status">本章 {{ chapterWords }} 字</div>
      </div>
      <div v-else class="editor-empty">
        <n-empty description="选择章节或点击「新章」开始写作" />
      </div>

      <div v-if="conflicts.length || clicheHits.length" class="marks">
        <div class="marks-title">正文标记（冲突 / 套路命中）</div>
        <div class="preview">
          <span
            v-for="(seg, i) in segments"
            :key="i"
            :class="{ plain: seg.kind === 'plain', conflict: seg.kind === 'conflict', cliche: seg.kind === 'cliche' }"
            >{{ seg.text }}</span
          >
        </div>
      </div>

      <div v-if="cliche" class="cliche-panel">
        <div class="cliche-title">
          套路密度
          <span class="cliche-density">{{ cliche.density }} /千字</span>
          <span class="cliche-total">共 {{ cliche.total }} 处</span>
        </div>
        <div class="cliche-cats">
          <span v-for="cat in CLICHE_CATEGORIES" :key="cat" class="cliche-cat">
            <n-tag size="small" :type="(cliche.by_category[cat] ?? 0) > 0 ? 'warning' : 'default'">
              {{ cat }}
            </n-tag>
            <span class="cliche-cat-count">{{ cliche.by_category[cat] ?? 0 }}</span>
          </span>
        </div>
      </div>

      <div v-if="conflicts.length || deepMeta" class="result-cols">
        <div v-if="conflicts.length" class="result-col result-conflicts">
          <div class="result-title">规则层一致性冲突</div>
          <n-list>
            <n-list-item v-for="(c, i) in conflicts" :key="i">
              <n-tag type="error" size="small">{{ c.rule }}</n-tag>
              {{ c.message }}
            </n-list-item>
          </n-list>
        </div>

        <div v-if="deepMeta" class="result-col result-deep">
          <div class="result-title">
            语义深度检查
            <span class="deep-meta">
              已查 {{ deepMeta.chunks_checked }} 块 / 跳过 {{ deepMeta.chunks_skipped }} 块
            </span>
            <n-tag v-if="deepMeta.truncated" type="warning" size="tiny">已截断（上限 8 块）</n-tag>
          </div>
          <n-empty v-if="!deepConflicts.length" description="语义层未发现冲突" />
          <n-list v-else>
            <n-list-item v-for="(d, i) in deepConflicts" :key="i">
              <div class="deep-item" @click="locateExcerpt(d.excerpt)">
                <div class="deep-head">
                  <n-tag size="small" :type="d.severity === 'high' ? 'error' : 'warning'">
                    {{ d.type }}
                  </n-tag>
                  <span class="deep-chunk">块 {{ d.chunk_index }}</span>
                </div>
                <div class="deep-excerpt">{{ d.excerpt }}</div>
                <div class="deep-reason">依据：{{ d.reason }}</div>
              </div>
            </n-list-item>
          </n-list>
        </div>
      </div>
    </div>

    <n-modal v-model:show="showPreview" preset="card" title="改写预览（尚未落库）" style="width: 760px">
      <div class="preview-cols">
        <div class="preview-col">
          <div class="preview-col-title">原文</div>
          <div class="preview-col-text">{{ previewPassage }}</div>
        </div>
        <div class="preview-col">
          <div class="preview-col-title">改写</div>
          <div class="preview-col-text">{{ previewNew }}</div>
        </div>
      </div>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="discardPreview">放弃</n-button>
          <n-button type="primary" @click="applyPreview">应用</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showDelete" preset="card" title="删除章节" style="width: 460px">
      <p>
        确定删除“{{ selectedChapter ? chapterLabel(selectedChapter) : '' }}”吗？该章约
        {{ chapterWords }} 字，删除后无法恢复。
      </p>
      <p v-if="isDirty" class="delete-warning">当前未保存的修改也会一并丢失。</p>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showDelete = false">取消</n-button>
          <n-button type="error" :loading="deleting" @click="deleteChapter">确认删除</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showWriteNext" preset="card" title="生成下一章" style="width: 560px">
      <div class="next-form">
        <label>
          <span>下一章标题（可选）</span>
          <n-input v-model:value="nextTitle" placeholder="留空则自动使用章节序号" />
        </label>
        <label>
          <span>下一章指令（可选，最高优先级）</span>
          <n-input
            v-model:value="nextDirective"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="例如：黑西装男人跟踪吴迪，推进上一章留下的悬念"
          />
        </label>
        <p class="next-modal-hint">
          系统会同时携带上一章章首、结尾、最近剧情、Story Bible 与未解决伏笔，避免重复开局或改写既有能力规则。
        </p>
      </div>
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showWriteNext = false">取消</n-button>
          <n-button type="primary" @click="confirmWriteNext">开始生成</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showSettle" preset="card" title="设定变更建议" style="width: 560px">
      <p class="settle-summary">摘要：{{ settleSummary }}</p>
      <div v-if="suggestions.length">
        <div v-for="(s, i) in suggestions" :key="i" class="suggestion-row">
          <n-checkbox v-model:checked="checkedSuggestion[i]">{{ suggestionLabel(s) }}</n-checkbox>
        </div>
      </div>
      <n-empty v-else description="未识别到新增设定" />
      <template #footer>
        <div class="modal-actions">
          <n-button @click="showSettle = false">取消</n-button>
          <n-button type="primary" @click="confirmSettle">确认落库</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.writing-panel {
  display: flex;
  height: 100%;
}
.chapter-sidebar {
  width: 240px;
  border-right: 1px solid #e5e5e5;
  padding: 12px;
  overflow: auto;
}
.pane-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  font-weight: 600;
}
.chapter-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.book-stats {
  margin-top: 2px;
  color: #888;
  font-size: 12px;
  font-weight: 400;
}
.chapter-sidebar :deep(.n-list-item.active) {
  background: #e8f4ff;
}
.editor {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 12px 16px;
}
.editor-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.title-input {
  width: 100%;
}
.directive-input {
  width: 100%;
}
.btn-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.toolbar-groups {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
}
.toolbar-groups > .btn-row:not(:last-child) {
  padding-right: 16px;
  border-right: 1px solid #e5e5e5;
}
.save-state {
  color: #18a058;
  font-size: 12px;
}
.save-state.dirty {
  color: #d07830;
}
.next-chapter-hint {
  color: #666;
  font-size: 12px;
}
.editor-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.content-editor {
  width: 100%;
  height: 100%;
  min-height: 320px;
  padding: 12px;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  font-family: inherit;
  line-height: 1.7;
  resize: none;
  box-sizing: border-box;
}
.editor-status {
  padding-top: 6px;
  color: #777;
  font-size: 12px;
  text-align: right;
}
.delete-warning {
  color: #d03050;
}
.next-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.next-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-weight: 600;
}
.next-modal-hint {
  margin: 0;
  color: #666;
  font-size: 12px;
  line-height: 1.6;
}
.editor-empty {
  padding: 40px;
}
.stream-box {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 12px;
}
.stream-title {
  color: #888;
  margin-bottom: 8px;
}
.stream-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.cost-hint {
  color: #999;
  font-size: 12px;
}
.marks {
  margin-top: 12px;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 12px;
  max-height: 200px;
  overflow: auto;
}
.marks-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.preview {
  white-space: pre-wrap;
  line-height: 1.7;
  margin-bottom: 8px;
}
/* 冲突一种底色、套路命中一种底色 */
.preview .conflict {
  background: #ffd8d8;
}
.preview .cliche {
  background: #ffe58f;
}
.cliche-panel {
  margin-top: 12px;
  border: 1px solid #f0d9a0;
  border-radius: 4px;
  padding: 12px;
}
.cliche-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.cliche-density {
  color: #d48806;
  margin-left: 8px;
}
.cliche-total {
  color: #999;
  margin-left: 8px;
  font-weight: 400;
}
.cliche-cats {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.cliche-cat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.cliche-cat-count {
  color: #666;
}
.result-cols {
  display: flex;
  gap: 12px;
  margin-top: 12px;
  align-items: flex-start;
}
.result-col {
  flex: 1;
  min-width: 0;
  border-radius: 4px;
  padding: 12px;
  max-height: 240px;
  overflow: auto;
}
.result-conflicts {
  border: 1px solid #f0c0c0;
}
.result-deep {
  border: 1px solid #c0d8f0;
}
.result-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.result-conflicts .result-title {
  color: #d03050;
}
.result-deep .result-title {
  color: #2080f0;
}
.deep-meta {
  color: #999;
  font-weight: 400;
  font-size: 12px;
  margin-left: 8px;
}
.deep-item {
  cursor: pointer;
}
.deep-item:hover .deep-excerpt {
  background: #e8f4ff;
}
.deep-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.deep-chunk {
  color: #999;
  font-size: 12px;
}
.deep-excerpt {
  line-height: 1.6;
}
.deep-reason {
  color: #666;
  font-size: 12px;
  margin-top: 4px;
}
.preview-cols {
  display: flex;
  gap: 12px;
}
.preview-col {
  flex: 1;
  min-width: 0;
}
.preview-col-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.preview-col-text {
  white-space: pre-wrap;
  line-height: 1.7;
  border: 1px solid #e5e5e5;
  border-radius: 4px;
  padding: 8px;
  max-height: 360px;
  overflow: auto;
}
.settle-summary {
  color: #666;
}
.suggestion-row {
  margin: 6px 0;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
