import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const chapterUrl = moduleUrl(compile(await readFile(new URL('../src/utils/chapter.ts', import.meta.url), 'utf8')))
const flush = async () => { for (let index = 0; index < 20; index++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const chapters = (novelId = 'novel-a', suffix = '') => [1, 2].map((seq) => ({ id: `${novelId}-chapter-${seq}${suffix}`, novel_id: novelId, seq, title: `${novelId}章${seq}${suffix}`,
  status: 'draft', outline_node_id: '', tension_score: null, drift_score: null, created_at: '', updated_at: '', word_count: 10 }))
const track = (novelId = 'novel-a', patch = {}) => ({ id: `${novelId}-foreshadow`, novel_id: novelId, content: `${novelId}伏笔`, status: 'planted',
  related_characters: [], planted_chapter_id: `${novelId}-chapter-1`, consumed_chapter_id: '', planted_seq: 1, suspended_chapters: 1, long_pending: false, ...patch })
const tracking = (novelId = 'novel-a', items = [track(novelId)]) => ({ latest_seq: 2, chapter_count: 2,
  long_pending_threshold: { chapters: 10, ratio: 0.2 }, groups: Object.fromEntries(['planted', 'suspended', 'consumed'].map((status) => [status, items.filter((item) => item.status === status)])) })
let sequence = 0
async function panel(options = {}) {
  const requests = []
  let activeHooks, activeMessages
  const database = options.database ?? new Map(['novel-a', 'novel-b'].map((id) => [id, [track(id)]]))
  const implementations = {
    listChapterMetadata: async (id) => chapters(id),
    getForeshadowings: async (id) => structuredClone(tracking(id, database.get(id) ?? [])),
    updateEntity: async (novelId, path, id, payload) => {
      assert.equal(path, 'foreshadowings')
      const item = database.get(novelId)?.find((row) => row.id === id)
      if (!item) throw Object.assign(new Error('伏笔不存在'), { status: 404 })
      Object.assign(item, payload); return structuredClone(item)
    }, ...options.api,
  }
  const key = `__foreshadowingState${++sequence}`
  globalThis[key] = {
    api: Object.fromEntries(Object.entries(implementations).map(([name, fn]) => [name, (...args) => { requests.push({ name, args: structuredClone(args) }); return fn(...args) }])),
    useMessage: () => { const messages = activeMessages; return Object.fromEntries(['success', 'error', 'warning', 'info'].map((type) => [type, (text) => messages.push({ type, text })])) },
    NButton: 'nbutton', NEmpty: 'nempty', NSelect: 'nselect', NTag: 'ntag',
    onBeforeUnmount: (fn) => { activeHooks.unmount = fn },
  }
  const descriptor = parse(await readFile(new URL('../src/components/ForeshadowingTracking.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `foreshadowing-${sequence}` })
  const source = `const __harness = globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/from ['"]\.\.\/utils\/chapter['"]/g, `from '${chapterUrl}'`)
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(moduleUrl(compile(source)))).default
  const template = compileTemplate({ source: descriptor.template.content, id: `foreshadowing-${sequence}`, filename: 'ForeshadowingTracking.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
  async function mountAgain(overrides = {}) {
    const props = reactive({ novelId: 'novel-a', ...options.props, ...overrides }), messages = [], hooks = {}
    activeHooks = hooks; activeMessages = messages
    const scope = effectScope(), vm = scope.run(() => component.setup(props, { expose() {}, emit() {} }))
    await flush()
    return { vm, props, requests, messages, database, implementations, mountAgain,
      render: () => rendered.render({}, [], props, proxyRefs(vm), {}, {}),
      dispose: () => { hooks.unmount?.(); scope.stop(); delete globalThis[key] } }
  }
  return mountAgain()
}

function nodes(node, predicate, rows = []) {
  if (Array.isArray(node)) { for (const child of node) nodes(child, predicate, rows); return rows }
  if (!node || typeof node !== 'object') return rows
  if (predicate(node)) rows.push(node)
  nodes(typeof node.children?.default === 'function' ? node.children.default() : node.children, predicate, rows)
  return rows
}
function textOf(node) {
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'string') return node
  if (!node || typeof node !== 'object') return ''
  return typeof node.children?.default === 'function' ? textOf(node.children.default()) : textOf(node.children)
}

test('跨作品迟到metadata与tracking不能混用，空作品即时清空且不再更新旧item', async () => {
  const old = deferred(), context = await panel({ api: { listChapterMetadata: async (id) => id === 'novel-a' ? old.promise : chapters(id) } })
  context.props.novelId = 'novel-b'; await flush()
  old.resolve(chapters('novel-a')); await flush()
  assert.equal(context.vm.chapters.value[0].novel_id, 'novel-b')
  assert.equal(context.vm.groupItems('planted')[0].id, 'novel-b-foreshadow')
  const item = context.vm.groupItems('planted')[0]
  context.props.novelId = ''
  assert.deepEqual(context.vm.chapters.value, []); assert.equal(context.vm.tracking.value, null); assert.equal(context.vm.currentChapterId.value, null)
  await context.vm.markConsumed(item); assert.equal(context.requests.some((row) => row.name === 'updateEntity'), false)
  context.dispose()
})

test('同item埋设和消费共用写锁，真实消费200后刷新失败也无旧消费按钮或改绑重试', async () => {
  const update = deferred(); let reads = 0
  const context = await panel({ api: { listChapterMetadata: async (id) => { if (++reads > 1) throw new Error('目录刷新失败'); return chapters(id) }, updateEntity: async () => update.promise } })
  const item = context.vm.groupItems('planted')[0], first = context.vm.markConsumed(item)
  const second = context.vm.setPlanted(item, null), third = context.vm.markConsumed(item)
  assert.equal(context.requests.filter((row) => row.name === 'updateEntity').length, 1)
  update.resolve(track('novel-a', { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-2' })); await Promise.all([first, second, third])
  assert.equal(context.vm.groupItems('planted').length, 0); assert.equal(context.vm.groupItems('consumed')[0].status, 'consumed')
  assert.equal(nodes(context.render(), (node) => node.type === 'nbutton' && textOf(node).includes('标记已消费')).length, 0)
  assert.ok(context.messages.some((row) => /已保存.*刷新未完成/.test(row.text))); assert.equal(context.messages.some((row) => row.type === 'error'), false)
  context.vm.currentChapterId.value = 'novel-a-chapter-1'; await context.vm.markConsumed(item); await context.vm.markConsumed(context.vm.groupItems('consumed')[0])
  assert.equal(context.requests.filter((row) => row.name === 'updateEntity').length, 1)
  context.dispose()
})

test('目录和tracking并行但必须一起原子采用，任一路失败不产生混合快照', async () => {
  const metadata = deferred(), facts = deferred()
  const context = await panel({ api: { listChapterMetadata: async () => metadata.promise, getForeshadowings: async () => facts.promise } })
  assert.deepEqual(context.requests.map((row) => row.name), ['listChapterMetadata', 'getForeshadowings'])
  facts.resolve(tracking()); await flush()
  assert.equal(context.vm.tracking.value, null); assert.deepEqual(context.vm.chapters.value, []); assert.equal(context.vm.loading.value, true)
  metadata.reject(new Error('目录读取失败')); await flush()
  assert.equal(context.vm.tracking.value, null); assert.deepEqual(context.vm.chapters.value, []); assert.equal(context.vm.currentChapterId.value, null)
  assert.equal(context.messages[0].text, '目录读取失败'); assert.equal(context.vm.loading.value, false)
  await context.vm.markConsumed(track()); assert.equal(context.requests.some((row) => row.name === 'updateEntity'), false)
  context.dispose()
})

test('同作品乱序refresh与旧finally只接受最新read，不停止新loading或报旧错', async () => {
  for (const outcome of ['success', 'failure']) {
    const first = deferred(), second = deferred(); let count = 0
    const context = await panel({ api: { listChapterMetadata: async (id) => ++count === 1 ? chapters(id) : count === 2 ? first.promise : second.promise } })
    const old = context.vm.load(), latest = context.vm.load()
    if (outcome === 'success') first.resolve(chapters('novel-a', '-old')); else first.reject(new Error('旧read失败'))
    await old
    assert.equal(context.vm.loading.value, true); assert.equal(context.messages.length, 0)
    assert.equal(context.vm.chapters.value[0].id, 'novel-a-chapter-1')
    second.resolve(chapters('novel-a', '-latest')); await latest
    assert.equal(context.vm.loading.value, false); assert.equal(context.vm.chapters.value[0].id, 'novel-a-chapter-1-latest')
    context.dispose()
  }
})

test('作品ABA和卸载使旧read成功错误静默，旧结果不替换新的同作品快照', async () => {
  for (const change of ['novel', 'unmount']) for (const outcome of ['success', 'failure']) {
    const old = deferred(); let calls = 0
    const context = await panel({ api: { listChapterMetadata: async (id) => ++calls === 1 ? old.promise : chapters(id, '-latest') } })
    if (change === 'novel') { context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush() }
    else context.dispose()
    const current = JSON.parse(JSON.stringify(context.vm.chapters.value))
    if (outcome === 'success') old.resolve(chapters('novel-a', '-old')); else old.reject(new Error('旧来源读取失败'))
    await flush()
    assert.deepEqual(context.vm.chapters.value, current); assert.equal(context.messages.length, 0)
    assert.deepEqual(context.requests.filter((row) => row.name === 'getForeshadowings').map((row) => row.args[0]), change === 'novel' ? ['novel-a', 'novel-b', 'novel-a'] : ['novel-a'])
    if (change !== 'unmount') context.dispose()
  }
})

test('目录refresh等待保留作者有效新选择及清空选择，未改选择仍默认末章', async () => {
  for (const selection of ['novel-a-chapter-1', null]) {
    const old = deferred(); let count = 0
    const context = await panel({ api: { listChapterMetadata: async (id) => ++count === 1 ? chapters(id) : old.promise } })
    assert.equal(context.vm.currentChapterId.value, 'novel-a-chapter-2')
    const reading = context.vm.load(); context.vm.currentChapterId.value = selection
    old.resolve(chapters()); await reading
    assert.equal(context.vm.currentChapterId.value, selection)
    context.dispose()
  }
})

test('当前item身份和chapter目录归属双重门禁，不发送错作品/旧卡/外部章节组合', async () => {
  const context = await panel(), oldItem = context.vm.groupItems('planted')[0]
  await context.vm.setPlanted(oldItem, 'novel-b-chapter-1')
  context.vm.currentChapterId.value = 'novel-b-chapter-2'; await context.vm.markConsumed(oldItem)
  assert.equal(context.requests.some((row) => row.name === 'updateEntity'), false)
  await context.vm.load(); await context.vm.setPlanted(oldItem, null); await context.vm.markConsumed(oldItem)
  assert.equal(context.requests.some((row) => row.name === 'updateEntity'), false)
  context.props.novelId = 'novel-b'; await flush(); await context.vm.markConsumed(oldItem)
  assert.equal(context.requests.some((row) => row.name === 'updateEntity'), false)
  assert.equal(context.messages.filter((row) => row.type === 'warning').length, 2)
  context.dispose()
})

test('消费保存冻结原novel/item/章节，等待期间作者选择新章不被回应或reload回改', async () => {
  const pending = deferred(), context = await panel({ api: { updateEntity: async () => pending.promise } })
  context.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0])
  context.vm.currentChapterId.value = 'novel-a-chapter-2'
  assert.deepEqual(context.requests.find((row) => row.name === 'updateEntity').args, ['novel-a', 'foreshadowings', 'novel-a-foreshadow', { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-1' }])
  Object.assign(context.database.get('novel-a')[0], { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-1' })
  pending.resolve(track('novel-a', { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-1' })); await saving
  assert.equal(context.vm.currentChapterId.value, 'novel-a-chapter-2'); assert.equal(context.vm.groupItems('consumed')[0].consumed_chapter_id, 'novel-a-chapter-1')
  context.dispose()
})

test('200先完整采用真实作者字段与group，派生值待刷新且不虚构未指定关联', async () => {
  let count = 0
  const saved = track('novel-a', { status: 'suspended', content: '真实返回的新伏笔', related_characters: ['character-c'], planted_chapter_id: 'novel-a-chapter-2', consumed_chapter_id: 'novel-a-chapter-1' })
  const context = await panel({ api: { listChapterMetadata: async (id) => { if (++count > 1) throw new Error('目录暂不可用'); return chapters(id) }, updateEntity: async () => saved } })
  await context.vm.setPlanted(context.vm.groupItems('planted')[0], 'novel-a-chapter-2')
  const adopted = context.vm.groupItems('suspended')[0]
  for (const field of ['id', 'content', 'status', 'related_characters', 'planted_chapter_id', 'consumed_chapter_id']) assert.deepEqual(adopted[field], saved[field])
  assert.equal(context.vm.groupItems('planted').length, 0); assert.equal(adopted.planted_seq, null); assert.equal(adopted.suspended_chapters, null)
  assert.match(textOf(context.render()), /真实返回的新伏笔.*埋设：已指定，序号待刷新.*悬置：待刷新.*章节关联已保存/)
  assert.ok(context.messages.some((row) => /已保存，刷新未完成/.test(row.text)))
  context.dispose()
})

test('不同item可并行真实提交，各200先投影，乱序reload不能丢另一项已保存事实', async () => {
  const database = new Map([['novel-a', [track(), track('novel-a', { id: 'another-foreshadow', content: '另一伏笔' })]]])
  const oldRefresh = deferred(), first = deferred(), second = deferred(); let calls = 0, reads = 0
  const context = await panel({ database, api: {
    getForeshadowings: async () => { if (++reads === 2) return oldRefresh.promise; return structuredClone(tracking('novel-a', database.get('novel-a'))) },
    updateEntity: async (_novel, _path, id, payload) => {
      await (++calls === 1 ? first.promise : second.promise)
      const row = database.get('novel-a').find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
    },
  } })
  const [a, b] = context.vm.groupItems('planted'), one = context.vm.markConsumed(a), two = context.vm.setPlanted(b, null)
  assert.equal(calls, 2)
  second.resolve(); await flush()
  assert.equal(context.vm.groupItems('planted').find((row) => row.id === b.id).planted_chapter_id, '')
  first.resolve(); await one
  const current = JSON.parse(JSON.stringify(context.vm.tracking.value))
  oldRefresh.resolve(tracking('novel-a', [track(), track('novel-a', { id: 'another-foreshadow' })])); await two
  assert.deepEqual(context.vm.tracking.value, current); assert.equal(context.vm.groupItems('consumed').length, 1)
  assert.equal(context.vm.groupItems('planted')[0].planted_chapter_id, '')
  context.dispose()
})

test('写前已挂起read不能把消费200投影回退，最新refresh失败仍保留消费状态', async () => {
  const old = deferred(); let reads = 0
  const context = await panel({ api: { getForeshadowings: async () => ++reads === 1 ? tracking() : reads === 2 ? old.promise : Promise.reject(new Error('后置刷新失败')) } })
  const item = context.vm.groupItems('planted')[0], reading = context.vm.load()
  await context.vm.markConsumed(item)
  assert.equal(context.vm.groupItems('consumed').length, 1)
  old.resolve(tracking()); await reading
  assert.equal(context.vm.groupItems('consumed').length, 1); assert.equal(context.vm.groupItems('planted').length, 0)
  assert.ok(context.messages.some((row) => /已保存，刷新未完成/.test(row.text)))
  context.dispose()
})

test('作品ABA期间sameitem生命期写锁保留，B其他item可写且旧回应静默不加载新来源', async () => {
  for (const outcome of ['success', 'failure']) {
    const oldA = deferred(), oldB = deferred(); let calls = 0
    const context = await panel({ api: { updateEntity: async () => ++calls === 1 ? oldA.promise : oldB.promise } })
    const a = context.vm.markConsumed(context.vm.groupItems('planted')[0])
    context.props.novelId = 'novel-b'; await flush()
    const b = context.vm.setPlanted(context.vm.groupItems('planted')[0], null)
    assert.equal(calls, 2)
    context.props.novelId = 'novel-a'; await flush()
    const currentItem = context.vm.groupItems('planted')[0]
    assert.equal(context.vm.itemSaving(currentItem), true)
    await context.vm.setPlanted(currentItem, null); await context.vm.markConsumed(currentItem)
    assert.equal(calls, 2)
    const requests = context.requests.length, currentChapter = context.vm.currentChapterId.value
    if (outcome === 'success') oldA.resolve(track('novel-a', { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-2' })); else oldA.reject(new Error('旧A更新失败'))
    await a
    assert.equal(context.vm.itemSaving(currentItem), false); assert.equal(context.vm.groupItems('planted')[0].status, 'planted')
    assert.equal(context.vm.currentChapterId.value, currentChapter); assert.equal(context.requests.length, requests); assert.equal(context.messages.length, 0)
    oldB.resolve(track('novel-b', { planted_chapter_id: '' })); await b
    assert.equal(context.requests.length, requests); assert.equal(context.messages.length, 0)
    context.dispose()
  }
})

test('旧保存post-refresh finally不解下一次同item写锁，旧错误不干扰新操作', async () => {
  const oldRead = deferred(), currentSave = deferred(); let writes = 0, reads = 0
  const context = await panel({ api: {
    getForeshadowings: async () => ++reads === 1 ? tracking() : reads === 2 ? oldRead.promise : tracking('novel-a', [track('novel-a', { status: 'consumed', planted_chapter_id: '', consumed_chapter_id: 'novel-a-chapter-2' })]),
    updateEntity: async () => ++writes === 1 ? track('novel-a', { status: 'consumed', consumed_chapter_id: 'novel-a-chapter-2' }) : currentSave.promise,
  } })
  const first = context.vm.markConsumed(context.vm.groupItems('planted')[0]); await flush()
  const second = context.vm.setPlanted(context.vm.groupItems('consumed')[0], null)
  oldRead.reject(new Error('旧刷新错误')); await first
  assert.equal(context.vm.itemSaving(context.vm.groupItems('consumed')[0]), true)
  assert.equal(context.messages.some((row) => row.text.includes('旧刷新错误')), false)
  currentSave.resolve(track('novel-a', { status: 'consumed', planted_chapter_id: '', consumed_chapter_id: 'novel-a-chapter-2' })); await second
  assert.equal(context.vm.itemSaving(context.vm.groupItems('consumed')[0]), false)
  context.dispose()
})

test('明确400/404不改本地实体，释放自身锁后允许作者修正并显式重试', async () => {
  for (const status of [400, 404]) {
    let count = 0
    const context = await panel({ api: { updateEntity: async () => { if (++count === 1) throw Object.assign(new Error('章节关联被拒绝'), { status }); return track('novel-a', { planted_chapter_id: '' }) } } })
    const item = context.vm.groupItems('planted')[0]
    await context.vm.setPlanted(item, null)
    assert.equal(context.vm.groupItems('planted')[0].planted_chapter_id, 'novel-a-chapter-1')
    assert.equal(context.vm.itemSaving(item), false); assert.equal(context.vm.itemNeedsRefresh(item), false)
    assert.equal(context.messages[0].type, 'error')
    await context.vm.setPlanted(item, null); assert.equal(count, 2)
    context.dispose()
  }
})

test('未知网络或异常200不谎报已保存，旧动作禁重交并提供显式刷新核对', async () => {
  for (const outcome of ['network', 'shape']) {
    let count = 0
    const context = await panel({ api: { updateEntity: async () => { count += 1; if (outcome === 'network') throw new Error('回应丢失'); return { id: 'novel-a-foreshadow', novel_id: 'novel-b' } } } })
    const item = context.vm.groupItems('planted')[0]
    await context.vm.markConsumed(item); assert.equal(context.vm.itemNeedsRefresh(item), true)
    await context.vm.markConsumed(item); await context.vm.setPlanted(item, null); assert.equal(count, 1)
    assert.match(textOf(context.render()), /保存结果待核对，请先刷新/)
    assert.equal(context.messages.some((row) => row.type === 'success'), false)
    assert.ok(nodes(context.render(), (node) => node.type === 'nbutton' && textOf(node).includes('标记已消费')).every((node) => node.props.disabled))
    await context.vm.load(); assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted')[0]), false)
    context.dispose()
  }
})

test('卸载后的旧mutation成功失败不toast不refresh，错来源read不能装载目录', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = deferred(), context = await panel({ api: { updateEntity: async () => pending.promise } })
    const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0]), requests = context.requests.length
    context.dispose()
    if (outcome === 'success') pending.resolve(track('novel-a', { status: 'consumed' })); else pending.reject(new Error('卸载旧错误'))
    await saving
    assert.equal(context.messages.length, 0); assert.equal(context.requests.length, requests)
  }
  const context = await panel({ api: { listChapterMetadata: async () => chapters('novel-b') } })
  assert.equal(context.vm.tracking.value, null); assert.deepEqual(context.vm.chapters.value, [])
  assert.equal(context.vm.currentChapterId.value, null); assert.match(context.messages[0].text, /当前作品不一致/)
  context.dispose()
})

test('旧scope完成使返回A的提交前snapshot禁写，提交前启动的迟到read不能解gate', async () => {
  const commit = deferred(), staleRead = deferred(); let aReads = 0, writes = 0
  const database = new Map([['novel-a', [track(), track('novel-a', { id: 'other-foreshadow' })]], ['novel-b', [track('novel-b')]]])
  const context = await panel({ database, api: {
    getForeshadowings: async (id) => id === 'novel-a' && ++aReads === 3 ? staleRead.promise : structuredClone(tracking(id, database.get(id))),
    updateEntity: async (novelId, _path, id, payload) => {
      writes += 1; await commit.promise
      const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
    },
  } })
  context.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0])
  context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush()
  const staleSnapshot = structuredClone(tracking('novel-a', database.get('novel-a'))), reading = context.vm.load()
  commit.resolve(); await saving
  const item = context.vm.groupItems('planted').find((row) => row.id === 'novel-a-foreshadow')
  assert.equal(context.vm.itemSaving(item), false); assert.equal(context.vm.itemNeedsRefresh(item), true)
  assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted').find((row) => row.id === 'other-foreshadow')), false)
  context.vm.currentChapterId.value = 'novel-a-chapter-2'
  await context.vm.markConsumed(item); await context.vm.setPlanted(item, null); assert.equal(writes, 1)
  assert.match(textOf(context.render()), /保存结果待核对，请先刷新/); assert.equal(context.messages.length, 0)
  staleRead.resolve(staleSnapshot); await reading
  const stillStale = context.vm.groupItems('planted').find((row) => row.id === item.id)
  assert.equal(context.vm.itemNeedsRefresh(stillStale), true); assert.equal(context.vm.loading.value, false)
  await context.vm.markConsumed(stillStale); assert.equal(writes, 1)
  await context.vm.load(); assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('consumed')[0]), false)
  await context.vm.markConsumed(context.vm.groupItems('consumed')[0]); assert.equal(writes, 1)
  assert.equal(context.vm.groupItems('consumed')[0].consumed_chapter_id, 'novel-a-chapter-1')
  assert.equal(context.vm.currentChapterId.value, 'novel-a-chapter-2'); context.dispose()
})

test('原API回应迟到但新epoch快照已包含真实200作者字段，不无谓封禁关联操作', async () => {
  const response = deferred(); let writes = 0
  const database = new Map([['novel-a', [track()]], ['novel-b', [track('novel-b')]]])
  const context = await panel({ database, api: { updateEntity: async (novelId, _path, id, payload) => {
    const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload)
    const saved = structuredClone(row)
    if (++writes === 1) await response.promise
    return saved
  } } })
  context.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0])
  context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush()
  assert.equal(context.vm.groupItems('consumed')[0].consumed_chapter_id, 'novel-a-chapter-1')
  const selected = context.vm.currentChapterId.value, reads = context.requests.length
  response.resolve(); await saving
  assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('consumed')[0]), false)
  assert.equal(context.vm.currentChapterId.value, selected); assert.equal(context.requests.length, reads); assert.equal(context.messages.length, 0)
  await context.vm.setPlanted(context.vm.groupItems('consumed')[0], null)
  assert.equal(writes, 2); assert.equal(context.vm.groupItems('consumed')[0].planted_chapter_id, '')
  context.dispose()
})

test('旧scope未知回执跨ABA保留核对gate，提交前晚读及再次往返都不能解除', async () => {
  const response = deferred(), oldRead = deferred(); let aReads = 0, writes = 0
  const context = await panel({ api: {
    getForeshadowings: async (id) => id === 'novel-a' && ++aReads === 3 ? oldRead.promise : tracking(id),
    updateEntity: async () => { writes += 1; await response.promise; throw new Error('旧请求回应丢失') },
  } })
  const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0])
  context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush()
  const reading = context.vm.load()
  response.resolve(); await saving
  assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted')[0]), true); assert.equal(context.messages.length, 0)
  oldRead.resolve(tracking()); await reading
  assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted')[0]), true)
  await context.vm.markConsumed(context.vm.groupItems('planted')[0]); assert.equal(writes, 1)
  await context.vm.load(); assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted')[0]), false)
  context.dispose()
})

test('旧scope明确400/404失败不污染新快照或无谓封禁，失败不回写作者选择', async () => {
  for (const status of [400, 404]) {
    const response = deferred()
    const context = await panel({ api: { updateEntity: async () => { await response.promise; throw Object.assign(new Error('服务明确拒绝关联'), { status }) } } })
    const saving = context.vm.markConsumed(context.vm.groupItems('planted')[0])
    context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush()
    context.vm.currentChapterId.value = 'novel-a-chapter-1'; const requests = context.requests.length
    response.resolve(); await saving
    assert.equal(context.vm.itemNeedsRefresh(context.vm.groupItems('planted')[0]), false)
    assert.equal(context.vm.currentChapterId.value, 'novel-a-chapter-1'); assert.equal(context.messages.length, 0)
    assert.equal(context.requests.length, requests); context.dispose()
  }
})

test('同编译模块卸载重挂期间共用未完成写锁，旧提交完成后必须核对新实例的旧快照', async () => {
  const commit = deferred(); let writes = 0
  const database = new Map([['novel-a', [track()]]])
  const old = await panel({ database, api: { updateEntity: async (novelId, _path, id, payload) => {
    writes += 1; await commit.promise
    const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
  } } })
  old.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = old.vm.markConsumed(old.vm.groupItems('planted')[0])
  old.dispose()
  const fresh = await old.mountAgain(), item = fresh.vm.groupItems('planted')[0]
  assert.equal(fresh.vm.itemSaving(item), true)
  await fresh.vm.markConsumed(item); await fresh.vm.setPlanted(item, null)
  assert.equal(writes, 1)
  commit.resolve(); await saving
  assert.equal(fresh.vm.itemSaving(item), false); assert.equal(fresh.vm.itemNeedsRefresh(item), true)
  assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
  assert.match(textOf(fresh.render()), /保存结果待核对，请先刷新/)
  await fresh.vm.markConsumed(item); await fresh.vm.setPlanted(item, null); assert.equal(writes, 1)
  await fresh.vm.load()
  const consumed = fresh.vm.groupItems('consumed')[0]
  assert.equal(consumed.consumed_chapter_id, 'novel-a-chapter-1'); assert.equal(fresh.vm.itemNeedsRefresh(consumed), false)
  await fresh.vm.markConsumed(consumed); assert.equal(writes, 1)
  fresh.dispose()
})

test('旧实例未知回执跨重挂保留核对gate，完成前发起的晚读不能当作已核对', async () => {
  const response = deferred(), lateRead = deferred(); let reads = 0, writes = 0
  const database = new Map([['novel-a', [track()]]])
  const old = await panel({ database, api: {
    getForeshadowings: async (id) => ++reads === 3 ? lateRead.promise : structuredClone(tracking(id, database.get(id))),
    updateEntity: async (novelId, _path, id, payload) => {
      writes += 1; await response.promise
      Object.assign(database.get(novelId).find((row) => row.id === id), payload)
      throw new Error('保存已提交但回应丢失')
    },
  } })
  old.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = old.vm.markConsumed(old.vm.groupItems('planted')[0]); old.dispose()
  const fresh = await old.mountAgain(), beforeCommit = structuredClone(tracking('novel-a', database.get('novel-a')))
  const reading = fresh.vm.load()
  response.resolve(); await saving
  assert.equal(fresh.vm.itemNeedsRefresh(fresh.vm.groupItems('planted')[0]), true)
  lateRead.resolve(beforeCommit); await reading
  const item = fresh.vm.groupItems('planted')[0]
  assert.equal(fresh.vm.loading.value, false); assert.equal(fresh.vm.itemNeedsRefresh(item), true)
  await fresh.vm.markConsumed(item); await fresh.vm.setPlanted(item, null); assert.equal(writes, 1)
  assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
  await fresh.vm.load()
  const consumed = fresh.vm.groupItems('consumed')[0]
  assert.equal(fresh.vm.itemNeedsRefresh(consumed), false); assert.equal(consumed.consumed_chapter_id, 'novel-a-chapter-1')
  await fresh.vm.markConsumed(consumed); assert.equal(writes, 1)
  fresh.dispose()
})

test('旧实例明确400或404失败释放共享锁，新实例仍能显式修正且不永久封禁', async () => {
  for (const status of [400, 404]) {
    const response = deferred(); let writes = 0
    const database = new Map([['novel-a', [track()]]])
    const old = await panel({ database, api: { updateEntity: async (novelId, _path, id, payload) => {
      if (++writes === 1) { await response.promise; throw Object.assign(new Error('服务明确拒绝'), { status }) }
      const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
    } } })
    const saving = old.vm.markConsumed(old.vm.groupItems('planted')[0]); old.dispose()
    const fresh = await old.mountAgain(), item = fresh.vm.groupItems('planted')[0]
    assert.equal(fresh.vm.itemSaving(item), true)
    response.resolve(); await saving
    assert.equal(fresh.vm.itemSaving(item), false); assert.equal(fresh.vm.itemNeedsRefresh(item), false)
    assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
    await fresh.vm.setPlanted(item, null)
    assert.equal(writes, 2); assert.equal(fresh.vm.groupItems('planted')[0].planted_chapter_id, '')
    fresh.dispose()
  }
})

test('新实例已读回旧保存的完整实际字段，迟到200不会无谓要求再次刷新', async () => {
  const response = deferred(); let writes = 0
  const database = new Map([['novel-a', [track()]]])
  const old = await panel({ database, api: { updateEntity: async (novelId, _path, id, payload) => {
    const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload)
    const saved = structuredClone(row); if (++writes === 1) await response.promise; return saved
  } } })
  old.vm.currentChapterId.value = 'novel-a-chapter-1'
  const saving = old.vm.markConsumed(old.vm.groupItems('planted')[0]); old.dispose()
  const fresh = await old.mountAgain(), item = fresh.vm.groupItems('consumed')[0], requestCount = fresh.requests.length
  assert.equal(item.consumed_chapter_id, 'novel-a-chapter-1'); assert.equal(fresh.vm.itemSaving(item), true)
  response.resolve(); await saving
  assert.equal(fresh.vm.itemNeedsRefresh(item), false); assert.equal(fresh.vm.itemSaving(item), false)
  assert.equal(fresh.requests.length, requestCount); assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
  await fresh.vm.setPlanted(item, null)
  assert.equal(writes, 2); assert.equal(fresh.vm.groupItems('consumed')[0].planted_chapter_id, '')
  fresh.dispose()
})

test('跨实例操作身份全页唯一，旧post-read的finally不能解新实例锁或停止新读取', async () => {
  const oldRead = deferred(), currentRead = deferred(), nextSave = deferred(); let reads = 0, writes = 0
  const database = new Map([['novel-a', [track()]]])
  const old = await panel({ database, api: {
    getForeshadowings: async (id) => { reads += 1; return reads === 2 ? oldRead.promise : reads === 4 ? currentRead.promise : structuredClone(tracking(id, database.get(id))) },
    updateEntity: async (novelId, _path, id, payload) => {
      if (++writes === 2) await nextSave.promise
      const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
    },
  } })
  const first = old.vm.markConsumed(old.vm.groupItems('planted')[0]); await flush()
  assert.equal(old.vm.loading.value, true); assert.equal(old.messages.length, 1)
  old.dispose()
  const fresh = await old.mountAgain(), item = fresh.vm.groupItems('consumed')[0]
  const second = fresh.vm.setPlanted(item, null), reading = fresh.vm.load()
  assert.equal(fresh.vm.itemSaving(item), true); assert.equal(fresh.vm.loading.value, true)
  oldRead.reject(new Error('旧实例后置读取失败')); await first
  assert.equal(fresh.vm.itemSaving(item), true); assert.equal(fresh.vm.loading.value, true)
  assert.equal(old.messages.length, 1); assert.equal(fresh.messages.length, 0)
  await fresh.vm.setPlanted(item, null); assert.equal(writes, 2)
  currentRead.resolve(structuredClone(tracking('novel-a', database.get('novel-a')))); await reading
  assert.equal(fresh.vm.itemSaving(fresh.vm.groupItems('consumed')[0]), true)
  nextSave.resolve(); await second
  assert.equal(fresh.vm.itemSaving(fresh.vm.groupItems('consumed')[0]), false)
  assert.equal(fresh.vm.groupItems('consumed')[0].planted_chapter_id, '')
  fresh.dispose()
})

test('共享registry仅按作品和item隔离，旧实例未完成写入不锁其它伏笔或作品', async () => {
  const response = deferred(); let writes = 0
  const database = new Map([['novel-a', [track(), track('novel-a', { id: 'other-foreshadow' })]], ['novel-b', [track('novel-b')]]])
  const old = await panel({ database, api: { updateEntity: async (novelId, _path, id, payload) => {
    writes += 1; if (novelId === 'novel-a' && id === 'novel-a-foreshadow') await response.promise
    const row = database.get(novelId).find((item) => item.id === id); Object.assign(row, payload); return structuredClone(row)
  } } })
  const first = old.vm.markConsumed(old.vm.groupItems('planted')[0]); old.dispose()
  const a = await old.mountAgain(), b = await old.mountAgain({ novelId: 'novel-b' })
  const blocked = a.vm.groupItems('planted').find((row) => row.id === 'novel-a-foreshadow')
  const independent = a.vm.groupItems('planted').find((row) => row.id === 'other-foreshadow')
  assert.equal(a.vm.itemSaving(blocked), true); assert.equal(a.vm.itemSaving(independent), false)
  assert.equal(b.vm.itemSaving(b.vm.groupItems('planted')[0]), false)
  await a.vm.markConsumed(independent); await b.vm.markConsumed(b.vm.groupItems('planted')[0]); assert.equal(writes, 3)
  response.resolve(); await first
  assert.equal(a.vm.itemNeedsRefresh(a.vm.groupItems('planted')[0]), true)
  assert.equal(b.vm.itemNeedsRefresh(b.vm.groupItems('consumed')[0]), false)
  assert.equal(old.messages.length, 0); assert.equal(a.messages.length, 1); assert.equal(b.messages.length, 1)
  a.dispose(); b.dispose()
})
