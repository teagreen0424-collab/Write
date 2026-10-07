import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const deps = Object.fromEntries(await Promise.all([
  ['../types', '../src/types.ts'], ['../utils/chapter', '../src/utils/chapter.ts'],
].map(async ([name, path]) => [name, url(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const settle = async () => { for (let i = 0; i < 12; i += 1) { await Promise.resolve(); await nextTick() } }
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const revision = (id = 'old-v1') => ({ id, revision_no: 1, title: '旧标题', content: '真实旧稿', word_count: 4, source: 'manual_save', status: 'draft' })
let sequence = 0

async function panel(overrides = {}) {
  const props = reactive({ show: true, novelId: 'work-a', chapterId: 'chapter-a', chapterTitle: '当前标题', currentContent: '当前正文', currentStatus: 'draft' })
  const messages = [], emitted = [], requests = [], hooks = {}
  const api = Object.fromEntries(Object.entries({
    listChapterRevisions: async () => [revision()],
    getChapterRevision: async (_work, _chapter, id) => revision(id),
    restoreChapterRevision: async () => ({ novel_id: 'work-a', id: 'chapter-a', content: '真实旧稿', status: 'draft' }),
    ...overrides,
  }).map(([name, fn]) => [name, (...args) => { requests.push({ name, args }); return fn(...args) }]))
  globalThis.__revisionUI = { api,
    useMessage: () => Object.fromEntries(['success', 'error', 'warning'].map((type) => [type, (text) => messages.push({ type, text })])),
    onBeforeUnmount: (fn) => { hooks.unmount = fn },
    ...Object.fromEntries(['NButton', 'NEmpty', 'NModal', 'NSpin', 'NTag'].map((name) => [name, name])),
  }
  const descriptor = parse(await readFile(new URL('../src/components/ChapterRevisionPanel.vue', import.meta.url), 'utf8')).descriptor
  let source = 'const __harness = globalThis.__revisionUI\n' + compileScript(descriptor, { id: `revision-${++sequence}` }).content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  for (const [name, module] of Object.entries(deps)) source = source.replaceAll(`from '${name}'`, `from '${module}'`)
  const component = (await import(url(compile(source) + `\n// ${sequence}`))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  await settle()
  if (!requests.some((item) => item.name === 'listChapterRevisions')) await vm.loadRevisions()
  return { vm, props, messages, emitted, requests, dispose: () => { hooks.unmount?.(); scope.stop() } }
}

test('版本列表丢弃关闭重开、作品ABA与章节ABA后的旧回应和错误', async () => {
  for (const switching of ['show', 'novelId', 'chapterId']) for (const outcome of ['success', 'failure']) {
    const pending = wait(); let calls = 0
    const context = await panel({ listChapterRevisions: async () => ++calls === 2 ? pending.promise : [revision('fresh')] })
    const loading = context.vm.loadRevisions()
    const initial = context.props[switching]
    context.props[switching] = switching === 'show' ? false : 'other'
    context.props[switching] = initial
    await settle()
    if (outcome === 'success') pending.resolve([revision('stale')]); else pending.reject(new Error('旧列表失败'))
    await loading
    assert.equal(context.vm.revisions.value.some((item) => item.id === 'stale'), false)
    assert.equal(context.messages.length, 0)
    context.dispose()
  }
})

test('历史详情快速改选时采用最后选择，旧请求不终止新请求或显示旧错误', async () => {
  for (const outcome of ['success', 'failure']) {
    const first = wait(), second = wait()
    const context = await panel({ listChapterRevisions: async () => [revision('v1'), revision('v2')],
      getChapterRevision: async (_work, _chapter, id) => id === 'v1' ? first.promise : second.promise })
    const a = context.vm.openDetail(revision('v1')), b = context.vm.openDetail(revision('v2'))
    if (outcome === 'success') first.resolve(revision('v1')); else first.reject(new Error('旧详情失败'))
    await a
    assert.equal(context.vm.detail.value, null)
    assert.equal(context.vm.detailLoading.value, true)
    assert.equal(context.messages.length, 0)
    second.resolve(revision('v2')); await b
    assert.equal(context.vm.detail.value.id, 'v2')
    assert.equal(context.vm.detailLoading.value, false)
    context.dispose()
  }
})

test('恢复已提交后即通知原章节，版本目录刷新失败仅提示后置失败', async () => {
  let reads = 0
  const context = await panel({ listChapterRevisions: async () => { if (++reads > 1) throw new Error('目录离线'); return [revision()] } })
  await context.vm.openDetail(revision()); context.vm.askRestore()
  await context.vm.confirmRestore()
  assert.equal(context.emitted.filter(([name]) => name === 'restored').length, 1)
  assert.equal(context.emitted.find(([name]) => name === 'restored')[1].content, '真实旧稿')
  assert.equal(context.messages.length, 1)
  assert.equal(context.messages[0].type, 'warning')
  assert.match(context.messages[0].text, /已恢复.*刷新失败/)
  assert.equal(context.vm.restoring.value, false)
  context.dispose()
})

test('恢复等待中禁止重复提交和关闭，外部切换来源后旧成功或失败保持静默', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await panel({ restoreChapterRevision: async () => pending.promise })
    await context.vm.openDetail(revision()); context.vm.askRestore()
    const restoring = context.vm.confirmRestore(); await context.vm.confirmRestore(); context.vm.close()
    assert.equal(context.requests.filter((item) => item.name === 'restoreChapterRevision').length, 1)
    assert.equal(context.emitted.length, 0)
    context.props.novelId = 'work-b'; context.props.novelId = 'work-a'
    await settle()
    if (outcome === 'success') pending.resolve({ novel_id: 'work-a', id: 'chapter-a', content: '真实旧稿' })
    else pending.reject(new Error('旧恢复失败'))
    await restoring
    assert.equal(context.emitted.length, 0)
    assert.equal(context.messages.length, 0)
    context.dispose()
  }
})

test('卸载后的历史详情与恢复回应不更新或提示', async () => {
  for (const action of ['detail', 'restore']) {
    const pending = wait()
    const context = await panel(action === 'detail' ? { getChapterRevision: async () => pending.promise } : { restoreChapterRevision: async () => pending.promise })
    if (action === 'restore') { await context.vm.openDetail(revision()); context.vm.askRestore() }
    const operation = action === 'detail' ? context.vm.openDetail(revision()) : context.vm.confirmRestore()
    context.dispose()
    pending.resolve(action === 'detail' ? revision() : { novel_id: 'work-a', id: 'chapter-a', content: '真实旧稿' })
    await operation
    assert.equal(context.messages.length, 0)
    assert.equal(context.emitted.length, 0)
  }
})
