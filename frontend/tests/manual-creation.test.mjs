import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive, watch } from 'vue'
import ts from 'typescript'
import { metadataFixture, memoryStorage } from './chapter-fixtures.mjs'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'], ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../utils/asyncScope', '../src/utils/asyncScope.ts'], ['../types', '../src/types.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const creationModule = await import(dependencies['../utils/chapterCreation'])
const apiModule = await import(moduleUrl(compile((await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))
  .replace("import('./utils/chapterCreation')", `import('${dependencies['../utils/chapterCreation']}')`))))
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
  const database = options.database ?? new Map(rows.map((row) => [row.id, structuredClone(row)]))
  const receipts = options.receipts ?? new Map()
  globalThis.localStorage = options.storage ?? memoryStorage()
  globalThis.window = { confirm: () => true, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  const implementations = {
    listChapterMetadata: async (novelId) => [...database.values()].filter((row) => row.novel_id === novelId).sort((a, b) => a.seq - b.seq).map(metadataFixture),
    getChapter: async (novelId, id) => {
      const row = database.get(id)
      if (!row || row.novel_id !== novelId) throw new Error('章节不存在')
      return structuredClone(row)
    },
    createChapter: async (novelId, data) => {
      const prior = receipts.get(data.request_id)
      if (prior) {
        if (prior.payload !== JSON.stringify(data)) throw Object.assign(new Error('原载荷不一致'), { status: 409, code: 'chapter_creation_payload_conflict' })
        const current = database.get(prior.id)
        if (!current) throw Object.assign(new Error('原章节已删除'), { status: 409, code: 'chapter_creation_deleted' })
        const changed = ['title', 'content', 'directive'].some((field) => current[field] !== prior.author[field])
        return { ...structuredClone(current), creation_replayed: true, author_changed_since_creation: changed }
      }
      const row = chapter(`created-${receipts.size + 1}`, { novel_id: novelId, ...data, seq: database.size + 1, summary: '', tension_score: null, drift_score: null })
      database.set(row.id, row)
      receipts.set(data.request_id, { id: row.id, payload: JSON.stringify(data), author: { title: row.title, content: row.content, directive: row.directive } })
      return { ...structuredClone(row), creation_replayed: false, author_changed_since_creation: false }
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
  return { vm, props, messages, requests, emitted, database, receipts, implementations, storage: globalThis.localStorage, listeners,
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

const recordValues = (storage) => [...storage.values.values()].map((raw) => JSON.parse(raw))
const unknown = () => { throw new Error('回应丢失') }

test('真实API新章提交显式带UUID，普通更新不附创建请求标识', async () => {
  const fetch = globalThis.fetch, requests = [], id = crypto.randomUUID()
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return new Response(JSON.stringify({ ...chapter(), creation_replayed: false, author_changed_since_creation: false })) }
  try {
    await apiModule.api.createChapter('novel-a', { title: '手写章', content: '正文', directive: '', status: 'draft', request_id: id })
    await apiModule.api.updateChapter('novel-a', 'chapter-a', { content: '新正文', revision_source: 'manual_save' })
    assert.equal(JSON.parse(requests[0].body).request_id, id)
    assert.equal('request_id' in JSON.parse(requests[1].body), false)
  } finally { globalThis.fetch = fetch }
})

test('POST前持久冻结首UUID和payload，挂起输入只更新待保留稿，双击不并发', async () => {
  const pending = deferred()
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => pending.promise } })
  context.vm.title.value = '首次标题'; context.vm.content.value = '首次正文'
  const saving = context.vm.saveContent(); await context.vm.saveContent()
  const sent = context.requests.find((row) => row.name === 'createChapter').args[1]
  const before = recordValues(context.storage)[0]
  assert.deepEqual(JSON.parse(before.payloadJson), sent); assert.match(sent.request_id, /^[0-9a-f-]{36}$/)
  context.vm.title.value = '后续标题'; context.vm.content.value = '挂起新正文'; context.vm.directive.value = '挂起新指令'
  const updated = recordValues(context.storage)[0]
  assert.equal(updated.payloadJson, before.payloadJson); assert.equal(updated.draft.content, '挂起新正文'); assert.equal(updated.edited, true)
  pending.reject(new Error('回应丢失')); await saving
  assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 1)
  assert.equal(context.vm.content.value, '挂起新正文'); assert.equal(context.vm.hasUnverifiedCreation.value, true)
  assert.match(textOf(context.render()), /核对原保存/)
  context.dispose()
})

test('首提交已成功但丢回后原样重试只建一章，新输入保留而不自动PUT', async () => {
  const context = await panel('WritingPanel', { rows: [] })
  let calls = 0
  const create = context.implementations.createChapter
  context.implementations.createChapter = create
  // 让同一实际夹具事务提交，只有回应第一次丢失。
  const pending = deferred()
  const second = await panel('WritingPanel', { rows: [], database: context.database, receipts: context.receipts, storage: context.storage,
    api: { createChapter: async (...args) => { const saved = await create(...args); if (++calls === 1) return pending.promise; return saved } } })
  second.vm.content.value = '首次完整正文'
  const saving = second.vm.saveContent(); await flush()
  second.vm.content.value = '作者等待期间新增的正文'
  pending.reject(new Error('回应丢失')); await saving
  assert.equal(context.database.size, 1); assert.equal(second.vm.selectedId.value, null)
  await second.vm.saveContent()
  const requests = second.requests.filter((row) => row.name === 'createChapter')
  assert.deepEqual(requests[1].args, requests[0].args); assert.equal(context.database.size, 1)
  assert.equal(second.vm.selectedId.value, 'created-1'); assert.equal(second.vm.savedSnapshot.value.content, '首次完整正文')
  assert.equal(second.vm.content.value, '作者等待期间新增的正文'); assert.equal(second.vm.isDirty.value, true)
  assert.equal(second.requests.some((row) => row.name === 'updateChapter'), false); assert.equal(second.storage.length, 0)
  assert.ok(second.messages.some((row) => /已核对原保存.*未另建/.test(row.text)))
  await second.vm.saveContent(); assert.equal(context.database.size, 1)
  assert.equal(second.vm.isDirty.value, false); assert.equal(second.requests.filter((row) => row.name === 'updateChapter').length, 1)
  second.dispose(); context.dispose()
})

test('刷新只列恢复入口不自动写，显式恢复最新稿后才沿原提交核对', async () => {
  const storage = memoryStorage(), pending = deferred()
  const first = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => pending.promise } })
  first.vm.title.value = '原标题'; first.vm.content.value = '原正文'
  const saving = first.vm.saveContent(); first.vm.content.value = '刷新前最后编辑'
  pending.reject(new Error('网络断开')); await saving
  const record = recordValues(storage)[0], sent = first.requests.find((row) => row.name === 'createChapter').args[1]
  first.dispose()
  const recovered = await panel('WritingPanel', { rows: [], storage })
  assert.equal(recovered.requests.some((row) => row.name === 'createChapter' || row.name === 'updateChapter'), false)
  assert.equal(recovered.vm.content.value, ''); assert.match(textOf(recovered.render()), /恢复待核对稿/)
  await recovered.vm.restoreCreation(record.requestId)
  assert.equal(recovered.vm.content.value, '刷新前最后编辑')
  assert.equal(recovered.requests.some((row) => row.name === 'createChapter'), false)
  await recovered.vm.saveContent()
  assert.deepEqual(recovered.requests.find((row) => row.name === 'createChapter').args[1], sent)
  assert.equal(recovered.vm.content.value, '刷新前最后编辑'); assert.equal(recovered.vm.savedSnapshot.value.content, '原正文')
  assert.equal(recovered.vm.isDirty.value, true); assert.equal(storage.length, 0)
  recovered.dispose()
})

test('恢复待核对稿前保护当前输入，取消不载入，确认恢复也不触发POST', async () => {
  const storage = memoryStorage()
  const first = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => unknown() } })
  first.vm.content.value = '待恢复正文'; await first.vm.saveContent(); const id = first.vm.activeCreation.value.requestId; first.dispose()
  const current = await panel('WritingPanel', { rows: [], storage })
  current.vm.content.value = '当前作者新稿'
  let restoring = current.vm.restoreCreation(id); await flush()
  current.vm.finishDiscard(false); await restoring
  assert.equal(current.vm.content.value, '当前作者新稿')
  restoring = current.vm.restoreCreation(id); await flush(); current.vm.finishDiscard(true); await restoring
  assert.equal(current.vm.content.value, '待恢复正文'); assert.equal(current.requests.some((row) => row.name === 'createChapter'), false)
  current.dispose()
})

test('重放当前Chapter已被其他窗口改稿时绑定真实ID基线，保留本地稿且不自动覆盖', async () => {
  const storage = memoryStorage()
  const base = await panel('WritingPanel', { rows: [], storage })
  const create = base.implementations.createChapter; let calls = 0
  const current = await panel('WritingPanel', { rows: [], storage, database: base.database, receipts: base.receipts,
    api: { createChapter: async (...args) => { const saved = await create(...args); if (++calls === 1) unknown(); return saved } } })
  current.vm.title.value = '作者原稿'; current.vm.content.value = '作者原正文'; await current.vm.saveContent()
  const stored = base.database.get('created-1'); Object.assign(stored, { title: '另一窗口章名', content: '另一窗口当前稿', directive: '另一窗口指令' })
  await current.vm.saveContent()
  assert.equal(current.vm.selectedId.value, 'created-1'); assert.equal(current.vm.savedSnapshot.value.content, stored.content)
  assert.equal(current.vm.selectedDetail.value.title, stored.title); assert.equal(current.vm.content.value, '作者原正文')
  assert.equal(current.vm.title.value, '作者原稿'); assert.equal(current.vm.isDirty.value, true)
  assert.equal(current.requests.some((row) => row.name === 'updateChapter'), false); assert.equal(base.database.size, 1)
  current.dispose(); base.dispose()
})

test('缺少完整boolean创建确认不冒充成功，异常回应沿原UUID继续核对', async () => {
  for (const flags of [{}, { creation_replayed: false }, { creation_replayed: false, author_changed_since_creation: 'false' }]) {
    const context = await panel('WritingPanel', { rows: [], api: { createChapter: async (_id, data) => ({ ...chapter('created-1'), title: data.title, content: data.content, directive: data.directive, ...flags }) } })
    context.vm.content.value = '作者正文'; await context.vm.saveContent(); const payload = context.requests.find((row) => row.name === 'createChapter').args[1]
    assert.equal(context.vm.selectedId.value, null); assert.equal(context.vm.hasUnverifiedCreation.value, true)
    assert.equal(context.vm.isDirty.value, true); assert.equal(context.messages.some((row) => row.type === 'success'), false)
    await context.vm.saveContent(); assert.deepEqual(context.requests.filter((row) => row.name === 'createChapter')[1].args[1], payload)
    assert.equal(context.storage.length, 1); context.dispose()
  }
})

test('删除墓碑和载荷/作用域冲突保留文本并阻断旧key，刷新恢复也不会悄悄重建', async () => {
  for (const code of ['chapter_creation_deleted', 'chapter_creation_payload_conflict', 'chapter_creation_scope_conflict']) {
    const storage = memoryStorage()
    const context = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => { throw Object.assign(new Error('创建被拒绝'), { status: 409, code }) } } })
    context.vm.content.value = '不得丢失正文'; await context.vm.saveContent(); await context.vm.saveContent()
    assert.equal(context.vm.creationBlocked.value, true); assert.equal(context.vm.content.value, '不得丢失正文')
    assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 1)
    const record = recordValues(storage)[0]; assert.equal(record.blockedCode, code); context.dispose()
    const recovered = await panel('WritingPanel', { rows: [], storage })
    await recovered.vm.restoreCreation(record.requestId); await recovered.vm.saveContent()
    assert.equal(recovered.vm.creationBlocked.value, true); assert.equal(recovered.requests.some((row) => row.name === 'createChapter'), false)
    recovered.dispose()
  }
})

test('未知回应后明确新章产生新UUID，相同文本合法；不同作品和多个意图独立保留', async () => {
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => unknown() } })
  context.vm.content.value = '同一正文'; await context.vm.saveContent(); const first = context.vm.activeCreation.value.requestId
  const newDraft = context.vm.startNew(); await flush(); context.vm.finishDiscard(true); await newDraft
  context.vm.content.value = '同一正文'; await context.vm.saveContent(); const second = context.vm.activeCreation.value.requestId
  assert.notEqual(first, second); assert.equal(context.storage.length, 2)
  context.props.novelId = 'novel-b'; await flush(); context.vm.content.value = 'B稿'; await context.vm.saveContent()
  assert.equal(context.storage.length, 3); assert.equal(context.vm.creationRecords.value.length, 1)
  context.props.novelId = 'novel-a'; await flush(); assert.equal(context.vm.creationRecords.value.length, 2)
  assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 3)
  context.dispose()
})

test('未知稿已清空输入仍有离开/刷新保护，AI入口不能绕过原创建核对', async () => {
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => unknown() } })
  context.vm.content.value = '原正文'; await context.vm.saveContent(); context.vm.content.value = ''
  assert.equal(context.vm.isDirty.value, false); assert.equal(context.vm.hasUnsavedChanges.value, true)
  let prevented = false
  context.listeners.get('beforeunload')({ preventDefault: () => { prevented = true }, returnValue: null })
  assert.equal(prevented, true); await context.vm.doWrite()
  assert.match(context.messages.at(-1).text, /先核对原草稿保存/)
  context.dispose()
})

test('初次存储失败仍保留内存意图与新编辑并明确复制，未知原请求可安全重试', async () => {
  const storage = memoryStorage(); storage.setItem = () => { throw new Error('存储不可用') }
  let calls = 0
  const context = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async (_id, data) => {
    if (++calls === 1) unknown()
    return { ...chapter('created-1'), title: data.title, content: data.content, directive: data.directive, creation_replayed: true, author_changed_since_creation: false }
  } } })
  context.vm.content.value = '原提交'; await context.vm.saveContent(); context.vm.content.value = '新稿保留'
  assert.match(context.vm.creationStorageWarning.value, /未能保存.*复制/); assert.equal(storage.length, 0)
  assert.equal(context.vm.creationRecords.value[0].draft.content, '新稿保留')
  await context.vm.saveContent()
  assert.equal(context.vm.content.value, '新稿保留'); assert.equal(context.vm.isDirty.value, true)
  assert.deepEqual(context.requests.filter((row) => row.name === 'createChapter')[1].args, context.requests.filter((row) => row.name === 'createChapter')[0].args)
  context.dispose()
})

test('首次记录已存后存储满不谎称最新稿可恢复，内存保留且键入不重复toast', async () => {
  const storage = memoryStorage(), context = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => unknown() } })
  context.vm.content.value = '原提交'; await context.vm.saveContent(); const count = context.messages.length
  storage.setItem = () => { throw new Error('满') }
  for (const text of ['最新稿一', '最新稿二', '最新稿三']) context.vm.content.value = text
  assert.equal(context.messages.length, count); assert.match(context.vm.creationStorageWarning.value, /最新编辑稿.*未能更新.*刷新后只能恢复先前/)
  assert.equal(recordValues(storage)[0].draft.content, '原提交'); assert.equal(context.vm.activeCreation.value.draft.content, '最新稿三')
  const id = context.vm.activeCreation.value.requestId
  await context.vm.startNew(true); await context.vm.restoreCreation(id)
  assert.equal(context.vm.content.value, '最新稿三')
  context.dispose()
})

test('确认ID后清理浏览器记录失败仍报已确认保存，不把后置缓存错误当数据库失败', async () => {
  const storage = memoryStorage(); storage.removeItem = () => { throw new Error('缓存清理失败') }
  const context = await panel('WritingPanel', { rows: [], storage })
  context.vm.content.value = '真实已保存稿'; await context.vm.saveContent()
  assert.equal(context.vm.selectedId.value, 'created-1'); assert.equal(context.vm.hasUnverifiedCreation.value, false)
  assert.equal(context.vm.savedSnapshot.value.content, '真实已保存稿'); assert.equal(context.vm.isDirty.value, false)
  assert.ok(context.messages.some((row) => /已确认保存.*清理失败/.test(row.text)))
  assert.ok(context.messages.some((row) => row.type === 'success')); assert.equal(storage.length, 1)
  context.dispose()
})

test('移除恢复记录需明确确认且仅删指定key，保留当前稿并阻止误用新key重复创建', async () => {
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => unknown() } })
  context.vm.content.value = '第一稿'; await context.vm.saveContent(); const first = context.vm.activeCreation.value.requestId
  await context.vm.startNew(true); context.vm.content.value = '第二稿'; await context.vm.saveContent(); const second = context.vm.activeCreation.value.requestId
  let deleting = context.vm.discardCreation(first); await flush(); context.vm.finishDiscard(false); await deleting
  assert.equal(context.storage.length, 2)
  deleting = context.vm.discardCreation(first); await flush(); context.vm.finishDiscard(true); await deleting
  assert.equal(context.storage.length, 1); assert.equal(recordValues(context.storage)[0].requestId, second)
  deleting = context.vm.discardCreation(second); await flush(); context.vm.finishDiscard(true); await deleting
  assert.equal(context.storage.length, 0); assert.equal(context.vm.content.value, '第二稿')
  context.vm.content.value = '第二稿的新输入'; await context.vm.saveContent()
  assert.equal(context.storage.length, 0); assert.equal(context.vm.creationBlocked.value, true)
  assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 2)
  context.dispose()
})

test('来源ABA和卸载后的旧创建确认不回写或删除新意图记录，旧失败也静默', async () => {
  for (const change of ['novel', 'new', 'unmount']) for (const outcome of ['success', 'failure']) {
    const old = deferred()
    const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => old.promise } })
    context.vm.content.value = '旧创建正文'; const saving = context.vm.saveContent(); const id = context.vm.activeCreation.value.requestId
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await flush() }
    if (change === 'new') await context.vm.startNew(true)
    if (change === 'unmount') context.dispose()
    context.vm.content.value = '当前新稿'
    if (outcome === 'success') old.resolve({ ...chapter('created-1'), creation_replayed: false, author_changed_since_creation: false })
    else old.reject(new Error('旧创建错误'))
    await saving
    assert.equal(context.vm.content.value, '当前新稿'); assert.equal(context.vm.selectedId.value, null)
    assert.equal(recordValues(context.storage).some((row) => row.requestId === id), true); assert.equal(context.messages.length, 0)
    if (change !== 'unmount') context.dispose()
  }
})

test('损坏或跨作品记录不会恢复或被自动提交，其他有效UUID仍可核对', async () => {
  const storage = memoryStorage(), first = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => unknown() } })
  first.vm.content.value = '有效稿'; await first.vm.saveContent(); first.dispose()
  storage.setItem('lingmo:chapter-creation:v1:novel-a:broken', '{bad-json')
  const foreign = recordValues(memoryStorage(Object.fromEntries([...storage.values].filter(([key]) => !key.endsWith('broken')))))[0]
  storage.setItem(`lingmo:chapter-creation:v1:novel-a:${crypto.randomUUID()}`, JSON.stringify({ ...foreign, novelId: 'novel-b' }))
  const context = await panel('WritingPanel', { rows: [], storage })
  assert.equal(context.vm.creationRecords.value.length, 1); assert.match(context.vm.creationStorageWarning.value, /部分.*无法读取/)
  assert.equal(context.requests.some((row) => row.name === 'createChapter'), false)
  context.dispose()
})

test('异常完整章回包保持原意图未知，不提前清缓存或伪称保存成功', async () => {
  for (const patch of [{ seq: undefined }, { summary: null }, { tension_score: '高' }, { created_at: null }, { status: 'unknown' }, { id: {} }]) {
    const context = await panel('WritingPanel', { rows: [], api: { createChapter: async (_id, data) => ({ ...chapter('created-1'),
      title: data.title, content: data.content, directive: data.directive, creation_replayed: false, author_changed_since_creation: false, ...patch }) } })
    context.vm.content.value = '完整本地稿'; await context.vm.saveContent()
    assert.equal(context.vm.selectedId.value, null); assert.equal(context.storage.length, 1)
    assert.equal(context.vm.content.value, '完整本地稿'); assert.equal(context.vm.hasUnverifiedCreation.value, true)
    assert.equal(context.messages.some((row) => row.type === 'success'), false)
    const first = context.requests.find((row) => row.name === 'createChapter').args
    await context.vm.saveContent(); assert.deepEqual(context.requests.filter((row) => row.name === 'createChapter')[1].args, first)
    context.dispose()
  }
})

test('旧意图回应不能删除新的UUID或结束新保存loading，新确认只清自己的记录', async () => {
  const old = deferred(), current = deferred(); let count = 0
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => (++count === 1 ? old.promise : current.promise) } })
  context.vm.content.value = '旧稿'; const first = context.vm.saveContent(); const oldId = context.vm.activeCreation.value.requestId
  await context.vm.startNew(true); context.vm.content.value = '新稿'; const second = context.vm.saveContent(); const currentId = context.vm.activeCreation.value.requestId
  old.resolve({ ...chapter('old-created'), content: '旧稿', creation_replayed: false, author_changed_since_creation: false }); await first
  assert.equal(context.vm.saving.value, true); assert.equal(context.vm.activeCreation.value.requestId, currentId)
  assert.equal(context.storage.length, 2); assert.equal(context.vm.content.value, '新稿'); assert.equal(context.vm.selectedId.value, null)
  current.resolve({ ...chapter('new-created'), title: '', directive: '', content: '新稿', creation_replayed: false, author_changed_since_creation: false }); await second
  assert.equal(context.vm.selectedId.value, 'new-created'); assert.equal(context.vm.content.value, '新稿')
  assert.deepEqual(recordValues(context.storage).map((row) => row.requestId), [oldId]); assert.equal(context.vm.saving.value, false)
  context.dispose()
})

test('恢复确认等待期间切作品ABA或继续编辑，不把旧记录带入当前稿', async () => {
  for (const change of ['novel', 'editing']) {
    const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => unknown() } })
    context.vm.content.value = '原待核对稿'; await context.vm.saveContent(); const id = context.vm.activeCreation.value.requestId
    await context.vm.startNew(true); context.vm.content.value = '现在的新稿'
    const restoring = context.vm.restoreCreation(id); await flush()
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; context.vm.content.value = '作品切换后的稿' }
    else context.vm.content.value = '确认等待期间又编辑'
    context.vm.finishDiscard(true); await restoring
    assert.equal(context.vm.content.value, change === 'novel' ? '作品切换后的稿' : '确认等待期间又编辑')
    assert.equal(context.vm.activeCreation.value, null); assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 1)
    context.dispose()
  }
})

test('恢复同步重置触发作品ABA时静默退出，不把待核对稿覆盖新作品编辑器', async () => {
  const context = await panel('WritingPanel', { rows: [], api: { createChapter: async () => unknown() } })
  context.vm.content.value = '待核对正文'; await context.vm.saveContent(); const id = context.vm.activeCreation.value.requestId
  await context.vm.startNew(true); context.vm.content.value = '当前独立正文'
  let fired = false
  const stop = watch(context.vm.content, (value) => {
    if (!value && !fired) { fired = true; context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; context.vm.content.value = '来源改变后的作者稿' }
  }, { flush: 'sync' })
  const restoring = context.vm.restoreCreation(id); await flush(); context.vm.finishDiscard(true); await restoring
  assert.equal(context.vm.content.value, '来源改变后的作者稿'); assert.equal(context.vm.activeCreation.value, null)
  assert.equal(context.requests.filter((row) => row.name === 'createChapter').length, 1)
  stop(); context.dispose()
})

test('storage读取不可用与创建busy都保留内存原请求，恢复存储后最新稿可准确持久', async () => {
  const storage = memoryStorage(), set = storage.setItem
  Object.defineProperty(storage, 'length', { configurable: true, get() { throw new Error('浏览器禁用存储') } })
  storage.setItem = () => { throw new Error('浏览器禁用存储') }
  const context = await panel('WritingPanel', { rows: [], storage, api: { createChapter: async () => { throw Object.assign(new Error('创建结果尚未确认'), { status: 409, code: 'chapter_creation_busy' }) } } })
  context.vm.content.value = '首稿'; await context.vm.saveContent(); context.vm.content.value = '最新稿'
  assert.match(context.vm.creationStorageWarning.value, /复制/); assert.equal(context.vm.creationBlocked.value, false)
  const id = context.vm.activeCreation.value.requestId, first = context.requests.find((row) => row.name === 'createChapter').args
  storage.setItem = set; Object.defineProperty(storage, 'length', { get() { return storage.values.size } })
  context.vm.content.value = '存储恢复后的最新稿'
  assert.equal(context.vm.creationStorageWarning.value, ''); assert.equal(recordValues(storage)[0].draft.content, '存储恢复后的最新稿')
  await context.vm.saveContent(); assert.deepEqual(context.requests.filter((row) => row.name === 'createChapter')[1].args, first)
  assert.equal(context.vm.activeCreation.value.requestId, id); assert.equal(context.vm.content.value, '存储恢复后的最新稿')
  context.dispose()
})

test('删除作品只清该作品全部恢复key，兼容编码名称和损坏记录，不动其他作品', () => {
  const storage = memoryStorage(); globalThis.localStorage = storage
  for (const novelId of ['novel/a:一', 'novel/a:一suffix', 'novel-b']) {
    for (let index = 0; index < 3; index++) {
      const record = creationModule.newChapterCreation(novelId, { title: '', content: '正文', directive: '' })
      assert.equal(creationModule.storeChapterCreation(record), true)
    }
  }
  storage.setItem('lingmo:chapter-creation:v1:novel%2Fa%3A%E4%B8%80:broken', '{bad-json')
  storage.setItem('unrelated', '保留')
  assert.equal(creationModule.clearNovelChapterCreations('novel/a:一'), true)
  assert.equal(storage.length, 7)
  assert.equal([...storage.values.keys()].some((key) => key.startsWith('lingmo:chapter-creation:v1:novel%2Fa%3A%E4%B8%80:')), false)
  assert.equal(creationModule.listChapterCreations('novel/a:一suffix').records.length, 3)
  assert.equal(creationModule.listChapterCreations('novel-b').records.length, 3)
  assert.equal(storage.getItem('unrelated'), '保留')
})

test('API删除数据库失败不清恢复记录，成功才清；存储清理故障仍保持已删除结果', async () => {
  const previous = globalThis.fetch
  try {
    for (const mode of ['db-failure', 'success', 'storage-failure', 'read-failure']) {
      const storage = memoryStorage(); globalThis.localStorage = storage
      const record = creationModule.newChapterCreation('novel-a', { title: '', content: '原稿', directive: '' })
      creationModule.storeChapterCreation(record)
      if (mode === 'storage-failure') storage.removeItem = () => { throw new Error('缓存清理故障') }
      if (mode === 'read-failure') Object.defineProperty(storage, 'length', { get() { throw new Error('缓存不可读') } })
      const requests = []
      globalThis.fetch = async (url, options) => {
        requests.push({ url, ...options }); assert.equal(storage.values.size, 1)
        return mode === 'db-failure' ? new Response(JSON.stringify({ detail: '数据库拒绝删除' }), { status: 409 }) : new Response(null, { status: 204 })
      }
      if (mode === 'db-failure') await assert.rejects(apiModule.api.deleteNovel('novel-a'), /数据库拒绝删除/)
      else assert.equal(await apiModule.api.deleteNovel('novel-a'), undefined)
      assert.equal(storage.values.size, mode === 'success' ? 0 : 1)
      assert.deepEqual(requests.map((row) => [row.url, row.method]), [['/api/v1/novels/novel-a', 'DELETE']])
    }
  } finally { globalThis.fetch = previous }
})

