import { splitChapterFixtures } from './chapter-fixtures.mjs'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import { nextTick, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const creationUrl = moduleUrl(compile(await readFile(new URL('../src/utils/chapterCreation.ts', import.meta.url), 'utf8')))
const chapterUrl = moduleUrl(compile(await readFile(new URL('../src/utils/chapter.ts', import.meta.url), 'utf8')))
const wordLimitsUrl = moduleUrl(compile(await readFile(new URL('../src/utils/wordLimits.ts', import.meta.url), 'utf8')))
const script = compileScript(parse(await readFile(new URL('../src/components/WritingPanel.vue', import.meta.url), 'utf8')).descriptor, { id: 'repair-preview-test' }).content
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { for (let index = 0; index < 20; index += 1) { await Promise.resolve(); await nextTick() } }
const chapter = (patch = {}) => ({ id: 'chapter-a', novel_id: 'novel-a', seq: 1, title: '第一章',
  content: '已有正文', directive: '依法申请', summary: '原有摘要', status: 'draft', outline_node_id: 'outline-a', ...patch })
const result = (patch = {}) => ({ issues: [], truncated: false, history_truncated: false, review_complete: true,
  previous_chapter_title: '', chapter_title: '第一章', requirements: [], requirement_checks: [],
  unreviewed_requirement_ids: [], word_count: null, ...patch })
const count = (n, limits = null, within = true) => ({ count: n, min_words: limits?.min_words ?? null,
  max_words: limits?.max_words ?? null, within_limits: limits ? within : null, warnings: within ? [] : ['候选稿超过最高字数，正文未修改'] })
const preview = (patch = {}) => ({ chapter_id: 'chapter-a', base_fingerprint: 'snapshot-a', title: '第一章', directive: '依法申请',
  original_content: '已有正文', content: '修订后的正文', original_word_count: count(4), word_count: count(7), ...patch })
let sequence = 0

async function panel(options = {}) {
  const props = reactive({ novelId: 'novel-a', initialChapterId: 'chapter-a' })
  const messages = [], generations = [], applications = [], checks = [], writes = [], hooks = {}
  const chapters = [chapter(), chapter({ id: 'chapter-b', seq: 2, title: '第二章', content: '第二章正文' })]
  let listCalls = 0
  const api = {
    listChapterMetadata: async () => { listCalls += 1; return [...chapters] },
    previewChapterRepair: async (...args) => { generations.push(args); return options.preview ? options.preview(...args) : preview() },
    applyChapterRepair: async (...args) => {
      applications.push(args)
      const updated = options.apply ? await options.apply(...args) : chapter({ content: args[3], summary: '', tension_score: null, drift_score: null })
      const index = chapters.findIndex((item) => item.id === updated.id)
      if (index >= 0) chapters[index] = updated
      return updated
    },
    checkChapterStory: async (...args) => { checks.push(args); return options.check ? options.check(...args) : result() },
    updateChapter: async (...args) => { writes.push(args); return chapter() },
  }
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.__repairHarness = {
    api: splitChapterFixtures(api), streamSSE: async () => { throw new Error('修订预览不应调用原有流式改写') }, ChapterRevisionPanel: {},
    NButton: {}, NCheckbox: {}, NEmpty: {}, NInput: {}, NInputNumber: {}, NList: {}, NListItem: {}, NModal: {}, NSelect: {}, NTag: {},
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  const source = 'const __harness = globalThis.__repairHarness\n' + script
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamSSE \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/from ['"]\.\.\/utils\/chapter['"]/g, `from '${chapterUrl}'`)
    .replace(/from ['"]\.\.\/utils\/chapterCreation['"]/g, `from '${creationUrl}'`)
    .replace(/from ['"]\.\.\/utils\/wordLimits['"]/g, `from '${wordLimitsUrl}'`)
    .replace(/import ChapterRevisionPanel from ['"]\.\/ChapterRevisionPanel.vue['"]/g, 'const { ChapterRevisionPanel } = __harness')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(moduleUrl(compile(source) + `\n// ${++sequence}`))).default
  const vm = component.setup(props, { expose() {}, emit() {} })
  hooks.mount(); await settle()
  return { vm, props, messages, generations, applications, checks, writes, hooks, chapters, listCalls: () => listCalls }
}

test('真实 API 修订预览只传修订依据，确认请求传指纹及候选全文', async () => {
  const { api } = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
  const originalFetch = globalThis.fetch
  const requests = [], data = { instructions: '保留价值冲突', word_limits: { min_words: 1200, max_words: 1600 },
    issues: [{ category: 'directive', severity: 'medium', requirement: '拒绝偷档案', excerpt: '', reason: '没有拒绝场景' }],
    unreviewed_requirement_ids: ['o001'] }
  try {
    globalThis.fetch = async (url, options) => { requests.push({ url, method: options.method, body: JSON.parse(options.body) }); return new Response(JSON.stringify(preview())) }
    await api.previewChapterRepair('novel-a', 'chapter-a', data)
    await api.applyChapterRepair('novel-a', 'chapter-a', 'fingerprint', '候选全文')
    assert.deepEqual(requests, [
      { url: '/api/v1/novels/novel-a/chapters/chapter-a/repair-preview', method: 'POST', body: data },
      { url: '/api/v1/novels/novel-a/chapters/chapter-a/repair-apply', method: 'POST', body: { base_fingerprint: 'fingerprint', content: '候选全文' } },
    ])
  } finally { globalThis.fetch = originalFetch }
})

test('真实 SFC 未保存的正文、标题、指令及非法范围均阻止修订，不隐式保存', async () => {
  for (const field of ['content', 'title', 'directive']) {
    const context = await panel()
    context.vm[field].value = '尚未保存的修改'
    context.vm.openRepair(); await context.vm.generateRepair()
    assert.equal(context.vm.showRepair.value, false)
    assert.match(context.messages[0].text, /请先保存/)
    assert.equal(context.generations.length + context.applications.length + context.writes.length, 0)
    assert.equal(context.vm[field].value, '尚未保存的修改')
    context.hooks.unmount()
  }
  const context = await panel()
  context.vm.storyMinWords.value = 2000; context.vm.storyMaxWords.value = 1600
  context.vm.openRepair()
  assert.equal(context.vm.showRepair.value, false)
  assert.match(context.messages[0].text, /最低字数不能高于/)
  context.hooks.unmount()
})

test('真实 SFC 冻结检查问题、待核 ID 和范围；候选越界不截断，放弃不写库', async () => {
  const limits = { min_words: 1200, max_words: 1600 }
  const issue = { category: 'directive', severity: 'medium', requirement: '拒绝偷档案', excerpt: '', reason: '没有拒绝场景' }
  const context = await panel({ preview: async () => preview({ content: '长'.repeat(1700), word_count: count(1700, limits, false) }) })
  context.vm.storyMinWords.value = 1200; context.vm.storyMaxWords.value = 1600
  context.vm.storyCheckResult.value = result({ issues: [issue], requirements: [{ id: 'o001', source: 'outline', text: '登记手续' }], unreviewed_requirement_ids: ['o001'] })
  context.vm.openRepair()
  context.vm.repairInstructions.value = '保留冲突和申请场景'
  context.vm.storyCheckResult.value.issues[0].reason = '之后改变的显示对象'
  await context.vm.generateRepair()
  assert.deepEqual(context.generations[0], ['novel-a', 'chapter-a', {
    instructions: '保留冲突和申请场景', word_limits: limits, issues: [{ ...issue, reason: '没有拒绝场景' }], unreviewed_requirement_ids: ['o001'],
  }])
  assert.equal(context.vm.repairPreview.value.content.length, 1700)
  assert.equal(context.vm.repairPreview.value.word_count.within_limits, false)
  assert.equal(context.vm.content.value, '已有正文')
  context.vm.discardRepair(); await context.vm.applyRepair()
  assert.equal(context.vm.repairPreview.value, null)
  assert.equal(context.vm.showRepair.value, false)
  assert.equal(context.applications.length + context.writes.length + context.checks.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 关闭生成窗口使迟到预览失效，旧失败不影响新请求 loading', async () => {
  const first = wait(), second = wait(); let calls = 0
  const context = await panel({ preview: async () => (++calls === 1 ? first.promise : second.promise) })
  context.vm.openRepair(); const old = context.vm.generateRepair()
  context.vm.handleRepairVisibility(false)
  context.vm.openRepair(); const current = context.vm.generateRepair()
  first.reject(new Error('旧请求失败')); await old
  assert.equal(context.vm.showRepair.value, true)
  assert.equal(context.vm.repairLoading.value, true)
  assert.equal(context.vm.repairError.value, '')
  second.resolve(preview()); await current
  assert.equal(context.vm.repairLoading.value, false)
  assert.equal(context.vm.repairPreview.value.content, '修订后的正文')
  context.vm.discardRepair()
  assert.equal(context.applications.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 正文、标题、指令和字数范围 A→B→A 都取消旧预览及确认资格', async () => {
  for (const field of ['content', 'title', 'directive', 'storyMinWords', 'storyMaxWords']) {
    const pending = wait()
    const context = await panel({ preview: async () => pending.promise })
    context.vm.openRepair(); const generating = context.vm.generateRepair()
    const original = context.vm[field].value
    context.vm[field].value = field.startsWith('story') ? 1500 : '短暂修改'
    context.vm[field].value = original
    pending.resolve(preview()); await generating; await context.vm.applyRepair()
    assert.equal(context.vm.repairPreview.value, null, field)
    assert.equal(context.vm.showRepair.value, false, field)
    assert.equal(context.applications.length, 0, field)
    assert.equal(context.vm[field].value, original, field)
    context.hooks.unmount()
  }
  const context = await panel()
  context.vm.openRepair(); await context.vm.generateRepair()
  context.vm.content.value = '新的手工正文'; context.vm.content.value = '已有正文'
  await context.vm.applyRepair()
  assert.equal(context.applications.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 修订要求 A→B→A 使旧生成失效但保留窗口，下一次可重新生成', async () => {
  const pending = wait(); let calls = 0
  const context = await panel({ preview: async () => ++calls === 1 ? pending.promise : preview() })
  context.vm.openRepair(); const generating = context.vm.generateRepair()
  context.vm.repairInstructions.value = '新的要求'; context.vm.repairInstructions.value = ''
  pending.resolve(preview()); await generating
  assert.equal(context.vm.repairPreview.value, null)
  assert.equal(context.vm.showRepair.value, true)
  await context.vm.generateRepair()
  assert.ok(context.vm.repairPreview.value)
  context.hooks.unmount()
})

test('真实 SFC 切章、跨作品切回或卸载后，迟到候选不重开也不保存', async () => {
  for (const change of ['chapter', 'novel', 'unmount']) {
    const pending = wait()
    const context = await panel({ preview: async () => pending.promise })
    context.vm.openRepair(); const generating = context.vm.generateRepair()
    if (change === 'chapter') { await context.vm.selectChapter(context.chapters[1], true); await context.vm.selectChapter(context.chapters[0], true) }
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (change === 'unmount') context.hooks.unmount()
    pending.resolve(preview()); await generating
    assert.equal(context.vm.repairPreview.value, null, change)
    assert.equal(context.vm.showRepair.value, false, change)
    assert.equal(context.applications.length, 0, change)
    if (change !== 'unmount') context.hooks.unmount()
  }
})

test('真实 SFC 后端依据不匹配时不能确认，只有显式刷新才替换已保存编辑稿', async () => {
  const context = await panel({ preview: async () => preview({ original_content: '外部更新的正文' }) })
  context.vm.openRepair(); await context.vm.generateRepair()
  assert.equal(context.vm.repairPreview.value, null)
  assert.equal(context.vm.repairRefreshNeeded.value, true)
  assert.equal(context.vm.content.value, '已有正文')
  await context.vm.applyRepair(); await context.vm.generateRepair()
  assert.equal(context.applications.length, 0)
  assert.equal(context.generations.length, 1)
  context.chapters[0] = chapter({ content: '外部更新的正文' })
  await context.vm.refreshRepairChapter()
  assert.equal(context.vm.content.value, '外部更新的正文')
  assert.equal(context.vm.isDirty.value, false)
  assert.equal(context.vm.showRepair.value, false)
  context.hooks.unmount()
})

test('真实 SFC 只在确认成功后替换同章，以冻结目标复检，保留草稿与版本入口', async () => {
  const limits = { min_words: 1200, max_words: 1600 }
  const context = await panel()
  context.vm.storyMinWords.value = 1200; context.vm.storyMaxWords.value = 1600
  context.vm.openRepair(); await context.vm.generateRepair()
  assert.equal(context.checks.length, 0)
  await context.vm.applyRepair()
  assert.deepEqual(context.applications, [['novel-a', 'chapter-a', 'snapshot-a', '修订后的正文']])
  assert.deepEqual(context.checks, [['novel-a', 'chapter-a', '修订后的正文', '依法申请', limits]])
  assert.equal(context.vm.content.value, '修订后的正文')
  assert.equal(context.vm.selectedChapter.value.summary, '')
  assert.equal(context.vm.selectedChapter.value.status, 'draft')
  assert.equal(context.vm.isDirty.value, false)
  assert.equal(context.vm.showRepair.value, false)
  assert.ok(context.vm.storyCheckResult.value)
  assert.equal(context.writes.length, 0)
  assert.match(context.messages.find((item) => /修订稿已保存/.test(item.text)).text, /旧版本已保留/)
  context.vm.openRevisionHistory()
  assert.equal(context.vm.showRevisions.value, true)
  context.hooks.unmount()
})

test('真实 SFC 保存请求期间关闭无效，重复确认和普通保存不会多写', async () => {
  const pending = wait()
  const context = await panel({ apply: async () => pending.promise })
  context.vm.openRepair(); await context.vm.generateRepair(); const applying = context.vm.applyRepair()
  context.vm.handleRepairVisibility(false); context.vm.discardRepair()
  await context.vm.applyRepair(); await context.vm.saveContent()
  await context.vm.selectChapter(context.chapters[1]); await context.vm.startNew()
  assert.equal(context.vm.showRepair.value, true)
  assert.equal(context.vm.selectedId.value, 'chapter-a')
  assert.equal(context.applications.length, 1)
  assert.equal(context.writes.length, 0)
  pending.resolve(chapter({ content: '修订后的正文', summary: '' })); await applying
  assert.equal(context.vm.repairApplying.value, false)
  context.hooks.unmount()
})

test('真实 SFC 保存迟到时编辑或切章 A→B→A 不覆盖编辑稿，也不串章复检', async () => {
  for (const change of ['content', 'title', 'directive', 'instructions', 'range', 'chapter', 'novel', 'unmount']) {
    const pending = wait()
    const context = await panel({ apply: async () => pending.promise })
    context.vm.openRepair(); await context.vm.generateRepair(); const applying = context.vm.applyRepair()
    if (['content', 'title', 'directive'].includes(change)) {
      const original = context.vm[change].value
      context.vm[change].value = '用户修改'; context.vm[change].value = original
    }
    if (change === 'range') { context.vm.storyMaxWords.value = 1600; context.vm.storyMaxWords.value = null }
    if (change === 'instructions') { context.vm.repairInstructions.value = '新修订要求'; context.vm.repairInstructions.value = '' }
    if (change === 'chapter') { await context.vm.selectChapter(context.chapters[1], true); await context.vm.selectChapter(context.chapters[0], true) }
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (change === 'unmount') context.hooks.unmount()
    const editorBefore = context.vm.content.value
    pending.resolve(chapter({ content: '已保存候选稿' })); await applying
    assert.equal(context.vm.content.value, editorBefore, change)
    assert.equal(context.checks.length, 0, change)
    assert.deepEqual(context.applications[0].slice(0, 2), ['novel-a', 'chapter-a'], change)
    assert.equal(context.vm.repairApplying.value, false, change)
    if (change !== 'unmount') context.hooks.unmount()
  }
})

test('真实 SFC 相同候选不触发覆盖或虚构新版本，仍可补充要求重新生成', async () => {
  const context = await panel({ preview: async () => preview({ content: '已有正文' }) })
  context.vm.openRepair(); await context.vm.generateRepair(); await context.vm.applyRepair()
  assert.equal(context.applications.length + context.checks.length + context.writes.length, 0)
  assert.equal(context.vm.showRepair.value, true)
  assert.equal(context.messages.some((item) => /旧版本已保留/.test(item.text)), false)
  context.vm.repairInstructions.value = '修改重复描写'
  assert.equal(context.vm.repairPreview.value, null)
  assert.equal(context.vm.showRepair.value, true)
  context.hooks.unmount()
})

test('真实 SFC 409/410 不抹编辑稿，不误称保存；刷新核对前不再生成或确认', async () => {
  for (const status of [409, 410]) {
    const context = await panel({ apply: async () => { throw Object.assign(new Error('依据已改变'), { status }) } })
    context.vm.openRepair(); await context.vm.generateRepair(); await context.vm.applyRepair()
    assert.equal(context.vm.content.value, '已有正文')
    assert.equal(context.vm.repairPreview.value, null)
    assert.equal(context.vm.repairRefreshNeeded.value, true)
    assert.match(context.vm.repairError.value, /本次未保存/)
    assert.equal(context.messages.some((item) => /修订稿已保存/.test(item.text)), false)
    await context.vm.generateRepair(); await context.vm.applyRepair()
    assert.equal(context.generations.length, 1)
    assert.equal(context.applications.length, 1)
    assert.equal(context.checks.length, 0)
    context.hooks.unmount()
  }
})

test('真实 SFC 保存回应丢失保留候选供复制，不把未确认结果说成已保存', async () => {
  const context = await panel({ apply: async () => { throw new Error('网络连接中断') } })
  context.vm.openRepair(); await context.vm.generateRepair(); await context.vm.applyRepair()
  assert.equal(context.vm.content.value, '已有正文')
  assert.equal(context.vm.repairPreview.value.content, '修订后的正文')
  assert.match(context.vm.repairError.value, /未得到成功确认/)
  assert.equal(context.messages.some((item) => /修订稿已保存/.test(item.text)), false)
  assert.equal(context.checks.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 已保存而复检失败分别报告；模型不完整或越界结果继续显示警告', async () => {
  const context = await panel({ check: async () => { throw new Error('模型检查失败') } })
  context.vm.openRepair(); await context.vm.generateRepair(); await context.vm.applyRepair()
  assert.equal(context.vm.content.value, '修订后的正文')
  assert.equal(context.vm.isDirty.value, false)
  assert.equal(context.vm.storyCheckResult.value, null)
  assert.equal(context.messages.some((item) => /修订稿已保存.*复检未完成/.test(item.text)), true)
  assert.equal(context.messages.some((item) => /修订保存未得到/.test(item.text)), false)
  context.hooks.unmount()
  const incomplete = await panel({ check: async () => result({ review_complete: false,
    word_count: count(1700, { min_words: 1200, max_words: 1600 }, false) }) })
  incomplete.vm.storyMinWords.value = 1200; incomplete.vm.storyMaxWords.value = 1600
  incomplete.vm.openRepair(); await incomplete.vm.generateRepair(); await incomplete.vm.applyRepair()
  assert.equal(incomplete.vm.storyCheckResult.value.review_complete, false)
  assert.equal(incomplete.vm.storyCheckResult.value.word_count.within_limits, false)
  assert.equal(incomplete.messages.at(-1).type, 'warning')
  assert.match(incomplete.messages.at(-1).text, /不完整/)
  incomplete.hooks.unmount()
})
