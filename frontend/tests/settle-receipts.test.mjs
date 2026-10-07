import { splitChapterFixtures } from './chapter-fixtures.mjs'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'], ['../utils/wordLimits', '../src/utils/wordLimits.ts'], ['../types', '../src/types.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const apiModule = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
const sourceHash = 'a'.repeat(64), otherHash = 'b'.repeat(64)
const settle = async () => { for (let i = 0; i < 25; i += 1) { await Promise.resolve(); await nextTick() } }
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const chapter = (novelId = 'novel-a', patch = {}) => ({ id: `${novelId}-chapter`, novel_id: novelId, seq: 1, title: '第一章',
  content: '作者已保存的正文', directive: '作者的章前指令', summary: '', outline_node_id: '', status: 'draft', tension_score: null, drift_score: null, ...patch })
const candidates = () => [{ type: 'character', data: { name: '人物甲' } }, { type: 'foreshadowing', data: { content: '伏笔甲' } }, { type: 'timeline_event', data: { description: '事件甲' } }]
const preview = (patch = {}) => ({ chapter_id: 'novel-a-chapter', source_fingerprint: sourceHash, summary: '章节摘要', suggestions: candidates(), drift: null, conflicts: [], ...patch })
const receipt = (index, patch = {}) => ({ index, type: candidates()[index].type, id: `entity-${index}`, receipt_id: `receipt-${index}`, replayed: false, deleted: false, ...patch })
const response = (created, failed = []) => ({ created, failed, new_count: created.filter((row) => !row.replayed).length, replayed_count: created.filter((row) => row.replayed).length })
let sequence = 0

async function writing(options = {}) {
  const props = reactive({ novelId: 'novel-a', initialChapterId: 'novel-a-chapter', ...options.props })
  const messages = [], requests = [], hooks = {}
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  const implementations = {
    listChapterMetadata: async (id) => [chapter(id)], settleChapter: async () => preview(), settleApply: async () => response([receipt(0), receipt(1), receipt(2)]),
    ...options.api,
  }
  const harness = {
    api: Object.fromEntries(Object.entries(splitChapterFixtures(implementations, (id, chapterId) => chapter(id, { id: chapterId }))).map(([name, fn]) => [name, (...args) => { requests.push({ name, args: structuredClone(args) }); return fn(...args) }])),
    streamSSE: async () => { throw new Error('此流程不应生成正文') },
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const name of ['NButton', 'NCheckbox', 'NEmpty', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NModal', 'NSelect', 'NTag']) harness[name] = name.toLowerCase()
  const key = `__settleReceipt${++sequence}`; globalThis[key] = harness
  const descriptor = parse(await readFile(new URL('../src/components/WritingPanel.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `receipt-${sequence}` })
  let source = `const __harness = globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamSSE \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1 = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  for (const [name, url] of Object.entries(dependencies)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source)))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit() {} }))
  await hooks.mount?.(); await settle()
  const template = compileTemplate({ source: descriptor.template.content, id: `receipt-${sequence}`, filename: 'WritingPanel.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${sequence}`))
  if (options.preview !== false) await vm.doSettle()
  return { vm, props, requests, messages,
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

function findNode(node, predicate) {
  if (Array.isArray(node)) return node.map((child) => findNode(child, predicate)).find(Boolean)
  if (!node || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  return findNode(typeof node.children?.default === 'function' ? node.children.default() : node.children, predicate)
}

test('真实API确认请求显式携带原预览hash，不在接口内猜当前来源', async () => {
  const original = globalThis.fetch, requests = []
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return new Response(JSON.stringify(response([receipt(0)]))) }
  try {
    await apiModule.api.settleApply('novel-a', 'chapter-a', [candidates()[0]], sourceHash)
    assert.equal(requests[0].url, '/api/v1/novels/novel-a/chapters/chapter-a/settle/apply')
    assert.deepEqual(JSON.parse(requests[0].body), { suggestions: [candidates()[0]], source_fingerprint: sourceHash })
  } finally { globalThis.fetch = original }
})

test('真实API定稿显式发送作者三项快照，旧调用的空body保持兼容', async () => {
  const original = globalThis.fetch, requests = []
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return new Response(JSON.stringify(preview())) }
  try {
    const fields = { title: '已保存的章名', content: '已保存的正文', directive: '已保存的指令' }
    await apiModule.api.settleChapter('novel-a', 'chapter-a', fields)
    await apiModule.api.settleChapter('novel-a', 'chapter-a')
    assert.deepEqual(JSON.parse(requests[0].body), { expected_author_fields: fields })
    assert.equal(requests[1].body, undefined)
  } finally { globalThis.fetch = original }
})

test('定稿前冻结作者快照，等待期间的新输入不改变已发送字段或接纳旧预览', async () => {
  const pending = wait()
  const context = await writing({ preview: false, api: { settleChapter: async () => pending.promise } })
  const fields = { title: context.vm.title.value, content: context.vm.content.value, directive: context.vm.directive.value }
  const settling = context.vm.doSettle()
  context.vm.title.value = '等待期间的新章名'; context.vm.content.value = '等待期间的新正文'; context.vm.directive.value = '等待期间的新指令'
  pending.resolve(preview()); await settling
  assert.deepEqual(context.requests.find((entry) => entry.name === 'settleChapter').args, ['novel-a', 'novel-a-chapter', fields])
  assert.equal(context.vm.content.value, '等待期间的新正文'); assert.equal(context.vm.showSettle.value, false)
  assert.equal(context.messages.length, 0)
  context.dispose()
})

test('定稿前置source409不打开确认窗口，保留本地正文并提供显式读取入口', async () => {
  const context = await writing({ api: { settleChapter: async () => { throw Object.assign(new Error('已保存稿另行变化'), { status: 409, code: 'settle_source_changed' }) } } })
  assert.equal(context.vm.showSettle.value, false); assert.equal(context.vm.settleRefreshNeeded.value, true)
  assert.equal(context.vm.content.value, chapter().content); assert.equal(context.vm.title.value, chapter().title)
  assert.match(textOf(context.render()), /重新读取章节核对/)
  await context.vm.confirmSettle()
  assert.equal(context.requests.some((entry) => entry.name === 'settleApply'), false)
  assert.ok(context.messages.some((entry) => /本次未开始定稿.*核对后再定稿/.test(entry.text)))
  context.dispose()
})

test('来源前置失败不会自动读取覆盖旧稿，作者点击可达按钮后才读取真实新稿', async () => {
  let reads = 0
  const newer = chapter('novel-a', { title: '另一请求保存的章名', content: '另一请求保存的正文', directive: '另一请求保存的指令' })
  const context = await writing({ api: {
    listChapterMetadata: async () => [++reads === 1 ? chapter() : newer],
    settleChapter: async () => { throw Object.assign(new Error('来源已变化'), { status: 409, code: 'settle_source_changed' }) },
  } })
  assert.equal(reads, 1); assert.equal(context.vm.content.value, chapter().content)
  const button = findNode(context.render(), (node) => node.type === 'nbutton' && textOf(node) === '重新读取章节核对')
  assert.ok(button)
  await button.props.onClick()
  assert.equal(reads, 2); assert.equal(context.vm.content.value, newer.content)
  assert.equal(context.vm.title.value, newer.title); assert.equal(context.vm.directive.value, newer.directive)
  assert.equal(context.vm.isDirty.value, false); assert.equal(context.vm.settleRefreshNeeded.value, false)
  context.dispose()
})

test('来源核对读取等待期间作者新正文保留，旧目录不能强制选择覆盖编辑器', async () => {
  const pending = wait(); let reads = 0
  const context = await writing({ api: {
    listChapterMetadata: async () => ++reads === 1 ? [chapter()] : pending.promise,
    settleChapter: async () => { throw Object.assign(new Error('来源已变化'), { status: 409, code: 'settle_source_changed' }) },
  } })
  const refreshing = context.vm.refreshSettleChapter()
  context.vm.content.value = '刷新等待期间的新正文'
  pending.resolve([chapter('novel-a', { content: '服务器另一稿' })]); await refreshing
  assert.equal(context.vm.content.value, '刷新等待期间的新正文'); assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.messages.filter((entry) => entry.type === 'info').length, 0)
  context.dispose()
})

test('缺失、非法或旧后端预览hash只能核对候选，不能确认或显示可安全落地', async () => {
  for (const hash of [undefined, '', 'a'.repeat(63), 'A'.repeat(64), 'not-a-source']) {
    const context = await writing({ api: { settleChapter: async () => preview({ source_fingerprint: hash }) } })
    const before = [...context.vm.settleDataTexts.value]
    assert.equal(context.vm.showSettle.value, true)
    await context.vm.confirmSettle()
    assert.equal(context.requests.some((entry) => entry.name === 'settleApply'), false)
    assert.deepEqual(context.vm.settleDataTexts.value, before)
    assert.equal(context.vm.settleConfirmBlocked.value, true)
    assert.ok(context.vm.settleWarnings.value.some((text) => /来源.*无法|不能确认/.test(text)))
    context.dispose()
  }
})

test('确认冻结原来源指纹、作品章节与过滤后原索引，新建与重放分别反馈', async () => {
  const context = await writing({ api: { settleApply: async () => response([receipt(0), receipt(2, { index: 1, replayed: true })], []) } })
  context.vm.checkedSuggestion.value = [true, false, true]
  await context.vm.confirmSettle()
  const request = context.requests.find((entry) => entry.name === 'settleApply')
  assert.equal(request.args[0], 'novel-a'); assert.equal(request.args[1], 'novel-a-chapter'); assert.equal(request.args[3], sourceHash)
  assert.deepEqual(request.args[2], [candidates()[0], candidates()[2]])
  assert.deepEqual(context.vm.settleCompleted.value, [true, false, true])
  assert.equal(context.vm.settledNewCount.value, 1); assert.equal(context.vm.settledReplayedCount.value, 1)
  assert.match(context.messages.at(-1).text, /新建 1 条.*已确认 1 条/)
  context.dispose()
})

test('部分成功只重试失败项仍携同一hash，已有确认项不会重新创建', async () => {
  let calls = 0
  const context = await writing({ api: { settleApply: async () => ++calls === 1
    ? response([receipt(0, { replayed: true })], [{ index: 1, type: 'foreshadowing', error: '伏笔需要修正' }, { index: 2, type: 'timeline_event', error: '事件需要修正' }])
    : response([receipt(1, { index: 0 }), receipt(2, { index: 1 })]) } })
  await context.vm.confirmSettle()
  assert.equal(context.vm.settleCompleted.value[0], true); assert.equal(context.vm.showSettle.value, true)
  context.vm.settleDataTexts.value[1] = JSON.stringify({ content: '修正伏笔' })
  await context.vm.confirmSettle()
  const requests = context.requests.filter((entry) => entry.name === 'settleApply')
  assert.equal(requests.length, 2); assert.equal(requests[1].args[3], sourceHash)
  assert.deepEqual(requests[1].args[2], [{ type: 'foreshadowing', data: { content: '修正伏笔' } }, candidates()[2]])
  assert.equal(context.vm.settledNewCount.value, 2); assert.equal(context.vm.settledReplayedCount.value, 1)
  context.dispose()
})

test('回执标明目标已删除时不宣称新建或复活，原建议锁定且明确未重建', async () => {
  const context = await writing({ api: { settleApply: async () => response([receipt(0, { replayed: true, deleted: true })],
    [{ index: 1, type: 'foreshadowing', error: '待修正' }, { index: 2, type: 'timeline_event', error: '待修正' }]) } })
  await context.vm.confirmSettle()
  assert.equal(context.vm.settleCompleted.value[0], true)
  assert.equal(context.vm.settledNewCount.value, 0); assert.equal(context.vm.settledDeletedCount.value, 1)
  assert.match(context.vm.settleReceiptLabel(0), /原目标已删除.*未重建/)
  assert.ok(context.messages.some((item) => /原目标已删除.*未重建/.test(item.text)))
  assert.match(textOf(context.render()), /原目标已删除.*未重建/)
  context.dispose()
})

test('响应丢失重试冻结原hash与JSON payload，等待期间的新编辑保留但不会换请求重复创建', async () => {
  const pending = wait(); let calls = 0
  const context = await writing({ api: { settleApply: async () => ++calls === 1 ? pending.promise : response([receipt(0, { replayed: true }), receipt(1, { replayed: true }), receipt(2, { replayed: true })]) } })
  const confirming = context.vm.confirmSettle()
  context.vm.settleDataTexts.value[0] = JSON.stringify({ name: '等待期间的新编辑' })
  context.vm.checkedSuggestion.value[1] = false
  pending.reject(new Error('连接中断，回应丢失')); await confirming
  assert.equal(context.vm.showSettle.value, true); assert.equal(context.vm.settledCount.value, 0)
  assert.equal(context.vm.settleApplying.value, false); assert.ok(context.vm.settlePendingSubmission.value)
  await context.vm.confirmSettle()
  const requests = context.requests.filter((entry) => entry.name === 'settleApply')
  assert.deepEqual(requests[1].args, requests[0].args)
  assert.equal(context.vm.settleDataTexts.value[0], JSON.stringify({ name: '等待期间的新编辑' }))
  assert.equal(context.vm.settledNewCount.value, 0); assert.equal(context.vm.settledReplayedCount.value, 3)
  assert.equal(context.vm.settlePendingSubmission.value, null)
  assert.equal(context.vm.showSettle.value, true)
  context.dispose()
})

test('真实source409保留候选正文，明确要求重新定稿并阻止旧预览再次确认', async () => {
  const context = await writing({ api: { settleApply: async () => { throw Object.assign(new Error('服务器正文已变化'), { status: 409, code: 'settle_source_changed' }) } } })
  const before = [...context.vm.settleDataTexts.value], body = context.vm.content.value
  await context.vm.confirmSettle(); await context.vm.confirmSettle()
  assert.equal(context.requests.filter((entry) => entry.name === 'settleApply').length, 1)
  assert.equal(context.vm.showSettle.value, true); assert.equal(context.vm.settleRefreshNeeded.value, true)
  assert.deepEqual(context.vm.settleDataTexts.value, before); assert.equal(context.vm.content.value, body)
  assert.ok(context.messages.some((item) => /重新定稿/.test(item.text)))
  context.dispose()
})

test('source部分失效保留成功回执和失败原索引，剩余项要求重新定稿', async () => {
  const context = await writing({ api: { settleApply: async () => response([receipt(0, { replayed: true })],
    [{ index: 1, type: 'timeline_event', error: '旧来源已变化', code: 'settle_source_changed' }]) } })
  context.vm.checkedSuggestion.value = [true, false, true]
  await context.vm.confirmSettle()
  assert.deepEqual(context.vm.settleCompleted.value, [true, false, false])
  assert.equal(context.vm.settleErrors.value[2], '旧来源已变化')
  assert.equal(context.vm.settleRefreshNeeded.value, true)
  assert.equal(context.vm.settledReplayedCount.value, 1)
  await context.vm.confirmSettle()
  assert.equal(context.requests.filter((entry) => entry.name === 'settleApply').length, 1)
  context.dispose()
})

test('同名未确认建议明确显示目标ID供作者核对，既不宣称合并也不算已确认', async () => {
  const error = '已有同名人物 entity-existing，请在设定面板核对后再决定'
  const context = await writing({ api: { settleApply: async () => response([], [{ index: 0, type: 'character', code: 'settle_name_exists', existing_ids: ['entity-existing'], error }]) } })
  context.vm.checkedSuggestion.value = [true, false, false]
  await context.vm.confirmSettle()
  assert.equal(context.vm.settleErrors.value[0], error); assert.match(textOf(context.render()), /entity-existing.*设定面板核对/)
  assert.equal(context.vm.settledCount.value, 0); assert.equal(context.vm.showSettle.value, true)
  assert.equal(context.vm.settleConfirmBlocked.value, false)
  context.dispose()
})

test('旧后端或缺完整回执不冒充新建/已确认，也不能盲目重试重复落库', async () => {
  for (const result of [
    { created: [{ index: 0, type: 'character', id: 'unknown' }], failed: [] },
    { ...response([receipt(0)]), new_count: undefined },
    { ...response([receipt(0)]), replayed_count: 3 },
    response([receipt(0, { replayed: false, deleted: true })]),
    { ...response([receipt(0)]), created: [null] },
    { ...response([]), failed: [null] },
  ]) {
    const context = await writing({ api: { settleApply: async () => result } })
    await context.vm.confirmSettle(); await context.vm.confirmSettle()
    assert.equal(context.vm.settledCount.value, 0); assert.equal(context.vm.showSettle.value, true)
    assert.equal(context.vm.settleReceiptUnavailable.value, true)
    assert.equal(context.requests.filter((entry) => entry.name === 'settleApply').length, 1)
    assert.equal(context.messages.some((item) => item.type === 'success'), false)
    context.dispose()
  }
})

test('来源ABA与卸载使旧hash确认回包静默，不影响当前hash、新输入或loading', async () => {
  for (const change of ['novel', 'chapter', 'text', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await writing({ api: { settleApply: async () => pending.promise } })
    const applying = context.vm.confirmSettle()
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (change === 'chapter') { await context.vm.startNew(true); await context.vm.selectChapter(chapter(), true) }
    if (change === 'text') { const before = context.vm.content.value; context.vm.content.value = '临时编辑'; context.vm.content.value = before }
    if (change === 'unmount') context.dispose()
    context.vm.content.value = '当前作者稿'
    if (outcome === 'success') pending.resolve(response([receipt(0), receipt(1), receipt(2)]))
    else pending.reject(Object.assign(new Error('旧source错误'), { status: 409, code: 'settle_source_changed' }))
    await applying
    assert.equal(context.vm.content.value, '当前作者稿'); assert.equal(context.vm.settledCount.value, 0)
    assert.equal(context.messages.length, 0)
    if (change !== 'unmount') context.dispose()
  }
})

test('重新定稿只使用新返回hash，旧确认完成不会结束新的确认loading', async () => {
  const old = wait(), fresh = wait(); let previews = 0, applies = 0
  const context = await writing({ api: {
    settleChapter: async () => preview({ source_fingerprint: ++previews === 1 ? sourceHash : otherHash }),
    settleApply: async () => ++applies === 1 ? old.promise : fresh.promise,
  } })
  const oldApply = context.vm.confirmSettle()
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle(); await context.vm.doSettle()
  const newApply = context.vm.confirmSettle()
  old.resolve(response([receipt(0), receipt(1), receipt(2)])); await oldApply
  assert.equal(context.vm.settleApplying.value, true); assert.equal(context.messages.length, 0)
  fresh.resolve(response([receipt(0), receipt(1), receipt(2)])); await newApply
  const requests = context.requests.filter((entry) => entry.name === 'settleApply')
  assert.equal(requests[0].args[3], sourceHash); assert.equal(requests[1].args[3], otherHash)
  assert.equal(context.vm.settleApplying.value, false)
  context.dispose()
})

test('确认请求在途时Esc与关闭回调不能伪装取消写入，完整回执成功后正常关闭', async () => {
  const pending = wait()
  const context = await writing({ api: { settleApply: async () => pending.promise } })
  const applying = context.vm.confirmSettle()
  const modal = findNode(context.render(), (node) => node.props?.title === '设定变更建议')
  assert.equal(modal.props['close-on-esc'], false); assert.equal(modal.props.closable, false)
  modal.props['onUpdate:show'](false)
  context.vm.handleSettleVisibility(false)
  assert.equal(context.vm.showSettle.value, true); assert.equal(context.vm.settleApplying.value, true)
  assert.equal(context.vm.settleContext.value.sourceFingerprint, sourceHash)
  pending.resolve(response([receipt(0), receipt(1), receipt(2)])); await applying
  assert.equal(context.vm.showSettle.value, false); assert.equal(context.vm.settleApplying.value, false)
  assert.equal(context.vm.settledNewCount.value, 3)
  context.dispose()
})

test('定稿提交已成功而目录刷新失败仍保留安全来源与回执，不报告未保存', async () => {
  let reads = 0
  const context = await writing({ api: { listChapterMetadata: async (id) => ++reads === 1 ? [chapter(id)] : Promise.reject(new Error('目录刷新失败')) } })
  assert.equal(context.vm.showSettle.value, true)
  assert.equal(context.vm.settleContext.value.sourceFingerprint, sourceHash)
  await context.vm.confirmSettle()
  assert.equal(context.vm.settledNewCount.value, 3)
  assert.ok(context.messages.some((item) => /定稿沉淀已完成.*刷新失败/.test(item.text)))
  assert.ok(context.messages.some((item) => /新建 3 条/.test(item.text)))
  context.dispose()
})
