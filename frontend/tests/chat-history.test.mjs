import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { for (let i = 0; i < 12; i++) { await nextTick(); await Promise.resolve() } }
const until = async check => { for (let i = 0; i < 300 && !check(); i++) { await settle(); await new Promise(resolve => setTimeout(resolve, 2)) } assert.ok(check(), '真实异步读取没有达到预期条件') }
const msg = (seq, cid = 'a') => ({ id: `${cid}-${seq}`, conversation_id: cid, role: seq % 2 ? 'user' : 'assistant', content: `第${seq}条😀`, meta: null, created_at: '相同时间戳' })
const range = (start, end, cid = 'a') => Array.from({ length: end - start + 1 }, (_, i) => msg(start + i, cid))
const page = (items, more = false, cid = 'a', novel = 'novel-a') => ({ conversation_id: cid, novel_id: novel, items, has_more: more, next_before_message_id: more ? items[0].id : null })
const batch = (ids, cid = 'a', novel = 'novel-a', actions = []) => ({ conversation_id: cid, novel_id: novel, actions_by_message_id: Object.fromEntries(ids.map(id => [id, actions])) })
const action = { action_type: 'chapter', status: 'completed', result_id: 'chapter-a', result_summary: { chapter_id: 'chapter-a', title: '真实章' }, target_deleted: false }
function nodes(value, predicate, result = []) {
  if (!value || typeof value !== 'object') return result
  if (predicate(value)) result.push(value)
  if (Array.isArray(value.children)) value.children.forEach(item => nodes(item, predicate, result))
  else if (value.children && typeof value.children === 'object') for (const slot of Object.values(value.children)) if (typeof slot === 'function') {
    const children = slot(); (Array.isArray(children) ? children : [children]).forEach(item => nodes(item, predicate, result))
  }
  return result
}
let sequence = 0
async function runtime(overrides = {}) {
  const calls = [], contexts = [], key = `__history${++sequence}`
  let active
  const api = {
    listMessageWindow: async () => page(range(71, 120), true),
    listMessageActionsBatch: async (cid, ids) => batch(ids, cid, cid === 'a' ? 'novel-a' : 'novel-b'),
    listMessageActions: async () => { throw new Error('Managed bubbles must never call the single-message endpoint') },
    sendMessage: async () => {}, ...overrides,
  }
  const harness = { api: Object.fromEntries(Object.entries(api).map(([name, fn]) => [name, (...args) => { calls.push({ name, args }); return fn(...args) }])),
    NButton: { name: 'NButton' }, NInput: { name: 'NInput' }, NEmpty: {}, NModal: {}, NTag: {},
    useMessage: () => { const context = active; return Object.fromEntries(['success', 'warning', 'error'].map(type => [type, text => context.notices.push({ type, text })])) },
    onBeforeUnmount: fn => { active.unmount = fn },
  }
  globalThis[key] = harness
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  const helperURL = url(compile((await readFile(new URL('../src/utils/chatSubmission.ts', import.meta.url), 'utf8')).replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// registry${key}`)
  const generationURL = url(compile((await readFile(new URL('../src/utils/chapterGeneration.ts', import.meta.url), 'utf8')).replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// generation${key}`)
  const consistencyURL = url(compile(await readFile(new URL('../src/utils/generationConsistency.ts', import.meta.url), 'utf8')))
  async function component(name) {
    const descriptor = parse(await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor
    const script = compileScript(descriptor, { id: name + key })
    const source = `const __harness=globalThis.${key}\n` + script.content
      .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
      .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1}=__harness')
      .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const {api}=__harness')
      .replace(/from ['"]\.\.\/utils\/chatSubmission['"]/g, `from '${helperURL}'`)
      .replace(/from ['"]\.\.\/utils\/chapterGeneration['"]/g, `from '${generationURL}'`)
      .replace(/from ['"]\.\.\/utils\/generationConsistency['"]/g, `from '${consistencyURL}'`)
      .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1={name:"$1"}')
      .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
    const definition = (await import(url(compile(source)))).default
    const template = compileTemplate({ source: descriptor.template.content, filename: name + '.vue', id: name + key, compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(template.errors, [])
    const rendering = await import(url(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
    return async initial => {
      const context = { props: reactive(initial), notices: [], events: [] }
      active = context
      const scope = effectScope()
      context.vm = scope.run(() => definition.setup(context.props, { expose() {}, emit: (...args) => context.events.push(args) }))
      context.dispose = () => { context.unmount?.(); scope.stop() }
      context.render = () => rendering.render({}, [], context.props, proxyRefs(context.vm), {}, {})
      contexts.push(context); await settle(); return context
    }
  }
  const mountWindow = await component('ChatWindow'), mountBubble = await component('MessageBubble')
  return { calls, helper: await import(helperURL), mount: extra => mountWindow({ conversation: { id: 'a', novel_id: 'novel-a', title: 'A' }, novels: [], ...extra }),
    bubble: props => mountBubble(props), close: () => contexts.forEach(item => item.dispose()) }
}

test('最近50窗口只一批25动作，真实父模板传入managed状态，50个实际Bubble不逐条GET', async () => {
  const r = await runtime(), c = await r.mount(); await until(() => c.vm.messages.value.length === 50)
  const bubbles = nodes(c.render(), node => node.type?.name === 'MessageBubble')
  assert.equal(bubbles.length, 50)
  for (const node of bubbles) { const child = await r.bubble({ message: node.props.message, novelId: node.props['novel-id'], actions: node.props.actions, actionsStatus: node.props['actions-status'] }); if (child.vm.isAssistant.value) assert.equal(child.vm.canApply.value, true) }
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 1)
  assert.equal(r.calls.filter(row => row.name === 'listMessageActionsBatch').length, 1)
  assert.equal(r.calls.find(row => row.name === 'listMessageActionsBatch').args[1].length, 25)
  assert.equal(r.calls.filter(row => row.name === 'listMessageActions').length, 0)
  assert.ok(nodes(c.render(), node => node.props?.onClick === c.vm.loadEarlier).length)
  r.close()
})

test('同时间戳按服务端顺序加载更早，双击只一请求，最新刷新保已加载旧页和原游标', async () => {
  const older = wait(); let reads = 0
  const r = await runtime({ listMessageWindow: (_cid, before) => { reads++; return before === 'a-71' ? older.promise : page(reads === 1 ? range(71, 120) : range(101, 150), true) } }), c = await r.mount()
  await until(() => c.vm.messages.value.length === 50); c.vm.input.value = '作者新输入'
  const loading = c.vm.loadEarlier(); await c.vm.loadEarlier(); assert.equal(reads, 2)
  older.resolve(page(range(21, 70), true)); await loading
  assert.deepEqual(c.vm.messages.value.map(item => item.id), range(21, 120).map(item => item.id))
  await c.vm.loadMessages()
  assert.deepEqual(c.vm.messages.value.map(item => item.id), range(21, 150).map(item => item.id))
  assert.equal(c.vm.currentState.value.beforeMessageId, 'a-21'); assert.equal(c.vm.input.value, '作者新输入')
  assert.equal(new Set(c.vm.messages.value.map(item => item.id)).size, 130); r.close()
})

test('最新不相接窗口保旧页且重设缺口游标，旧更早读取不能倒退新游标', async () => {
  const older = wait(); let latest = 0
  const r = await runtime({ listMessageWindow: (_cid, before) => before ? older.promise : page(++latest === 1 ? range(71, 120) : range(201, 250), true) }), c = await r.mount()
  await until(() => c.vm.messages.value.length === 50)
  const old = c.vm.loadEarlier(); await c.vm.loadMessages()
  older.resolve(page(range(21, 70), true)); await old
  assert.equal(c.vm.currentState.value.beforeMessageId, 'a-201')
  assert.deepEqual(c.vm.messages.value.map(item => item.id), [...range(71, 120), ...range(201, 250)].map(item => item.id))
  assert.equal(c.vm.currentState.value.olderLoading, false); r.close()
})

test('更早读取失败保留历史和新稿，明确再读最后页后移除更多按钮', async () => {
  let fail = true
  const r = await runtime({ listMessageWindow: (_cid, before) => { if (!before) return page(range(51, 100), true); if (fail) throw new Error('更早读取真实失败'); return page(range(1, 50)) } }), c = await r.mount()
  await until(() => c.vm.messages.value.length === 50); c.vm.input.value = '不可被更早读取清掉'
  await c.vm.loadEarlier(); assert.equal(c.vm.messages.value.length, 50); assert.equal(c.vm.currentState.value.hasEarlier, true)
  assert.equal(c.vm.currentState.value.olderLoading, false); fail = false; await c.vm.loadEarlier()
  assert.deepEqual(c.vm.messages.value.map(item => item.id), range(1, 100).map(item => item.id))
  assert.equal(c.vm.currentState.value.hasEarlier, false); assert.equal(c.vm.input.value, '不可被更早读取清掉'); r.close()
})

test('批量动作失败不能当作未落地，真实Bubble禁应用并显式重试后显示已删目标', async () => {
  let fail = true
  const r = await runtime({ listMessageWindow: () => page([msg(2)]), listMessageActionsBatch: (cid, ids) => { if (fail) throw new Error('batch503'); return batch(ids, cid, 'novel-a', [{ ...action, target_deleted: true }]) } }), c = await r.mount()
  await until(() => c.vm.currentState.value.actions['a-2']?.status === 'failed')
  const node = nodes(c.render(), node => node.type?.name === 'MessageBubble')[0]
  const b = await r.bubble({ message: node.props.message, novelId: 'novel-a', actions: node.props.actions, actionsStatus: node.props['actions-status'] })
  assert.equal(b.vm.canApply.value, false); assert.ok(nodes(b.render(), value => value.children === '重试动作记录').length)
  b.vm.loadStandaloneActions(); assert.deepEqual(b.events, [['reload-actions']]); fail = false
  c.vm.reloadMessageActions('a-2'); await until(() => c.vm.currentState.value.actions['a-2'].status === 'ready')
  b.props.actions = c.vm.currentState.value.actions['a-2'].items; b.props.actionsStatus = 'ready'; await settle()
  assert.equal(b.vm.chapterDeleted.value, true); assert.equal(b.vm.chapterResult.value.chapter_id, 'chapter-a')
  assert.equal(r.calls.filter(row => row.name === 'listMessageActions').length, 0); r.close()
})

test('动作逐消息token保护同模块重挂，新批次pending不被旧实例finally改为失败', async () => {
  const oldBatch = wait(), newBatch = wait(); let requests = 0
  const r = await runtime({ listMessageWindow: () => page([msg(2)]), listMessageActionsBatch: () => ++requests === 1 ? oldBatch.promise : newBatch.promise })
  const first = await r.mount(); await until(() => requests === 1); first.dispose()
  const next = await r.mount(); await until(() => requests === 2)
  oldBatch.resolve(batch(['a-2'])); await settle()
  assert.equal(next.vm.currentState.value.actions['a-2'].status, 'pending')
  newBatch.resolve(batch(['a-2'], 'a', 'novel-a', [action])); await until(() => next.vm.currentState.value.actions['a-2'].status === 'ready')
  assert.equal(next.vm.currentState.value.actions['a-2'].items[0].result_id, 'chapter-a'); r.close()
})

test('缺字段旧响应或伪窗口不替换历史，不退回全量GET也不发动作请求', async () => {
  const invalid = [[], { ...page([msg(2)]), conversation_id: 'b' }, { ...page([msg(2)]), novel_id: 'foreign' },
    page([msg(2), msg(2)]), page([msg(2, 'b')]), { ...page([msg(2)], true), next_before_message_id: 'wrong' },
    { ...page([msg(2)]), has_more: 'false' }, page([{ ...msg(2), meta: [] }])]
  for (const value of invalid) {
    const r = await runtime({ listMessageWindow: () => value }), c = await r.mount(); await settle()
    assert.equal(c.vm.messages.value.length, 0); assert.equal(c.vm.currentState.value.historyLoaded, false)
    assert.equal(r.calls.filter(row => row.name === 'listMessageActionsBatch').length, 0)
    assert.equal(c.vm.currentState.value.historyLoading, false); r.close()
  }
})

test('批量缺ID或错误来源不能宣称动作已核对，合法已返旧记录仍保留但禁新应用', async () => {
  const invalid = [{ ...batch(['a-2']), conversation_id: 'b' }, { ...batch(['a-2']), novel_id: 'foreign' },
    { ...batch(['a-2']), actions_by_message_id: {} }, batch(['a-2', 'extra']), batch(['a-2'], 'a', 'novel-a', [{ ...action, target_deleted: 'false' }])]
  for (const value of invalid) {
    let calls = 0
    const r = await runtime({ listMessageWindow: () => page([msg(2)]), listMessageActionsBatch: () => ++calls === 1 ? batch(['a-2'], 'a', 'novel-a', [action]) : value }), c = await r.mount()
    await until(() => c.vm.currentState.value.actions['a-2']?.status === 'ready'); c.vm.reloadMessageActions('a-2')
    await until(() => c.vm.currentState.value.actions['a-2'].status === 'failed')
    assert.equal(c.vm.currentState.value.actions['a-2'].items[0].result_id, 'chapter-a'); r.close()
  }
})

test('更早读取A→B→A迟到不覆盖新来源，目录删除不让旧分页复活state', async () => {
  const early = wait(); let latest = 0
  const r = await runtime({ listMessageWindow: (cid, before) => before ? early.promise : page(range(71, 120, cid), true, cid, cid === 'a' ? 'novel-a' : 'novel-b') }), c = await r.mount({ conversationIds: ['a', 'b'] })
  await until(() => c.vm.messages.value.length === 50); const loading = c.vm.loadEarlier()
  const a = c.props.conversation; c.props.conversation = { id: 'b', novel_id: 'novel-b' }; await settle(); c.vm.input.value = 'B新稿'
  c.props.conversation = a; await settle(); c.vm.input.value = 'A新稿'
  early.resolve(page(range(21, 70), true)); await loading
  assert.equal(c.vm.messages.value.length, 50); assert.equal(c.vm.input.value, 'A新稿')
  c.props.conversationIds = []; await settle(); assert.equal(r.helper.chatSubmissionStates.size, 0)
  assert.equal(c.vm.messages.value.length, 0); r.close()
})

test('更早分页等批量动作高度稳定后再次保滚动锚点，作者主动滚动不会被拉回', async () => {
  for (const userScroll of [false, true]) {
    const actions = wait(); let batchCount = 0
    const r = await runtime({ listMessageWindow: (_cid, before) => before ? page(range(21, 70), true) : page(range(71, 120), true),
      listMessageActionsBatch: (cid, ids) => ++batchCount === 1 ? batch(ids) : actions.promise }), c = await r.mount()
    await until(() => c.vm.messages.value.length === 50)
    let readyHeight = 0
    const element = { scrollTop: 100, get scrollHeight() { return c.vm.messages.value.length * 10 + readyHeight },
      querySelector: () => ({ isConnected: true, getBoundingClientRect: () => ({ top: 300 + (c.vm.messages.value.length - 50) * 10 + readyHeight - element.scrollTop }) }) }
    c.vm.listEl.value = element; const loading = c.vm.loadEarlier(); await until(() => batchCount === 2)
    assert.equal(element.scrollTop, 600)
    if (userScroll) element.scrollTop = 777
    readyHeight = 40; actions.resolve(batch(range(21, 70).filter(item => item.role === 'assistant').map(item => item.id)))
    await loading; assert.equal(element.scrollTop, userScroll ? 777 : 640); r.close()
  }
})

test('全局窗口普通聊天直接ready空动作，不调用新batch或旧global400单条接口', async () => {
  const r = await runtime({ listMessageWindow: () => page([msg(2)], false, 'a', null) }), c = await r.mount({ conversation: { id: 'a', novel_id: null } })
  await until(() => c.vm.messages.value.length === 1)
  assert.equal(c.vm.currentState.value.actions['a-2'].status, 'ready')
  assert.equal(r.calls.filter(row => ['listMessageActionsBatch', 'listMessageActions'].includes(row.name)).length, 0)
  c.vm.input.value = '全局新构思'; assert.equal(c.vm.dirty.value, true); r.close()
})

test('旧动作摘要合法JSON的null和数组不使整批失败，settings确认保留且不伪造目标ID', async () => {
  const legacy = [{ ...action, action_type: 'settings', result_summary: null }, { ...action, result_summary: ['旧数组摘要'] }]
  const r = await runtime({ listMessageWindow: () => page([msg(2)]), listMessageActionsBatch: (_cid, ids) => batch(ids, 'a', 'novel-a', legacy) }), c = await r.mount()
  await until(() => c.vm.currentState.value.actions['a-2']?.status === 'ready')
  const b = await r.bubble({ message: msg(2), novelId: 'novel-a', actions: c.vm.currentState.value.actions['a-2'].items, actionsStatus: 'ready' })
  assert.equal(b.vm.settingsApplied.value, true); assert.equal(b.vm.chapterResult.value, null)
  assert.equal(b.vm.canApply.value, true); r.close()
})

test('动作重读期间已有作者编辑卡片保持挂载并inert，未核对不能新开或提交卡片', async () => {
  const r = await runtime(), b = await r.bubble({ message: msg(2), novelId: 'novel-a', actions: [], actionsStatus: 'ready' })
  b.vm.showApply.value = true; b.vm.showOutline.value = true; b.vm.showWriting.value = true; b.vm.showSettings.value = true
  const names = value => ['ApplySuggestionCard', 'OutlinePlanCard', 'ChapterWriteCard'].includes(value.type?.name)
  assert.equal(nodes(b.render(), names).length, 4)
  b.props.actionsStatus = 'pending'; await settle()
  assert.equal(b.vm.canApply.value, false); assert.equal(nodes(b.render(), names).length, 4)
  assert.equal(nodes(b.render(), value => value.type === 'div' && value.props?.inert === true).length, 4)
  b.props.actionsStatus = 'failed'; await settle(); assert.equal(nodes(b.render(), names).length, 4)
  r.close()
})

test('真实Bubble保存meta后父缓存采用完整回包，旧窗口不能撤回标签且重挂保旧页落地状态', async () => {
  const stale = wait(); let reads = 0
  const confirmed = { ...msg(110), meta: { applied: [{ type: 'character', action: 'create', id: 'character-a' }] } }
  const saved = new Map()
  const r = await runtime({ listMessageWindow: (_cid, before) => before ? page(range(21, 70), true) : ++reads === 2 ? stale.promise : page(range(71, 120).map(item => saved.get(item.id) ?? item), true),
    updateMessage: async (_cid, id) => { const value = { ...confirmed, id }; saved.set(id, value); return value } })
  const c = await r.mount(); await until(() => c.vm.messages.value.length === 50); await c.vm.loadEarlier()
  const oldRead = c.vm.loadMessages()
  const node = nodes(c.render(), value => value.type?.name === 'MessageBubble' && value.props.message.id === 'a-110')[0]
  const b = await r.bubble({ message: node.props.message, novelId: 'novel-a', actions: node.props.actions, actionsStatus: 'ready' })
  await b.vm.onApplied(confirmed.meta.applied)
  const updated = b.events.find(([name]) => name === 'message-updated')[1]
  node.props.onMessageUpdated(updated)
  stale.resolve(page(range(71, 120), true)); await oldRead
  assert.deepEqual(c.vm.messages.value.find(item => item.id === 'a-110').meta, confirmed.meta)
  const oldNode = nodes(c.render(), value => value.type?.name === 'MessageBubble' && value.props.message.id === 'a-60')[0]
  const oldBubble = await r.bubble({ message: oldNode.props.message, novelId: 'novel-a', actions: oldNode.props.actions, actionsStatus: 'ready' })
  await oldBubble.vm.onApplied(confirmed.meta.applied)
  oldNode.props.onMessageUpdated(oldBubble.events.find(([name]) => name === 'message-updated')[1])
  c.dispose(); const next = await r.mount(); await settle()
  assert.equal(next.vm.messages.value.length, 100)
  assert.equal(next.vm.currentState.value.beforeMessageId, 'a-21')
  assert.deepEqual(next.vm.messages.value.find(item => item.id === 'a-60').meta, confirmed.meta)
  r.close()
})

test('旧页三个真实确认callback同步留父动作事实，读回等待中重挂失败不丢已确认标签', async () => {
  for (const type of ['outline', 'chapter', 'settings']) {
    const reading = wait()
    const r = await runtime({ listMessageWindow: (_cid, before) => page(before ? range(21, 70) : range(71, 120), true),
      listMessageActionsBatch: (_cid, ids) => ids.length === 1 ? reading.promise : batch(ids) })
    const c = await r.mount(); await until(() => c.vm.messages.value.length === 50); await c.vm.loadEarlier()
    const node = nodes(c.render(), value => value.type?.name === 'MessageBubble' && value.props.message.id === 'a-60')[0]
    const b = await r.bubble({ message: node.props.message, novelId: 'novel-a', actions: node.props.actions, actionsStatus: 'ready' })
    if (type === 'outline') b.vm.onOutlineApplied({ volume_id: 'actual-volume', node_ids: ['actual-volume'] })
    if (type === 'chapter') b.vm.onChapterApplied({ id: 'actual-chapter', title: '真实章' })
    if (type === 'settings') b.vm.onSettingsApplied([{ type: 'character', action: 'create', id: 'actual-character' }])
    node.props.onActionConfirmed(b.events.find(([name]) => name === 'action-confirmed')[1])
    node.props.onReloadActions(); await settle()
    assert.equal(c.vm.currentState.value.actions['a-60'].items[0].action_type, type)
    assert.equal(c.vm.currentState.value.actions['a-60'].status, 'pending')
    c.dispose(); b.dispose(); const next = await r.mount(); reading.reject(new Error('旧实例后置读回失败')); await settle()
    const entry = next.vm.currentState.value.actions['a-60']
    assert.equal(entry.status, 'failed'); assert.equal(entry.items[0].action_type, type)
    const remounted = await r.bubble({ message: msg(60), novelId: 'novel-a', actions: entry.items, actionsStatus: entry.status })
    assert.equal(remounted.vm.canApply.value, false)
    if (type === 'outline') assert.equal(remounted.vm.outlineResult.value.volume_id, 'actual-volume')
    if (type === 'chapter') assert.equal(remounted.vm.chapterResult.value.chapter_id, 'actual-chapter')
    if (type === 'settings') assert.equal(remounted.vm.settingsApplied.value, true)
    r.close()
  }
})

test('原UUID在50条窗口外仍独立核对，缺席不能推断notfound或触发自动POST', async () => {
  const raw = '原发送😀', request = wait(); let saved
  const r = await runtime({ sendMessage: async (cid, text, _h, uuid) => { saved = { request_id: uuid, user_message_id: uuid, conversation_id: cid, content: text, content_hash: createHash('sha256').update(text).digest('hex'), accepted: true, receipt_replayed: true }; throw new Error('传输未知') }, getChatSubmission: async () => saved })
  const c = await r.mount(); await until(() => c.vm.messages.value.length === 50)
  c.vm.input.value = raw; await c.vm.send(); assert.equal(c.vm.submission.value.status, 'unknown')
  await c.vm.loadMessages(); assert.equal(c.vm.messages.value.some(item => item.id === saved.request_id), false)
  assert.equal(c.vm.submission.value.status, 'unknown'); c.vm.input.value = '核对后还要保留的新稿'
  await c.vm.checkSubmission(); assert.equal(c.vm.submission.value.status, 'saved')
  assert.equal(c.vm.input.value, '核对后还要保留的新稿'); assert.equal(r.calls.filter(row => row.name === 'sendMessage').length, 1); r.close()
})

test('真实api.ts新窗口与batch请求路径/编码/JSON明确，旧全量接口仍可直接使用', async () => {
  const calls = [], original = globalThis.fetch
  globalThis.fetch = async (path, options = {}) => { calls.push({ path, options }); return new Response(JSON.stringify(path.includes('/window') ? page([]) : path.includes('/batch') ? batch(['a-2']) : []), { status: 200 }) }
  try {
    const api = (await import(url(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))).api
    await api.listMessageWindow('a'); await api.listMessageWindow('a', '原游标/😀'); await api.listMessageActionsBatch('a', ['a-2']); await api.listMessages('a')
    assert.equal(calls[0].path, '/api/v1/conversations/a/messages/window?limit=50')
    assert.ok(calls[1].path.endsWith('&before_message_id=' + encodeURIComponent('原游标/😀')))
    assert.equal(calls[2].options.method, 'POST'); assert.deepEqual(JSON.parse(calls[2].options.body), { message_ids: ['a-2'] })
    assert.equal(calls[3].path, '/api/v1/conversations/a/messages')
  } finally { globalThis.fetch = original }
})
