import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'
import { metadataFixture } from './chapter-fixtures.mjs'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'], ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../utils/asyncScope', '../src/utils/asyncScope.ts'], ['../types', '../src/types.ts'],
  ['../utils/chapterGeneration', '../src/utils/chapterGeneration.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile((await readFile(new URL(path, import.meta.url), 'utf8')).replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)))])))
const apiModule = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
const flush = async () => { for (let i = 0; i < 25; i++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const chapter = (id = 'chapter-a', patch = {}) => ({ id, novel_id: 'novel-a', outline_node_id: `outline-${id}`, seq: id === 'chapter-a' ? 1 : 2,
  title: id, content: `${id}正文`, directive: `${id}指令`, summary: `${id}摘要`, status: 'draft', tension_score: 7, drift_score: 0.3,
  created_at: '2026-10-07T00:00:00', updated_at: '2026-10-07T00:00:00', ...patch })
const settleResult = (patch = {}) => ({ chapter_id: 'chapter-a', source_fingerprint: 'a'.repeat(64), summary: '真实新摘要',
  suggestions: [], drift: 0.08, conflicts: [], warnings: [], checks: { drift: 'completed', consistency: 'completed' }, ...patch })
let sequence = 0

async function panel(name = 'WritingPanel', options = {}) {
  const props = reactive({ novelId: 'novel-a', ...options.props })
  const messages = [], requests = [], emitted = [], hooks = {}, listeners = new Map()
  const rows = options.rows ?? [chapter(), chapter('chapter-b')]
  const database = new Map(rows.map((row) => [row.id, structuredClone(row)]))
  globalThis.window = { confirm: () => true, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  const implementations = {
    listChapterMetadata: async (novelId) => [...database.values()].filter((row) => row.novel_id === novelId).sort((a, b) => a.seq - b.seq).map(metadataFixture),
    getChapter: async (novelId, id) => {
      const row = database.get(id)
      if (!row || row.novel_id !== novelId) throw new Error('章节不存在')
      return structuredClone(row)
    },
    createChapter: async (novelId, data) => {
      const row = chapter('created-chapter', { novel_id: novelId, ...data, seq: database.size + 1, summary: '', tension_score: null, drift_score: null })
      database.set(row.id, row); return structuredClone(row)
    },
    updateChapter: async (novelId, id, data) => {
      const row = { ...database.get(id), ...data, novel_id: novelId }
      if (['title', 'content', 'directive'].some((field) => field in data)) Object.assign(row, { status: 'draft', summary: '', tension_score: null, drift_score: null })
      database.set(id, row); return structuredClone(row)
    },
    deleteChapter: async (_novelId, id) => { database.delete(id) },
    settleChapter: async () => settleResult(),
    listNovels: async () => [{ id: 'novel-a', style_anchor: '已保存文风' }, { id: 'novel-b', style_anchor: 'B文风' }],
    listEntities: async () => [], updateNovel: async () => {}, listOutline: async () => [],
    getForeshadowings: async () => ({ groups: { planted: [], suspended: [], consumed: [] } }),
    ...options.api,
  }
  const harness = {
    api: Object.fromEntries(Object.entries(implementations).map(([name, fn]) => [name, (...args) => { requests.push({ name, args: structuredClone(args) }); return fn(...args) }])),
    streamSSE: options.streamSSE ?? (async () => { throw new Error('此流程不应调用模型') }),
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    useDialog: () => ({ warning: () => {} }),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const component of ['NButton', 'NCheckbox', 'NEmpty', 'NForm', 'NFormItem', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NMenu', 'NModal', 'NSelect', 'NTag']) harness[component] = component.toLowerCase()
  const key = `__chapterDirectory${++sequence}`; globalThis[key] = harness
  const descriptor = parse(await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `directory-${sequence}` })
  let source = `const __harness = globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api(?:, streamSSE)? \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1 = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  for (const [name, url] of Object.entries(dependencies)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source)))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  await hooks.mount?.(); await flush()
  const template = compileTemplate({ source: descriptor.template.content, id: `directory-${sequence}`, filename: `${name}.vue`, compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${sequence}`))
  return { vm, props, messages, requests, emitted, database,
    render: () => rendered.render({}, [], props, proxyRefs(vm), {}, {}),
    dispose: () => { hooks.unmount?.(); scope.stop(); delete globalThis[key] },
  }
}

function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'string') return node
  if (!node || typeof node !== 'object') return ''
  return typeof node.children?.default === 'function' ? textOf(node.children.default()) : textOf(node.children)
}
function assertMetadata(rows) { for (const row of rows) for (const field of ['content', 'summary', 'directive']) assert.equal(field in row, false) }

test('真实API元信息与详情使用独立路径，旧完整目录接口保持兼容', async () => {
  const original = globalThis.fetch, urls = []
  globalThis.fetch = async (url) => { urls.push(url); return new Response(JSON.stringify(url.endsWith('/metadata') ? [metadataFixture(chapter())] : url.endsWith('/chapters') ? [chapter()] : chapter())) }
  try {
    assertMetadata(await apiModule.api.listChapterMetadata('novel-a'))
    assert.equal((await apiModule.api.getChapter('novel-a', 'chapter-a')).content, chapter().content)
    await apiModule.api.listChapters('novel-a')
    assert.deepEqual(urls, ['/api/v1/novels/novel-a/chapters/metadata', '/api/v1/novels/novel-a/chapters/chapter-a', '/api/v1/novels/novel-a/chapters'])
  } finally { globalThis.fetch = original }
})

test('工作台目录只读取元信息，未选章不读取任何正文，选章一次取得完整详情', async () => {
  const context = await panel()
  assertMetadata(context.vm.chapters.value)
  assert.deepEqual(context.requests.map((entry) => entry.name), ['listChapterMetadata'])
  assert.equal(context.vm.selectedDetail.value, null)
  await context.vm.selectChapter(context.vm.chapters.value[0])
  assert.equal(context.vm.content.value, chapter().content); assert.equal(context.vm.directive.value, chapter().directive)
  assert.equal(context.vm.selectedChapter.value.summary, chapter().summary)
  await context.vm.selectChapter(context.vm.chapters.value[0])
  assert.equal(context.requests.filter((entry) => entry.name === 'getChapter').length, 1)
  assertMetadata(context.vm.chapters.value)
  context.dispose()
})

test('详情快选乱序只接受最后目标，旧完成不停止新目标的读取提示', async () => {
  const old = deferred(), fresh = deferred()
  const context = await panel('WritingPanel', { api: { getChapter: async (_id, target) => target === 'chapter-a' ? old.promise : fresh.promise } })
  const first = context.vm.selectChapter(context.vm.chapters.value[0], true)
  const second = context.vm.selectChapter(context.vm.chapters.value[1], true)
  old.resolve(chapter()); await first
  assert.equal(context.vm.selectedId.value, null); assert.equal(context.vm.chapterLoadingId.value, 'chapter-b')
  fresh.resolve(chapter('chapter-b')); await second
  assert.equal(context.vm.selectedId.value, 'chapter-b'); assert.equal(context.vm.content.value, chapter('chapter-b').content)
  assert.equal(context.vm.chapterLoadingId.value, null); assert.equal(context.messages.length, 0)
  context.dispose()
})

test('回到当前章可取消在途改选，目标ABA与旧错误不能替换当前详情', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = deferred()
    const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async (_id, target) => target === 'chapter-a' ? chapter() : pending.promise } })
    const selecting = context.vm.selectChapter(context.vm.chapters.value[1], true)
    await context.vm.selectChapter(context.vm.chapters.value[0])
    if (outcome === 'success') pending.resolve(chapter('chapter-b')); else pending.reject(new Error('旧改选失败'))
    await selecting
    assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, chapter().content)
    assert.equal(context.vm.chapterLoadingId.value, null); assert.equal(context.messages.length, 0)
    context.dispose()
  }
})

test('详情等待期间当前正文仍可编辑，新输入与基线不会被迟到目标覆盖', async () => {
  const pending = deferred()
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async (_id, target) => target === 'chapter-a' ? chapter() : pending.promise } })
  const selecting = context.vm.selectChapter(context.vm.chapters.value[1], true)
  assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, chapter().content)
  context.vm.content.value = '作者在读取时的新输入'
  pending.resolve(chapter('chapter-b')); assert.equal(await selecting, false)
  assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, '作者在读取时的新输入')
  assert.equal(context.vm.savedSnapshot.value.content, chapter().content); assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.vm.chapterLoadingId.value, null)
  context.dispose()
})

test('详情当前读取失败或身份不匹配保留旧章，不把其他章的正文装进编辑器', async () => {
  for (const invalid of ['failure', 'chapter', 'novel']) {
    const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async (_id, target) => {
      if (target === 'chapter-a') return chapter()
      if (invalid === 'failure') throw new Error('离线详情失败')
      return chapter(invalid === 'chapter' ? 'wrong-chapter' : 'chapter-b', { novel_id: invalid === 'novel' ? 'novel-b' : 'novel-a' })
    } } })
    await context.vm.selectChapter(context.vm.chapters.value[1], true)
    assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, chapter().content)
    assert.equal(context.vm.isDirty.value, false); assert.match(context.messages.at(-1).text, /编辑稿已保留/)
    context.dispose()
  }
})

test('详情来源作品/正文ABA、新章身份重置和卸载使旧成功失败全部静默', async () => {
  for (const change of ['novel', 'text', 'new', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = deferred()
    const context = await panel('WritingPanel', { api: { getChapter: async () => pending.promise } })
    const selecting = context.vm.selectChapter(context.vm.chapters.value[0], true)
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await flush() }
    if (change === 'text') { context.vm.content.value = '短暂新稿'; context.vm.content.value = '' }
    if (change === 'new') await context.vm.startNew(true)
    if (change === 'unmount') context.dispose()
    context.vm.content.value = '当前新手写正文'
    if (outcome === 'success') pending.resolve(chapter()); else pending.reject(new Error('旧详情错误'))
    await selecting
    assert.equal(context.vm.content.value, '当前新手写正文'); assert.equal(context.vm.selectedId.value, null)
    assert.equal(context.messages.length, 0)
    if (change !== 'unmount') context.dispose()
  }
})

test('初始章详情挂起时新手写稿不被自动定位替换', async () => {
  const pending = deferred()
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async () => pending.promise } })
  context.vm.title.value = '作者新章'; context.vm.content.value = '初始读取期间的手写稿'
  pending.resolve(chapter()); await flush()
  assert.equal(context.vm.selectedId.value, null); assert.equal(context.vm.title.value, '作者新章')
  assert.equal(context.vm.content.value, '初始读取期间的手写稿'); assert.equal(context.vm.isDirty.value, true)
  context.dispose()
})

test('丢弃确认通过前不读目标详情，取消仍停留原章并保留作者稿', async () => {
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' } })
  context.vm.content.value = '尚未保存的作者稿'
  const selecting = context.vm.selectChapter(context.vm.chapters.value[1]); await flush()
  assert.equal(context.vm.showDiscard.value, true)
  assert.equal(context.requests.filter((entry) => entry.name === 'getChapter').length, 1)
  context.vm.finishDiscard(false); await selecting
  assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, '尚未保存的作者稿')
  context.dispose()
})

test('每次键入仅重数当前章，目录总字数缓存不重新访问其他99章字数', async () => {
  const rows = Array.from({ length: 100 }, (_, i) => chapter(`chapter-${i}`, { seq: i + 1, content: '汉'.repeat(6000) }))
  const reads = Array(100).fill(0)
  const metadata = rows.map((row, i) => { const value = metadataFixture(row); Object.defineProperty(value, 'word_count', { enumerable: true, get: () => { reads[i]++; return 6000 } }); return value })
  const context = await panel('WritingPanel', { rows, props: { initialChapterId: 'chapter-0' }, api: { listChapterMetadata: async () => metadata } })
  assert.equal(context.vm.totalWords.value, 600000)
  const before = reads.slice()
  for (let i = 0; i < 30; i++) { context.vm.content.value += '字'; assert.equal(context.vm.totalWords.value, 600001 + i) }
  assert.deepEqual(reads.slice(1), before.slice(1))
  assertMetadata(context.vm.chapters.value)
  context.dispose()
})

test('保存真实响应与摘要校正更新独立详情，目录仍无正文且字数即时准确', async () => {
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' } })
  context.vm.content.value = '汉 字😀𠀀\u0000'
  await context.vm.saveContent()
  assert.equal(context.vm.chapters.value[0].word_count, 5)
  assert.equal(context.vm.selectedChapter.value.content, '汉 字😀𠀀\u0000'); assert.equal(context.vm.selectedChapter.value.summary, '')
  context.vm.content.value = '正文的尚未保存改动'
  context.vm.openSummary(); context.vm.summaryDraft.value = '只校正摘要'; await context.vm.saveSummary()
  assert.equal(context.vm.selectedChapter.value.summary, '只校正摘要'); assert.equal(context.vm.content.value, '正文的尚未保存改动')
  assert.equal(context.vm.isDirty.value, true); assertMetadata(context.vm.chapters.value)
  assert.equal(context.requests.filter((entry) => entry.name === 'getChapter').length, 1)
  context.dispose()
})

test('摘要或恢复真实写入完成后，先前改选详情不能覆盖已确认基线', async () => {
  for (const operation of ['summary', 'restore']) {
    const pending = deferred()
    const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async (_id, target) => target === 'chapter-a' ? chapter() : pending.promise } })
    if (operation === 'restore') context.vm.openRevisionHistory()
    const selecting = context.vm.selectChapter(context.vm.chapters.value[1], true)
    if (operation === 'summary') {
      context.vm.openSummary(); context.vm.summaryDraft.value = '已确认校正摘要'; await context.vm.saveSummary()
    } else {
      const restored = chapter('chapter-a', { summary: '', tension_score: null, drift_score: null })
      context.database.set(restored.id, restored); await context.vm.onRestored(restored)
    }
    pending.resolve(chapter('chapter-b')); await selecting
    assert.equal(context.vm.selectedId.value, 'chapter-a'); assert.equal(context.vm.content.value, chapter().content)
    assert.equal(context.vm.savedSnapshot.value.content, chapter().content)
    assert.equal(context.vm.selectedChapter.value.summary, operation === 'summary' ? '已确认校正摘要' : '')
    assertMetadata(context.vm.chapters.value); context.dispose()
  }
})

test('定稿真实主结果先同步摘要状态漂移，后置目录失败不显示旧评分或草稿', async () => {
  for (const drift of [0.08, null, NaN, Infinity]) {
    let reads = 0
    const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: {
      listChapterMetadata: async () => { if (++reads > 1) throw new Error('目录失败'); return [metadataFixture(chapter())] },
      settleChapter: async () => settleResult({ drift, warnings: ['质量检查仍需核对'] }),
    } })
    await context.vm.doSettle()
    assert.equal(context.vm.selectedChapter.value.summary, '真实新摘要'); assert.equal(context.vm.selectedChapter.value.status, 'finalized')
    assert.equal(context.vm.selectedChapter.value.drift_score, Number.isFinite(drift) && typeof drift === 'number' ? drift : null)
    assert.equal(context.vm.selectedChapter.value.tension_score, 7)
    assert.equal(context.vm.chapters.value[0].status, 'finalized'); assert.equal(context.vm.chapters.value[0].drift_score, context.vm.selectedChapter.value.drift_score)
    assert.match(textOf(context.render()), /已定稿/); assert.ok(context.vm.settleWarnings.value.includes('质量检查仍需核对'))
    assert.equal(context.vm.content.value, chapter().content); assert.equal(context.vm.isDirty.value, false)
    assertMetadata(context.vm.chapters.value); context.dispose()
  }
})

test('新章SSE采用完成回包指定章，不猜目录末章且不重复拉取详情', async () => {
  const created = chapter('generated', { seq: 3, content: '真实生成完成稿', summary: '' })
  const tail = chapter('another-client-tail', { seq: 4, content: '另一请求后续章' })
  const context = await panel('WritingPanel', { api: { listChapterMetadata: async () => [metadataFixture(created), metadataFixture(tail)] },
    streamSSE: async (_url, _data, handlers) => { handlers.onToken('真实生成完成稿'); await handlers.onDone({ type: 'done', chapter: created }) },
  })
  await context.vm.generateChapter()
  assert.equal(context.vm.selectedId.value, 'generated'); assert.equal(context.vm.content.value, created.content)
  assert.equal(context.requests.some((entry) => entry.name === 'getChapter'), false)
  assert.equal(context.vm.streaming.value, false); assert.equal(context.vm.isDirty.value, false)
  assertMetadata(context.vm.chapters.value); context.dispose()
})

test('生成完成后的目录刷新等待仍可手写，新编辑保留而真实生成稿作为保存基线', async () => {
  const pending = deferred(); let reads = 0
  const created = chapter('generated', { seq: 3, content: '真实生成完成稿', summary: '' })
  const context = await panel('WritingPanel', { api: { listChapterMetadata: async () => ++reads === 1 ? [] : pending.promise },
    streamSSE: async (_url, _data, handlers) => { handlers.onToken(created.content); await handlers.onDone({ chapter: created }) },
  })
  const generating = context.vm.generateChapter(); await flush()
  assert.equal(context.vm.selectedId.value, created.id); assert.equal(context.vm.streaming.value, false)
  context.vm.content.value = '生成完成后作者的新手写内容'
  pending.resolve([metadataFixture(created)]); await generating
  assert.equal(context.vm.content.value, '生成完成后作者的新手写内容'); assert.equal(context.vm.savedSnapshot.value.content, created.content)
  assert.equal(context.vm.selectedDetail.value.content, created.content); assert.equal(context.vm.isDirty.value, true)
  assertMetadata(context.vm.chapters.value); context.dispose()
})

test('前轮生成目录迟到不能清下一轮tokens或loading，也不能倒退下一轮已保存详情', async () => {
  for (const phase of ['tokens', 'done']) {
    const oldDirectory = deferred(), nextFinish = deferred(); let reads = 0, streams = 0
    const created = chapter('generated', { seq: 3, content: '首轮真实生成稿', summary: '' })
    const continued = { ...created, content: '第二轮真实续写完成稿', summary: '', tension_score: null, drift_score: null }
    const context = await panel('WritingPanel', { api: { listChapterMetadata: async () => {
      if (++reads === 1) return []
      if (reads === 2) return oldDirectory.promise
      return [metadataFixture(continued)]
    } }, streamSSE: async (_url, _data, handlers) => {
      if (++streams === 1) { handlers.onToken(created.content); await handlers.onDone({ chapter: created }) }
      else { handlers.onToken('第二轮已收到的tokens'); await nextFinish.promise; await handlers.onDone({ chapter: continued }) }
    } })
    const first = context.vm.generateChapter(); await flush()
    assert.equal(context.vm.selectedId.value, created.id); assert.equal(context.vm.streaming.value, false)
    const second = context.vm.doContinue(); await flush()
    try {
      if (phase === 'done') { nextFinish.resolve(); await second }
      oldDirectory.resolve([metadataFixture(created)]); await first
      if (phase === 'tokens') {
        assert.equal(context.vm.streamText.value, '第二轮已收到的tokens'); assert.equal(context.vm.streaming.value, true)
        nextFinish.resolve(); await second
      }
      assert.equal(context.vm.content.value, continued.content)
      assert.equal(context.vm.savedSnapshot.value.content, continued.content)
      assert.equal(context.vm.selectedDetail.value.content, continued.content)
      assert.equal(context.vm.chapters.value[0].word_count, metadataFixture(continued).word_count)
    } finally { nextFinish.resolve(); await second; context.dispose() }
  }
})

test('前轮续写目录等待期间切章ABA并启动新流，旧刷新不清新tokens或回退详情', async () => {
  const oldDirectory = deferred(), nextFinish = deferred(); let reads = 0, streams = 0
  const firstSaved = chapter('chapter-a', { content: '首轮已保存续写稿' })
  const lastSaved = { ...firstSaved, content: '第二轮完整已保存稿' }
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { listChapterMetadata: async () => {
    if (++reads === 1) return [metadataFixture(chapter())]
    if (reads === 2) return oldDirectory.promise
    return [metadataFixture(lastSaved)]
  } }, streamSSE: async (_url, _data, handlers) => {
    if (++streams === 1) { context.database.set(firstSaved.id, firstSaved); await handlers.onDone({ chapter: firstSaved }) }
    else { handlers.onToken('第二轮tokens'); await nextFinish.promise; context.database.set(lastSaved.id, lastSaved); await handlers.onDone({ chapter: lastSaved }) }
  } })
  const first = context.vm.doContinue(); await flush()
  await context.vm.startNew(true); await context.vm.selectChapter({ id: 'chapter-a', novel_id: 'novel-a' }, true)
  const second = context.vm.doContinue(); await flush()
  try {
    oldDirectory.resolve([metadataFixture(firstSaved)]); await first
    assert.equal(context.vm.streamText.value, '第二轮tokens'); assert.equal(context.vm.streaming.value, true)
    nextFinish.resolve(); await second
    assert.equal(context.vm.content.value, lastSaved.content); assert.equal(context.vm.selectedDetail.value.content, lastSaved.content)
    assert.equal(context.vm.savedSnapshot.value.content, lastSaved.content)
  } finally { nextFinish.resolve(); await second; context.dispose() }
})

test('新章与续写已保存但目录失败仍采用真实完成稿，不报告正文未保存', async () => {
  for (const operation of ['generate', 'continue']) {
    let reads = 0
    const saved = chapter(operation === 'generate' ? 'generated' : 'chapter-a', { content: '真实完成已保存稿', summary: '', tension_score: null, drift_score: null })
    const context = await panel('WritingPanel', { props: operation === 'continue' ? { initialChapterId: 'chapter-a' } : {}, api: {
      listChapterMetadata: async () => { if (++reads > 1) throw new Error('目录刷新失败'); return [metadataFixture(chapter())] },
    }, streamSSE: async (_url, _data, handlers) => { handlers.onToken('真实完成已保存稿'); await handlers.onDone({ chapter: saved }) } })
    if (operation === 'generate') await context.vm.generateChapter(); else await context.vm.doContinue()
    assert.equal(context.vm.selectedId.value, saved.id); assert.equal(context.vm.content.value, saved.content)
    assert.equal(context.vm.savedSnapshot.value.content, saved.content); assert.equal(context.vm.isDirty.value, false)
    assert.equal(context.vm.streaming.value, false); assert.ok(context.messages.some((entry) => /已保存.*列表刷新失败/.test(entry.text)))
    assert.equal(context.messages.some((entry) => /正文未保存|生成未完成/.test(entry.text)), false)
    assertMetadata(context.vm.chapters.value); context.dispose()
  }
})

test('删除真实成功后邻章详情失败不复活删除ID，正文保留为未保存草稿', async () => {
  const context = await panel('WritingPanel', { props: { initialChapterId: 'chapter-a' }, api: { getChapter: async (_id, target) => {
    if (target === 'chapter-a') return chapter()
    throw new Error('邻章读取失败')
  } } })
  await context.vm.deleteChapter()
  assert.equal(context.vm.chapters.value.some((row) => row.id === 'chapter-a'), false)
  assert.equal(context.vm.selectedId.value, null); assert.equal(context.vm.content.value, chapter().content)
  assert.equal(context.vm.isDirty.value, true); assert.equal(context.vm.selectedDetail.value, null)
  assert.ok(context.messages.some((entry) => /邻章读取失败/.test(entry.text)))
  context.dispose()
})

test('文风页目录不读正文，仅在选择章节时按需取详情前800字', async () => {
  const context = await panel('SettingsPanel', { rows: [chapter('chapter-a', { content: '样'.repeat(1000) })] })
  assertMetadata(context.vm.chapters.value); assert.equal(context.requests.some((entry) => entry.name === 'getChapter'), false)
  assert.equal(context.vm.styleAnchor.value, '已保存文风')
  await context.vm.pickAnchorChapter('chapter-a')
  assert.equal(context.vm.styleAnchor.value, '样'.repeat(800)); assert.equal(context.vm.dirty.value, true)
  assert.equal(context.requests.filter((entry) => entry.name === 'getChapter').length, 1)
  context.dispose()
})

test('文风章节快速选择ABA只采用最后详情，旧完成不结束新读取', async () => {
  const old = deferred(), latest = deferred(); let calls = 0
  const context = await panel('SettingsPanel', { api: { getChapter: async () => ++calls === 1 ? old.promise : latest.promise } })
  const first = context.vm.pickAnchorChapter('chapter-a')
  context.vm.pickAnchorChapter(null)
  const second = context.vm.pickAnchorChapter('chapter-a')
  old.resolve(chapter('chapter-a', { content: '旧样本' })); await first
  assert.equal(context.vm.styleAnchor.value, '已保存文风'); assert.equal(context.vm.loadingAnchor.value, true)
  latest.resolve(chapter('chapter-a', { content: '最新样本' })); await second
  assert.equal(context.vm.styleAnchor.value, '最新样本'); assert.equal(context.vm.loadingAnchor.value, false)
  context.dispose()
})

test('文风章节读取失败或身份不匹配保留原基准，清除选择后旧正文不回写', async () => {
  for (const outcome of ['failure', 'identity', 'clear']) {
    const pending = deferred()
    const context = await panel('SettingsPanel', { api: { getChapter: async () => pending.promise } })
    const picking = context.vm.pickAnchorChapter('chapter-a')
    if (outcome === 'clear') await context.vm.pickAnchorChapter(null)
    if (outcome === 'failure') pending.reject(new Error('文风详情失败'))
    else pending.resolve(chapter(outcome === 'identity' ? 'chapter-b' : 'chapter-a', { content: '晚到正文' }))
    await picking
    assert.equal(context.vm.styleAnchor.value, '已保存文风'); assert.equal(context.vm.loadingAnchor.value, false)
    assert.equal(context.messages.length, outcome === 'clear' ? 0 : 1)
    context.dispose()
  }
})

test('文风详情不覆盖手动输入或保存中新编辑，跨作品ABA与卸载旧错误静默', async () => {
  for (const change of ['manual', 'save', 'novel', 'unmount']) {
    const pending = deferred()
    const context = await panel('SettingsPanel', { api: { getChapter: async () => pending.promise } })
    const picking = context.vm.pickAnchorChapter('chapter-a')
    if (change === 'manual') { context.vm.styleAnchor.value = '短暂编辑'; context.vm.styleAnchor.value = '已保存文风' }
    if (change === 'save') { await context.vm.saveAnchor(); context.vm.styleAnchor.value = '保存之后的新编辑' }
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await flush() }
    if (change === 'unmount') context.dispose()
    const before = context.vm.styleAnchor.value, messages = context.messages.length
    if (change === 'novel' || change === 'unmount') pending.reject(new Error('旧文风读取失败'))
    else pending.resolve(chapter('chapter-a', { content: '旧章节正文' }))
    await picking
    assert.equal(context.vm.styleAnchor.value, before); assert.equal(context.messages.length, messages)
    if (change !== 'unmount') context.dispose()
  }
})

test('章生成卡与伏笔跟踪只读取元信息，显示与默认选章无需详情', async () => {
  for (const name of ['ChapterWriteCard', 'ForeshadowingTracking']) {
    const context = await panel(name, { props: { conversationId: 'conversation-a', messageId: 'message-a', sourceText: '章前构思' } })
    assert.equal(context.requests.filter((entry) => entry.name === 'listChapterMetadata').length, 1)
    assert.equal(context.requests.some((entry) => entry.name === 'getChapter' || entry.name === 'listChapters'), false)
    if (name === 'ChapterWriteCard') assert.equal(context.vm.previousChapterId.value, 'chapter-b')
    else { assertMetadata(context.vm.chapters.value); assert.equal(context.vm.currentChapterId.value, 'chapter-b') }
    context.dispose()
  }
})
