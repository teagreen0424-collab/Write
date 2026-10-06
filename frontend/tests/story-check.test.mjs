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
const chapterUrl = moduleUrl(compile(await readFile(new URL('../src/utils/chapter.ts', import.meta.url), 'utf8')))
const script = compileScript(parse(await readFile(new URL('../src/components/WritingPanel.vue', import.meta.url), 'utf8')).descriptor, { id: 'story-check-test' }).content
let sequence = 0
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { for (let index = 0; index < 20; index += 1) { await Promise.resolve(); await nextTick() } }
const chapter = (patch = {}) => ({ id: 'chapter-a', novel_id: 'novel-a', seq: 1, title: '第一章',
  content: '作者尚未保存的正文', directive: '已保存的章前指令', summary: '已有摘要', status: 'draft', outline_node_id: '', ...patch })
const result = (patch = {}) => ({ issues: [], truncated: false, history_truncated: false, review_complete: true,
  previous_chapter_title: '前一章', chapter_title: '第一章', ...patch })

async function panel(options = {}) {
  const props = reactive({ novelId: 'novel-a', initialChapterId: 'chapter-a' })
  const messages = [], checks = [], writes = [], hooks = {}
  const chapters = [chapter(), chapter({ id: 'chapter-b', seq: 2, title: '第二章', content: '第二章正文' })]
  const api = {
    listChapters: async () => chapters,
    checkChapterStory: async (...args) => { checks.push(args); return options.check ? options.check(...args) : result() },
    updateChapter: async (...args) => { writes.push(args); return chapter() },
  }
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.__storyCheckHarness = {
    api, streamSSE: async () => { throw new Error('检查不应生成正文') }, ChapterRevisionPanel: {},
    NButton: {}, NCheckbox: {}, NEmpty: {}, NInput: {}, NList: {}, NListItem: {}, NModal: {}, NSelect: {}, NTag: {},
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  const source = 'const __harness = globalThis.__storyCheckHarness\n' + script
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamSSE \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/from ['"]\.\.\/utils\/chapter['"]/g, `from '${chapterUrl}'`)
    .replace(/import ChapterRevisionPanel from ['"]\.\/ChapterRevisionPanel.vue['"]/g, 'const { ChapterRevisionPanel } = __harness')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(moduleUrl(compile(source) + `\n// ${++sequence}`))).default
  const vm = component.setup(props, { expose() {}, emit() {} })
  hooks.mount(); await settle()
  return { vm, props, messages, checks, writes, hooks, chapters }
}

test('真实 API 请求传当前编辑正文与空指令覆盖，不要求绑定大纲', async () => {
  const { api } = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
  const originalFetch = globalThis.fetch
  const requests = []
  try {
    globalThis.fetch = async (url, options) => { requests.push({ url, options }); return new Response(JSON.stringify(result())) }
    const response = await api.checkChapterStory('novel-a', 'chapter-a', '未保存的稿件', '')
    assert.equal(requests[0].url, '/api/v1/novels/novel-a/chapters/chapter-a/story-check')
    assert.equal(requests[0].options.method, 'POST')
    assert.deepEqual(JSON.parse(requests[0].options.body), { content: '未保存的稿件', directive: '' })
    assert.equal(response.review_complete, true)
  } finally { globalThis.fetch = originalFetch }
})

test('真实 SFC 检查未保存稿及空指令，显示分类原要求与引文而不保存或定稿', async () => {
  const finding = { category: 'directive', severity: 'medium', requirement: '明确拒绝偷档案', excerpt: '正文中的句子', reason: '正文没有价值冲突场景' }
  const context = await panel({ check: async () => result({ issues: [finding] }) })
  context.vm.content.value = '作者刚改的正文中的句子'
  context.vm.directive.value = ''
  await context.vm.doStoryCheck()
  assert.deepEqual(context.checks, [['novel-a', 'chapter-a', '作者刚改的正文中的句子', '']])
  assert.deepEqual(context.vm.storyCheckResult.value.issues, [finding])
  assert.equal(context.vm.STORY_ISSUE_LABEL[finding.category], '章前指令')
  assert.equal(context.vm.STORY_SEVERITY_LABEL[finding.severity], '中等')
  assert.equal(context.vm.content.value, '作者刚改的正文中的句子')
  assert.equal(context.chapters[0].status, 'draft')
  assert.equal(context.chapters[0].summary, '已有摘要')
  assert.equal(context.writes.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 编辑正文或指令即清结果，改回原值也不能接受旧请求', async () => {
  for (const field of ['content', 'directive']) {
    const pending = wait()
    const context = await panel({ check: async () => pending.promise })
    const checkedValue = context.vm[field].value
    const checking = context.vm.doStoryCheck()
    context.vm[field].value = '正在改稿'
    context.vm[field].value = checkedValue
    assert.equal(context.vm.storyCheckLoading.value, false)
    pending.resolve(result({ issues: [{ category: 'continuity' }] })); await checking
    assert.equal(context.vm.storyCheckResult.value, null)
    assert.equal(context.messages.length, 0)
    context.hooks.unmount()
  }
  const context = await panel()
  await context.vm.doStoryCheck()
  assert.ok(context.vm.storyCheckResult.value)
  context.vm.directive.value = '新指令'
  assert.equal(context.vm.storyCheckResult.value, null)
  context.hooks.unmount()
})

test('真实 SFC 切章后旧请求不能串章，切回同章也不会接纳旧结果', async () => {
  const pending = wait()
  const context = await panel({ check: async () => pending.promise })
  const checking = context.vm.doStoryCheck()
  await context.vm.selectChapter(context.chapters[1], true)
  await context.vm.selectChapter(context.chapters[0], true)
  pending.resolve(result()); await checking
  assert.equal(context.vm.storyCheckResult.value, null)
  assert.equal(context.vm.storyCheckLoading.value, false)
  assert.equal(context.messages.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 跨作品同章节 ID 和卸载后的迟到返回不会显示旧结果', async () => {
  for (const change of ['novel', 'unmount']) {
    const pending = wait()
    const context = await panel({ check: async () => pending.promise })
    const checking = context.vm.doStoryCheck()
    if (change === 'novel') { context.props.novelId = 'novel-b'; await settle() }
    else context.hooks.unmount()
    pending.resolve(result()); await checking
    assert.equal(context.vm.storyCheckResult.value, null)
    assert.equal(context.messages.length, 0)
    if (change === 'novel') context.hooks.unmount()
  }
})

test('真实 SFC 旧请求结束不会覆盖新的结果或提前关闭新请求 loading', async () => {
  const first = wait(), second = wait()
  let count = 0
  const context = await panel({ check: async () => (++count === 1 ? first.promise : second.promise) })
  const oldRequest = context.vm.doStoryCheck()
  context.vm.content.value = '新正文'
  const newRequest = context.vm.doStoryCheck()
  first.reject(new Error('旧请求失败')); await oldRequest
  assert.equal(context.vm.storyCheckLoading.value, true)
  assert.equal(context.messages.length, 0)
  second.resolve(result({ chapter_title: '新稿检查' })); await newRequest
  assert.equal(context.vm.storyCheckLoading.value, false)
  assert.equal(context.vm.storyCheckResult.value.chapter_title, '新稿检查')
  context.hooks.unmount()
})

test('真实 SFC 截断、不完整或检查失败均不能提示全部通过', async () => {
  for (const response of [result({ review_complete: false }), result({ truncated: true })]) {
    const context = await panel({ check: async () => response })
    await context.vm.doStoryCheck()
    assert.equal(context.messages[0].type, 'warning')
    assert.match(context.vm.storyCheckEmptyText.value, /不完整|未完成|截断/)
    assert.doesNotMatch(context.vm.storyCheckEmptyText.value, /全部通过/)
    context.hooks.unmount()
  }
  const context = await panel({ check: async () => { throw new Error('模型返回格式无效') } })
  await context.vm.doStoryCheck()
  assert.equal(context.vm.storyCheckResult.value, null)
  assert.equal(context.vm.storyCheckLoading.value, false)
  assert.equal(context.messages[0].type, 'error')
  assert.match(context.messages[0].text, /剧情检查未完成/)
  context.hooks.unmount()
})
