import { splitChapterFixtures, memoryStorage } from './chapter-fixtures.mjs'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencyUrls = Object.fromEntries(await Promise.all([
  ['../types', '../src/types.ts'], ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'],
  ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../utils/generationConsistency', '../src/utils/generationConsistency.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const settle = async () => { for (let i = 0; i < 20; i += 1) { await Promise.resolve(); await nextTick() } }
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const chapter = (novelId = 'novel-a', patch = {}) => ({ id: `${novelId}-chapter`, novel_id: novelId, seq: 1,
  title: `${novelId}标题`, directive: '', content: `${novelId}正文`, status: 'finalized', summary: '原摘要',
  tension_score: 8, drift_score: 0.2, outline_node_id: `${novelId}-outline`, ...patch })
let sequence = 0

async function writing(options = {}) {
  const props = reactive({ novelId: 'novel-a', ...options.props })
  const messages = [], requests = [], emitted = [], hooks = {}, listeners = new Map()
  globalThis.window = { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  globalThis.localStorage = memoryStorage()
  const implementations = {
    listChapterMetadata: async (id) => [chapter(id)],
    createChapter: async (id, data) => chapter(id, { ...data, id: 'created-chapter', status: 'draft', summary: '', tension_score: null, drift_score: null }),
    updateChapter: async (id, chapterId, data) => chapter(id, { ...data, id: chapterId,
      ...(['title', 'content', 'directive'].some((field) => field in data) ? { status: 'draft', summary: '', tension_score: null, drift_score: null } : {}),
    }),
    deleteChapter: async () => {},
    scanCliche: async () => ({ hits: [], total: 0, density: 0, by_category: {} }),
    checkConsistency: async () => ({ conflicts: [] }),
    checkConsistencyDeep: async () => ({ conflicts: [], chunks_checked: 1, chunks_skipped: 0, truncated: false }),
    checkChapterOutline: async () => ({ issues: [] }),
    ...options.api,
  }
  const harness = {
    api: Object.fromEntries(Object.entries(splitChapterFixtures(implementations, (id, chapterId) => chapter(id, { id: chapterId }))).map(([name, fn]) => [name, (...args) => {
      requests.push({ name, args: structuredClone(args) }); return fn(...args)
    }])),
    streamSSE: options.streamSSE ?? (async () => { throw new Error('手写流程不得调用模型') }),
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const component of ['NButton', 'NCheckbox', 'NEmpty', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NModal', 'NSelect', 'NTag']) harness[component] = component.toLowerCase()
  globalThis.__manualWriting = harness
  const descriptor = parse(await readFile(new URL('../src/components/WritingPanel.vue', import.meta.url), 'utf8')).descriptor
  const compiled = compileScript(descriptor, { id: `manual-${++sequence}` })
  let source = 'const __harness = globalThis.__manualWriting\n' + compiled.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamSSE \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1 = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  for (const [name, url] of Object.entries(dependencyUrls)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source) + `\n// ${sequence}`))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  await hooks.mount?.(); await settle()
  const template = compileTemplate({ source: descriptor.template.content, id: `manual-${sequence}`, filename: 'WritingPanel.vue', compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.deepEqual(template.errors, [])
  const rendering = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${sequence}`))
  return { vm, props, messages, requests, emitted, listeners,
    render: () => rendering.render({}, [], props, proxyRefs(vm), {}, {}),
    dispose: () => { hooks.unmount?.(); scope.stop() },
  }
}

function descendants(node, predicate, found = []) {
  if (node && typeof node === 'object') {
    if (predicate(node)) found.push(node)
    if (Array.isArray(node.children)) for (const child of node.children) descendants(child, predicate, found)
  }
  return found
}

test('空作品真实模板可手写保存草稿，不调用模型且新文本受离开与刷新保护', async () => {
  const context = await writing({ api: { listChapterMetadata: async () => [] } })
  const { vm } = context
  assert.equal(descendants(context.render(), (node) => node.type === 'textarea' && node.props?.class === 'content-editor').length, 1)
  vm.title.value = '离线手写章'; vm.directive.value = '作者自己的方向'; vm.content.value = '断网也能写的新正文'
  assert.equal(vm.isDirty.value, true)
  await settle()
  assert.equal(context.emitted.some(([name, value]) => name === 'dirty-change' && value), true)
  let prevented = false
  context.listeners.get('beforeunload')({ preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  const leaving = vm.startNew(); await settle()
  assert.equal(vm.showDiscard.value, true); vm.finishDiscard(false); await leaving
  assert.equal(vm.content.value, '断网也能写的新正文')
  await vm.saveContent()
  const request = context.requests.find((entry) => entry.name === 'createChapter')
  const { request_id, ...payload } = request.args[1]
  assert.match(request_id, /^[0-9a-f-]{36}$/)
  assert.deepEqual([request.args[0], payload], ['novel-a', { title: '离线手写章', directive: '作者自己的方向', content: '断网也能写的新正文', status: 'draft' }])
  assert.equal(vm.selectedId.value, 'created-chapter')
  assert.equal(vm.isDirty.value, false)
  assert.equal(context.requests.some((entry) => entry.name === 'updateChapter'), false)
  context.dispose()
})

test('新章保存挂起保留新输入并防双击，刷新失败后下一次保存更新已创建身份', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ api: {
    listChapterMetadata: async () => { if (++reads > 1) throw new Error('刷新失败'); return [] },
    createChapter: async () => pending.promise,
  } })
  const { vm } = context
  vm.title.value = '提交标题'; vm.content.value = '提交正文'
  const saving = vm.saveContent(); await vm.saveContent()
  vm.title.value = '新标题'; vm.directive.value = '新指令'; vm.content.value = '保存中继续手写'
  pending.resolve(chapter('novel-a', { id: 'created-chapter', title: '提交标题', content: '提交正文', status: 'draft', summary: '', tension_score: null, drift_score: null }))
  await saving
  assert.equal(context.requests.filter((entry) => entry.name === 'createChapter').length, 1)
  assert.equal(vm.selectedId.value, 'created-chapter')
  assert.equal(vm.content.value, '保存中继续手写')
  assert.equal(vm.isDirty.value, true)
  assert.equal(vm.selectedChapter.value.status, 'draft')
  assert.match(context.messages[0].text, /已保存.*刷新失败/)
  await vm.saveContent()
  const update = context.requests.find((entry) => entry.name === 'updateChapter')
  assert.equal(update.args[1], 'created-chapter'); assert.equal(update.args[2].content, '保存中继续手写')
  assert.equal(vm.isDirty.value, false)
  context.dispose()
})

test('已有章保存不覆盖等待期间新编辑，采用真实返回的草稿与清空派生字段', async () => {
  const pending = wait()
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { updateChapter: async () => pending.promise } })
  const { vm } = context
  vm.content.value = '提交正文'; const saving = vm.saveContent()
  vm.content.value = '尚未保存的新正文'
  pending.resolve(chapter('novel-a', { content: '提交正文', status: 'draft', summary: '', tension_score: null, drift_score: null }))
  await saving
  assert.equal(vm.content.value, '尚未保存的新正文')
  assert.equal(vm.savedSnapshot.value.content, '提交正文')
  assert.equal(vm.isDirty.value, true)
  assert.equal(vm.selectedChapter.value.status, 'draft')
  assert.equal(vm.selectedChapter.value.summary, '')
  assert.equal(vm.selectedChapter.value.tension_score, null)
  assert.equal(vm.selectedChapter.value.drift_score, null)
  context.dispose()
})

test('新建与普通保存失败保留作者文本及脏状态', async () => {
  for (const existing of [false, true]) {
    const context = await writing({ props: existing ? { initialChapterId: 'novel-a-chapter' } : {}, api: {
      createChapter: async () => { throw new Error('本地保存失败') }, updateChapter: async () => { throw new Error('本地保存失败') },
    } })
    context.vm.title.value = '未保存标题'; context.vm.content.value = '不得丢失的正文'
    await context.vm.saveContent()
    assert.equal(context.vm.content.value, '不得丢失的正文'); assert.equal(context.vm.isDirty.value, true)
    assert.equal(context.vm.saving.value, false); assert.match(context.messages[0].text, /本地保存失败/)
    context.dispose()
  }
})

test('保存旧来源在作品A→B→A、章节离开返回及卸载后均不回写或提示', async () => {
  for (const switching of ['novel', 'chapter', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { updateChapter: async () => pending.promise } })
    const { vm } = context
    vm.content.value = '旧提交'; const saving = vm.saveContent()
    if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (switching === 'chapter') { await vm.startNew(true); await vm.selectChapter(chapter(), true) }
    if (switching === 'unmount') context.dispose()
    vm.content.value = '返回后的作者输入'
    if (outcome === 'success') pending.resolve(chapter('novel-a', { content: '旧提交' }))
    else pending.reject(new Error('旧保存失败'))
    await saving
    assert.equal(vm.content.value, '返回后的作者输入'); assert.equal(vm.isDirty.value, true)
    assert.equal(context.messages.length, 0); assert.equal(vm.saving.value, false)
    if (switching !== 'unmount') context.dispose()
  }
})

test('新章同为null身份时放弃并另建也使旧POST失效，不把新稿绑定旧创建结果', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ api: { listChapterMetadata: async () => [], createChapter: async () => pending.promise } })
    context.vm.content.value = '旧草稿'; const saving = context.vm.saveContent()
    await context.vm.startNew(true); context.vm.content.value = '作者明确另开的新草稿'
    if (outcome === 'success') pending.resolve(chapter('novel-a', { id: 'old-created', content: '旧草稿' }))
    else pending.reject(new Error('旧POST失败'))
    await saving
    assert.equal(context.vm.selectedId.value, null); assert.equal(context.vm.content.value, '作者明确另开的新草稿')
    assert.equal(context.vm.isDirty.value, true); assert.equal(context.messages.length, 0)
    context.dispose()
  }
})

test('旧保存完成不会停止另一来源正在保存的新稿', async () => {
  const first = wait(), second = wait(); let calls = 0
  const context = await writing({ api: { listChapterMetadata: async () => [], createChapter: async () => ++calls === 1 ? first.promise : second.promise } })
  context.vm.content.value = '旧草稿'; const oldSaving = context.vm.saveContent()
  await context.vm.startNew(true); context.vm.content.value = '新草稿'; const newSaving = context.vm.saveContent()
  first.resolve(chapter('novel-a', { id: 'old-created', content: '旧草稿' })); await oldSaving
  assert.equal(context.vm.saving.value, true); assert.equal(context.messages.length, 0)
  second.resolve(chapter('novel-a', { id: 'new-created', content: '新草稿' })); await newSaving
  assert.equal(context.vm.saving.value, false); assert.equal(context.vm.selectedId.value, 'new-created')
  assert.equal(context.vm.isDirty.value, false)
  context.dispose()
})

test('同正文重复规则/套路检查只接受最新请求', async () => {
  for (const [action, method, field, latest, old] of [
    ['doConsistency', 'checkConsistency', 'conflicts', { conflicts: [{ reason: '最新冲突' }] }, { conflicts: [{ reason: '旧冲突' }] }],
    ['doScanCliche', 'scanCliche', 'cliche', { total: 0, hits: [], density: 0, by_category: {} }, { total: 1, hits: [], density: 1, by_category: {} }],
  ]) {
    const first = wait(), second = wait(); let calls = 0
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { [method]: async () => ++calls === 1 ? first.promise : second.promise } })
    const oldChecking = context.vm[action](), newChecking = context.vm[action]()
    second.resolve(latest); await newChecking
    first.resolve(old); await oldChecking
    assert.deepEqual(context.vm[field].value, field === 'conflicts' ? latest.conflicts : latest)
    assert.equal(context.messages.length, 1)
    context.dispose()
  }
})

for (const [action, method, field, result] of [
  ['doScanCliche', 'scanCliche', 'cliche', { total: 1, density: 1, hits: [{ start: 0, end: 2 }], by_category: {} }],
  ['doConsistency', 'checkConsistency', 'conflicts', { conflicts: [{ start: 0, end: 2, reason: '旧冲突' }] }],
  ['doDeepCheck', 'checkConsistencyDeep', 'deepMeta', { conflicts: [{ excerpt: '旧正文' }], chunks_checked: 1, chunks_skipped: 0, truncated: false }],
  ['doOutlineCheck', 'checkChapterOutline', 'outlineCheckResult', { issues: [{ description: '旧大纲结果' }] }],
]) {
  test(`${action} 实际SFC丢弃正文ABA、作品ABA、章节ABA与卸载的旧成功及错误`, async () => {
    for (const switching of ['text', 'novel', 'chapter', 'unmount']) for (const outcome of ['success', 'failure']) {
      const pending = wait()
      const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { [method]: async () => pending.promise } })
      const { vm } = context; const checking = vm[action]()
      if (switching === 'text') { const text = vm.content.value; vm.content.value = '临时编辑'; vm.content.value = text }
      if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
      if (switching === 'chapter') { await vm.startNew(true); await vm.selectChapter(chapter(), true) }
      if (switching === 'unmount') context.dispose()
      if (outcome === 'success') pending.resolve(result); else pending.reject(new Error('旧检查失败'))
      await checking
      assert.deepEqual(vm[field].value, field === 'conflicts' ? [] : null)
      assert.equal(context.messages.length, 0)
      if (switching !== 'unmount') context.dispose()
    }
  })
}

test('旧深度与大纲检查完成不会停止新正文正在进行的检查', async () => {
  for (const [action, method, loading, result] of [
    ['doDeepCheck', 'checkConsistencyDeep', 'deepLoading', { conflicts: [], chunks_checked: 1, chunks_skipped: 0, truncated: false }],
    ['doOutlineCheck', 'checkChapterOutline', 'outlineCheckLoading', { issues: [] }],
  ]) {
    const first = wait(), second = wait(); let calls = 0
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { [method]: async () => ++calls === 1 ? first.promise : second.promise } })
    const oldChecking = context.vm[action]()
    context.vm.content.value = '修改后的新正文'
    const newChecking = context.vm[action]()
    first.resolve(result); await oldChecking
    assert.equal(context.vm[loading].value, true)
    assert.equal(context.messages.length, 0)
    second.resolve(result); await newChecking
    assert.equal(context.vm[loading].value, false)
    assert.equal(context.messages.length, 1)
    context.dispose()
  }
})

test('新章等待目录初始读取时手写不被迟到的初始章节选择覆盖', async () => {
  const pending = wait()
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => pending.promise } })
  context.vm.content.value = '先手写的新章'; pending.resolve([chapter()]); await settle()
  assert.equal(context.vm.selectedId.value, null)
  assert.equal(context.vm.content.value, '先手写的新章')
  assert.equal(context.vm.isDirty.value, true)
  context.dispose()
})

test('目录ABA与同作品乱序读取不倒退，空作品来源清空旧目录和编辑器', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ api: { listChapterMetadata: async (id) => ++reads === 1 ? pending.promise : [chapter(id, { title: '当前目录' })] } })
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  pending.resolve([chapter('novel-a', { title: '旧目录' })]); await settle()
  assert.equal(context.vm.chapters.value[0].title, '当前目录')
  const old = wait(), newest = wait(); reads = 0
  // 独立实例保持同作品的旧读取不会覆盖最近响应。
  const ordered = await writing({ api: { listChapterMetadata: async () => ++reads === 1 ? [chapter()] : reads === 2 ? old.promise : newest.promise } })
  const readingOld = ordered.vm.loadChapters(), readingNew = ordered.vm.loadChapters()
  newest.resolve([chapter('novel-a', { title: '新目录' })]); await readingNew
  old.resolve([chapter('novel-a', { title: '过时目录' })]); await readingOld
  assert.equal(ordered.vm.chapters.value[0].title, '新目录')
  await ordered.vm.selectChapter(chapter(), true)
  ordered.props.novelId = ''; await settle()
  assert.deepEqual(ordered.vm.chapters.value, []); assert.equal(ordered.vm.selectedId.value, null); assert.equal(ordered.vm.content.value, '')
  context.dispose(); ordered.dispose()
})

test('摘要保存冻结来源，关闭重开或作品/章节ABA及卸载不接受旧响应', async () => {
  for (const switching of ['modal', 'novel', 'chapter', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { updateChapter: async () => pending.promise } })
    const { vm } = context
    vm.openSummary(); vm.summaryDraft.value = '旧提交摘要'; const saving = vm.saveSummary()
    if (switching === 'modal') { vm.showSummary.value = false; vm.openSummary() }
    if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle(); vm.openSummary() }
    if (switching === 'chapter') { await vm.startNew(true); await vm.selectChapter(chapter(), true); vm.openSummary() }
    if (switching === 'unmount') context.dispose()
    vm.summaryDraft.value = '当前作者摘要'
    if (outcome === 'success') pending.resolve(chapter('novel-a', { summary: '旧提交摘要' })); else pending.reject(new Error('旧摘要失败'))
    await saving
    assert.equal(vm.summaryDraft.value, '当前作者摘要'); assert.equal(context.messages.length, 0); assert.equal(vm.summarySaving.value, false)
    if (switching !== 'unmount') context.dispose()
  }
})

test('摘要保存中新输入保留为未保存，旧完成不停止下一来源的保存', async () => {
  const pending = wait()
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { updateChapter: async () => pending.promise } })
  context.vm.openSummary(); context.vm.summaryDraft.value = '提交摘要'; const saving = context.vm.saveSummary()
  context.vm.summaryDraft.value = '随后编辑摘要'
  pending.resolve(chapter('novel-a', { summary: '提交摘要' })); await saving
  assert.equal(context.vm.summaryDraft.value, '随后编辑摘要'); assert.equal(context.vm.savedSummary.value, '提交摘要'); assert.equal(context.vm.isSummaryDirty.value, true)
  context.dispose()
})

test('旧摘要请求完成不会停止关闭重开后新摘要的保存', async () => {
  const first = wait(), second = wait(); let calls = 0
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { updateChapter: async () => ++calls === 1 ? first.promise : second.promise } })
  context.vm.openSummary(); context.vm.summaryDraft.value = '旧摘要'; const oldSaving = context.vm.saveSummary()
  context.vm.showSummary.value = false; context.vm.openSummary(); context.vm.summaryDraft.value = '新摘要'; const newSaving = context.vm.saveSummary()
  first.resolve(chapter('novel-a', { summary: '旧摘要' })); await oldSaving
  assert.equal(context.vm.summarySaving.value, true); assert.equal(context.messages.length, 0)
  second.resolve(chapter('novel-a', { summary: '新摘要' })); await newSaving
  assert.equal(context.vm.summarySaving.value, false); assert.equal(context.vm.savedSummary.value, '新摘要')
  context.dispose()
})

test('已有手写正文未保存时AI生成入口提示保存，不调用模型覆盖新稿', async () => {
  const context = await writing({ api: { listChapterMetadata: async () => [] } })
  context.vm.content.value = '必须保留的手写正文'; await context.vm.doWrite()
  assert.equal(context.vm.content.value, '必须保留的手写正文'); assert.equal(context.vm.streaming.value, false)
  assert.match(context.messages[0].text, /请先保存/)
  context.dispose()
})

test('普通AI新章真实并发冲突保留精确输出与作者新稿，可复制且导航和刷新受保护', async () => {
  const pending=wait();let handlers
  const context=await writing({api:{listChapterMetadata:async()=>[]},streamSSE:async(_url,_payload,h)=>{
    handlers=h;h.onToken(' 😀候选\u0000\r\n ');await pending.promise
    await h.onError('生成期间最新章节发生变化，本次候选未保存','chapter_write_conflict',409)
  }})
  const generating=context.vm.doWrite();await settle()
  context.vm.content.value='等待期间作者的新稿';context.vm.title.value='新标题'
  pending.resolve();await generating
  assert.equal(context.vm.streamText.value,' 😀候选\u0000\r\n ')
  assert.equal(context.vm.content.value,'等待期间作者的新稿');assert.equal(context.vm.title.value,'新标题')
  assert.equal(context.vm.selectedId.value,null);assert.equal(context.vm.streaming.value,false)
  assert.equal(context.vm.streamUnconfirmed.value,false);assert.equal(context.vm.hasIncompleteOutput.value,true)
  assert.ok(descendants(context.render(),node=>node.type==='textarea'&&node.props?.['aria-label']==='未完成稿').length)
  const copied=[];Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>copied.push(text)}}})
  await context.vm.copyIncompleteOutput();assert.deepEqual(copied,[' 😀候选\u0000\r\n '])
  let prevented=false;context.listeners.get('beforeunload')({preventDefault(){prevented=true}});assert.equal(prevented,true)
  assert.equal(context.requests.some(row=>['createChapter','updateChapter'].includes(row.name)),false)
  context.dispose()
})

test('普通新章旧冲突在作品切换、ABA与卸载后不改变当前新稿或新一轮状态', async () => {
  for(const changed of ['novel','aba','unmount']) {
    const pending=wait();const context=await writing({api:{listChapterMetadata:async()=>[]},streamSSE:async(_url,_payload,h)=>{
      h.onToken('旧作品候选');await pending.promise;h.onToken('旧迟到token');await h.onError('旧作品冲突','chapter_write_conflict',409)
    }})
    const generating=context.vm.doWrite();await settle()
    if(changed==='unmount')context.dispose();else{context.props.novelId='novel-b';await settle();if(changed==='aba'){context.props.novelId='novel-a';await settle()}}
    const stateBeforeLate={output:context.vm.streamText.value,streaming:context.vm.streaming.value}
    context.vm.content.value='当前作者新稿';context.vm.title.value='当前标题'
    pending.resolve();await generating
    assert.equal(context.vm.content.value,'当前作者新稿');assert.equal(context.vm.title.value,'当前标题')
    assert.equal(context.messages.length,0);assert.equal(context.vm.streamText.value,stateBeforeLate.output)
    assert.equal(context.vm.streaming.value,stateBeforeLate.streaming)
    if(changed!=='unmount')context.dispose()
  }
})

test('删除迟到不重选或丢弃新作品正文，ABA与卸载不提示旧错误', async () => {
  for (const switching of ['novel', 'chapter', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { deleteChapter: async () => pending.promise } })
    const { vm } = context; const deleting = vm.deleteChapter()
    if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (switching === 'chapter') { await vm.startNew(true); await vm.selectChapter(chapter(), true) }
    if (switching === 'unmount') context.dispose()
    vm.content.value = '新的作者正文'
    if (outcome === 'success') pending.resolve(); else pending.reject(new Error('旧删除失败'))
    await deleting
    assert.equal(vm.content.value, '新的作者正文'); assert.equal(context.messages.length, 0); assert.equal(vm.deleting.value, false)
    assert.equal(context.requests.find((entry) => entry.name === 'deleteChapter').args[0], 'novel-a')
    if (switching !== 'unmount') context.dispose()
  }
})

test('删除成功刷新失败先移除真实已删章，挂起旧目录不能复活记录', async () => {
  const old = wait(); let reads = 0
  const remaining = chapter('novel-a', { id: 'remaining', seq: 2 })
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => ++reads === 1 ? [chapter(), remaining] : reads === 2 ? old.promise : Promise.reject(new Error('刷新失败')) } })
  const oldReading = context.vm.loadChapters(); await context.vm.deleteChapter()
  assert.deepEqual(context.vm.chapters.value.map((item) => item.id), ['remaining'])
  assert.equal(context.vm.selectedId.value, 'remaining')
  assert.match(context.messages[0].text, /已删除.*刷新失败/)
  old.resolve([chapter(), remaining]); await oldReading
  assert.deepEqual(context.vm.chapters.value.map((item) => item.id), ['remaining'])
  context.dispose()
})

test('删除过程中新的作者输入保留为新章草稿，不被下一章强制覆盖', async () => {
  const pending = wait()
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { deleteChapter: async () => pending.promise, listChapterMetadata: async () => [chapter()] } })
  const deleting = context.vm.deleteChapter(); context.vm.content.value = '等待删除时新写的文字'
  pending.resolve(); await deleting
  assert.equal(context.vm.selectedId.value, null)
  assert.equal(context.vm.content.value, '等待删除时新写的文字'); assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.vm.chapters.value.some((item) => item.id === 'novel-a-chapter'), false)
  context.dispose()
})

async function rewriteContext(api = {}) {
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api,
    streamSSE: async (_url, payload, callbacks) => {
      callbacks.onToken('候选段落')
      const source = payload.expected_author_fields, points = Array.from(source.content)
      await callbacks.onDone({ type: 'done', preview: true,
        content: points.slice(0, payload.selection_start).join('') + '候选段落' + points.slice(payload.selection_end).join(''),
        chapter: chapter('novel-a', source), expected_author_fields: { ...source },
        selection_start: payload.selection_start, selection_end: payload.selection_end })
    },
  })
  context.vm.contentRef.value = { selectionStart: 0, selectionEnd: 2 }
  await context.vm.doRewrite()
  assert.equal(context.vm.showPreview.value, true)
  return context
}

test('改写预览绑定生成章与作者源文本，切章或正文ABA后不能写入当前章', async () => {
  for (const switching of ['text', 'chapter', 'novel']) {
    const context = await rewriteContext()
    const oldText = context.vm.content.value
    if (switching === 'text') { context.vm.content.value = '新编辑'; context.vm.content.value = oldText }
    if (switching === 'chapter') await context.vm.selectChapter(chapter('novel-a', { id: 'other-chapter' }), true)
    if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    await context.vm.applyPreview()
    assert.equal(context.requests.some((entry) => entry.name === 'updateChapter'), false)
    assert.equal(context.vm.showPreview.value, false)
    context.dispose()
  }
})

test('改写确认等待新输入保留，双击不重复且刷新失败保留真实保存基线', async () => {
  const pending = wait(); let reads = 0
  const context = await rewriteContext({ updateChapter: async () => pending.promise, listChapterMetadata: async () => ++reads === 1 ? [chapter()] : Promise.reject(new Error('刷新失败')) })
  const candidate = context.vm.previewFullContent.value
  const applying = context.vm.applyPreview(); await context.vm.applyPreview()
  context.vm.title.value = '新作者标题'; context.vm.content.value = '确认过程中继续手写'
  pending.resolve(chapter('novel-a', { content: candidate, status: 'draft', summary: '', tension_score: null, drift_score: null })); await applying
  assert.equal(context.requests.filter((entry) => entry.name === 'updateChapter').length, 1)
  assert.equal(context.vm.content.value, '确认过程中继续手写'); assert.equal(context.vm.title.value, '新作者标题')
  assert.equal(context.vm.savedSnapshot.value.content, candidate); assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.vm.selectedChapter.value.status, 'draft'); assert.match(context.messages[0].text, /已.*改写.*刷新失败/)
  context.dispose()
})

test('改写确认旧来源成功/失败以及卸载不得回写当前正文或提示', async () => {
  for (const switching of ['chapter', 'novel', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = wait(); const context = await rewriteContext({ updateChapter: async () => pending.promise })
    const candidate = context.vm.previewFullContent.value
    const applying = context.vm.applyPreview()
    if (switching === 'chapter') { await context.vm.startNew(true); await context.vm.selectChapter(chapter(), true) }
    if (switching === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (switching === 'unmount') context.dispose()
    context.vm.content.value = '新的当前稿'
    if (outcome === 'success') pending.resolve(chapter('novel-a', { content: candidate })); else pending.reject(new Error('旧改写失败'))
    await applying
    assert.equal(context.vm.content.value, '新的当前稿'); assert.equal(context.messages.length, 0); assert.equal(context.vm.previewApplying.value, false)
    if (switching !== 'unmount') context.dispose()
  }
})

test('恢复回调只接受本次版本面板来源，晚到刷新保留恢复期间的新编辑', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => ++reads === 1 ? [chapter()] : pending.promise } })
  context.vm.openRevisionHistory()
  const restored = chapter('novel-a', { content: '恢复版本正文', status: 'draft' })
  const restoring = context.vm.onRestored(restored)
  context.vm.content.value = '恢复过程中作者又写的新稿'
  pending.resolve([restored]); await restoring
  assert.equal(context.vm.content.value, '恢复过程中作者又写的新稿')
  assert.equal(context.vm.savedSnapshot.value.content, '恢复版本正文'); assert.equal(context.vm.isDirty.value, true)
  await context.vm.startNew(true); await context.vm.selectChapter(chapter(), true)
  context.vm.content.value = '离开返回后的正文'
  await context.vm.onRestored(restored)
  assert.equal(context.vm.content.value, '离开返回后的正文')
  context.dispose()
})

test('下一章大纲迟到的成功/失败不打开其他来源对话框，旧对话不能确认生成', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listOutline: async () => pending.promise } })
    const preparing = context.vm.doWriteNext()
    context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
    if (outcome === 'success') pending.resolve([{ id: 'old-outline', level: 'chapter', title: '旧大纲' }]); else pending.reject(new Error('旧大纲错误'))
    await preparing; await context.vm.confirmWriteNext()
    assert.equal(context.vm.showWriteNext.value, false); assert.equal(context.messages.length, 0)
    context.dispose()
  }
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listOutline: async () => [] } })
  await context.vm.doWriteNext(); assert.equal(context.vm.showWriteNext.value, true)
  context.vm.directive.value = '刚改的新指令'; await context.vm.confirmWriteNext()
  assert.equal(context.vm.streaming.value, false); assert.equal(context.vm.showWriteNext.value, false)
  context.dispose()
})

test('同作品删除等待换到另一章后仍移除已删记录，并保持当前作者新稿', async () => {
  const pending = wait(), second = chapter('novel-a', { id: 'second', seq: 2, content: '第二章正文' })
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => [chapter(), second], deleteChapter: async () => pending.promise } })
  const deleting = context.vm.deleteChapter(); await context.vm.selectChapter(second, true)
  context.vm.content.value = '第二章新增稿'; pending.resolve(); await deleting
  assert.equal(context.vm.selectedId.value, 'second'); assert.equal(context.vm.content.value, '第二章新增稿')
  assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.vm.chapters.value.some((item) => item.id === 'novel-a-chapter'), false)
  assert.equal(context.messages.length, 0)
  context.dispose()
})

test('摘要保存成功使保存前的目录读取失效，不把已保存摘要倒退', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => ++reads === 1 ? [chapter()] : pending.promise } })
  const oldReading = context.vm.loadChapters()
  context.vm.openSummary(); context.vm.summaryDraft.value = '已保存新摘要'; await context.vm.saveSummary()
  pending.resolve([chapter()]); await oldReading
  assert.equal(context.vm.selectedChapter.value.summary, '已保存新摘要')
  context.dispose()
})

test('恢复后目录刷新挂起期间显式保存，旧恢复回调不能把已保存正文再覆盖', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ props: { initialChapterId: 'novel-a-chapter' }, api: { listChapterMetadata: async () => ++reads === 2 ? pending.promise : [chapter()] } })
  context.vm.openRevisionHistory()
  const restored = chapter('novel-a', { content: '恢复版本正文' })
  const restoring = context.vm.onRestored(restored)
  // 编辑器尚未换成恢复正文，作者明确保存当前文本，不需要再输入相同字符。
  await context.vm.saveContent()
  pending.resolve([restored]); await restoring
  assert.equal(context.vm.content.value, 'novel-a正文')
  assert.equal(context.vm.savedSnapshot.value.content, 'novel-a正文')
  assert.equal(context.vm.isDirty.value, false)
  context.dispose()
})
