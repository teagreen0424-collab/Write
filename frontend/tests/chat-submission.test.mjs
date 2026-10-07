import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const flush = async () => { for (let n = 0; n < 8; n++) { await new Promise(resolve => setTimeout(resolve, 1)); await nextTick() } }
const until = async predicate => { for (let n = 0; n < 200 && !predicate(); n++) await new Promise(resolve => setTimeout(resolve, 2)); assert.ok(predicate(), '实际异步操作未达到预期状态') }
const receipt = (id, raw, requestId, patch = {}) => ({ request_id: requestId, conversation_id: id, user_message_id: requestId,
  content: raw, content_hash: createHash('sha256').update(raw).digest('hex'), accepted: true, receipt_replayed: true, ...patch })
const done = (id, requestId) => ({ type: 'done', request_id: requestId, conversation_id: id, user_message_id: requestId, assistant_message_id: randomUUID(), title: '本次回复已保存' })
let sequence = 0
async function runtime(options = {}) {
  const descriptor = parse(await readFile(new URL('../src/components/ChatWindow.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `chat-submission-${++sequence}` })
  const helperUrl = url(compile((await readFile(new URL('../src/utils/chatSubmission.ts', import.meta.url), 'utf8')).replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// registry-${sequence}`)
  const calls = [], streams = [], listeners = new Map(); let mounting
  const api = {
    listMessages: async () => [],
    getChatSubmission: async () => { throw Object.assign(new Error('原次未找到'), { status: 404, code: 'chat_message_request_not_found' }) },
    sendMessage: async (id, raw, handlers, requestId) => {
      await handlers.onUserSaved({ ...receipt(id, raw, requestId), type: 'user_saved', receipt_replayed: false })
      handlers.onToken('本次回复'); await handlers.onDone(done(id, requestId))
    }, ...options.api,
  }
  const harness = {
    // Existing author-state scenarios keep their array fixtures; expose the new window shape
    // explicitly and record the actual new API method, never claim an old full-list GET.
    api: Object.fromEntries(Object.entries(api).map(([name, fn]) => [name === 'listMessages' ? 'listMessageWindow' : name, (...args) => {
      calls.push({ name: name === 'listMessages' ? 'listMessageWindow' : name, args: args.map(value => typeof value === 'object' && value?.onToken ? '[handlers]' : structuredClone(value)) })
      if (name === 'sendMessage') streams.push({ id: args[0], raw: args[1], handlers: args[2], requestId: args[3] })
      if (name === 'listMessages') return Promise.resolve(fn(...args)).then(items => ({ conversation_id: args[0],
        novel_id: mounting.props?.conversation?.id === args[0] ? mounting.props.conversation.novel_id : null,
        items, has_more: false, next_before_message_id: null }))
      return fn(...args)
    }])),
    NButton: { name: 'NButton' }, NInput: { name: 'NInput' }, NEmpty: { name: 'NEmpty' }, NModal: { name: 'NModal' },
    useMessage: () => { const notices = mounting.notices; return Object.fromEntries(['success', 'error', 'warning'].map(name => [name, text => notices.push({ type: name, text })])) },
    onBeforeUnmount: fn => { mounting.unmount = fn },
  }
  globalThis.__submission = harness
  const source = 'const __harness = globalThis.__submission\n' + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/from ['"]\.\.\/utils\/chatSubmission['"]/g, `from '${helperUrl}'`)
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1 = { name: "MessageBubble" }')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const definition = (await import(url(compile(source)))).default
  const template = compileTemplate({ source: descriptor.template.content, filename: 'ChatWindow.vue', id: 'chat-submission', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendering = await import(url(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
  async function mount(extra = {}) {
    const notices = [], events = [], props = reactive({ conversation: { id: 'conversation-a', novel_id: null, title: 'A' }, novels: [], ...extra })
    globalThis.window = { addEventListener: (name, fn) => { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn) }, removeEventListener: (name, fn) => listeners.get(name)?.delete(fn) }
    mounting = { notices, props }
    const scope = effectScope(), vm = scope.run(() => definition.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
    const unmount = mounting.unmount; await flush()
    return { vm, props, notices, events, render: () => rendering.render({}, [], props, proxyRefs(vm), {}, {}),
      dispose: () => { unmount(); scope.stop() }, listeners }
  }
  return { mount, calls, streams, helper: await import(helperUrl) }
}
function nodes(node, predicate, found = []) {
  if (!node || typeof node !== 'object') return found
  if (predicate(node)) found.push(node)
  if (Array.isArray(node.children)) node.children.forEach(child => nodes(child, predicate, found))
  else if (node.children && typeof node.children === 'object') for (const slot of Object.values(node.children)) if (typeof slot === 'function') {
    const children = slot(); (Array.isArray(children) ? children : [children]).forEach(child => nodes(child, predicate, found))
  }
  return found
}
const posts = runtime => runtime.calls.filter(row => row.name === 'sendMessage')
const unknown = async (_id, _raw, handlers) => handlers.onError('原发送实际写入被拒绝')

test('会话目录后到：恢复的会话最初被空目录挡住，资格到达后首次读取且等待中新输入不丢', async () => {
  const history = wait()
  const r = await runtime({ api: { listMessages: () => history.promise } })
  const c = await r.mount({ conversationIds: [] })
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 0)
  c.props.conversationIds = ['conversation-a']; await flush()
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 1)
  c.vm.input.value = '目录等待期间作者新构思'
  history.resolve([{ id: 'persisted-history', conversation_id: 'conversation-a', role: 'assistant', content: '已经保存的历史', meta: null, created_at: '' }])
  await flush()
  assert.equal(c.vm.messages.value.length, 1)
  assert.equal(nodes(c.render(), row => row.type?.name === 'MessageBubble').length, 1)
  assert.equal(c.vm.input.value, '目录等待期间作者新构思')
  c.props.conversationIds = ['conversation-a', 'b']; await flush()
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 1)
  assert.equal(posts(r).length, 0); c.dispose()
})

test('会话目录首次无过滤已读取，后来提供合法目录不重复读同一历史', async () => {
  const r = await runtime(), c = await r.mount()
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 1)
  c.vm.input.value = '已有未发送稿'
  c.props.conversationIds = ['conversation-a']; await flush()
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, 1)
  assert.equal(c.vm.input.value, '已有未发送稿'); assert.equal(posts(r).length, 0); c.dispose()
})

test('目录资格后到不能重载跨挂载同会话的在途发送或清除原稿和锁', async () => {
  const response = wait()
  const r = await runtime({ api: { sendMessage: async (_id, _raw, handlers) => {
    await response.promise; await handlers.onError('旧实例结果仍待核对')
  } } })
  const first = await r.mount(); first.vm.input.value = '原在途构思'
  const sending = first.vm.send(); await until(() => posts(r).length === 1)
  const original = first.vm.submission.value; first.dispose()
  const next = await r.mount({ conversationIds: [] })
  const reads = r.calls.filter(row => row.name === 'listMessageWindow').length
  next.props.conversationIds = ['conversation-a']; await flush()
  assert.equal(r.calls.filter(row => row.name === 'listMessageWindow').length, reads)
  assert.equal(next.vm.streaming.value, true)
  assert.equal(next.vm.submission.value, original)
  assert.equal(nodes(next.render(), row => row.type?.name === 'NInput')[0].props.disabled, true)
  await next.vm.send(); assert.equal(posts(r).length, 1)
  response.resolve(); await sending
  assert.equal(next.vm.submission.value.raw, '原在途构思')
  assert.equal(next.vm.submission.value.status, 'unknown'); next.dispose()
})

test('未知原稿和新输入跨挂载保留，后到的合法历史不按文本冒充原UUID确认', async () => {
  let pending
  const r = await runtime({ api: { sendMessage: unknown, listMessages: () => pending ? pending.promise : [] } })
  const first = await r.mount(); first.vm.input.value = '未知原构思'; await first.vm.send()
  first.vm.input.value = '等待核对时另写的新稿'
  const original = first.vm.submission.value; first.dispose(); pending = wait()
  const next = await r.mount({ conversationIds: [] })
  next.props.conversationIds = ['conversation-a']; await flush()
  assert.equal(next.vm.input.value, '等待核对时另写的新稿')
  pending.resolve([{ id: 'another-saved-UUID', conversation_id: 'conversation-a', role: 'user', content: '未知原构思', meta: null, created_at: '' }])
  await flush()
  assert.equal(next.vm.messages.value.length, 1)
  assert.equal(next.vm.submission.value, original); assert.equal(original.status, 'unknown')
  assert.equal(next.vm.input.value, '等待核对时另写的新稿')
  assert.equal(next.vm.dirty.value, true); assert.equal(posts(r).length, 1); next.dispose()
})

test('后到资格的A→B→A读取仍拒绝旧A，明确删除清状态且旧读取不能复活', async () => {
  const reads = []
  const r = await runtime({ api: { listMessages: id => { const pending = wait(); reads.push({ id, ...pending }); return pending.promise } } })
  const c = await r.mount({ conversationIds: [] })
  c.props.conversationIds = ['conversation-a']; await flush()
  const originalConversation = c.props.conversation
  c.props.conversation = { id: 'b', novel_id: null, title: 'B' }; await flush()
  assert.equal(reads.length, 1)
  c.props.conversationIds = ['conversation-a', 'b']; await flush()
  c.props.conversation = originalConversation; await flush(); assert.equal(reads.length, 3)
  const row = (id, content) => ({ id: content, conversation_id: id, role: 'assistant', content, meta: null, created_at: '' })
  reads[0].resolve([row('conversation-a', '旧A')]); reads[1].resolve([row('b', 'B历史')]); await flush()
  assert.equal(c.vm.messages.value.length, 0)
  reads[2].resolve([row('conversation-a', '新A')]); await flush()
  assert.equal(c.vm.messages.value[0].content, '新A')
  const oldRead = c.vm.loadMessages(); await flush()
  c.props.conversationIds = []; await flush()
  assert.equal(r.helper.chatSubmissionStates.size, 0)
  reads[3].resolve([row('conversation-a', '删除后的旧读')]); await oldRead
  assert.equal(c.vm.messages.value.length, 0); assert.equal(r.helper.chatSubmissionStates.size, 0)
  c.dispose()
})

test('发送前独立冻结精确原稿与UUID，失败历史空不删唯一原文，可复制或明确恢复且刷新保护仍在', async () => {
  const r = await runtime({ api: { sendMessage: unknown } }), c = await r.mount(), raw = '\n　原构思😀\u0000，首尾必须保留。　\n', copied = []
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async value => copied.push(value) } } })
  c.vm.input.value = raw; const sending = c.vm.send()
  assert.equal(c.vm.submission.value.raw, raw); assert.equal(c.vm.input.value, '')
  await sending
  assert.equal(posts(r)[0].args[1], raw); assert.match(posts(r)[0].args[3], /^[0-9a-f-]{36}$/)
  assert.equal(c.vm.submission.value.status, 'unknown'); assert.equal(c.vm.messages.value.length, 0)
  assert.equal(c.vm.dirty.value, true)
  const textarea = nodes(c.render(), row => row.type === 'textarea' && row.props?.['aria-label'] === '原发送稿')[0]
  assert.equal(textarea.props.value, raw); assert.equal(textarea.props.readonly, '')
  await c.vm.copySubmission(); assert.deepEqual(copied, [raw])
  let prevented = false; [...c.listeners.get('beforeunload')][0]({ preventDefault() { prevented = true } }); assert.equal(prevented, true)
  c.vm.restoreSubmission(); assert.equal(c.vm.input.value, raw); assert.equal(posts(r).length, 1); c.dispose()
})

test('空白稿也受dirty/beforeunload保护，实际同模块双setup恢复页内原输入且无自动网络发送', async () => {
  const r = await runtime(), first = await r.mount()
  first.vm.input.value = ' \n　\t'; assert.equal(first.vm.dirty.value, true); await first.vm.send(); assert.equal(posts(r).length, 0)
  first.dispose(); const next = await r.mount()
  assert.equal(next.vm.input.value, ' \n　\t'); assert.equal(next.vm.dirty.value, true); assert.equal(posts(r).length, 0)
  next.dispose()
})

test('已验证user_saved仅证用户保存，模型失败原稿和片段仍保留，不假称回复完整', async () => {
  const r = await runtime({ api: { sendMessage: async (id, raw, h, uuid) => {
    await h.onUserSaved({ ...receipt(id, raw, uuid), type: 'user_saved', receipt_replayed: false }); h.onToken('半条回复'); await h.onError('模型中断')
  } } }), c = await r.mount()
  c.vm.input.value = '已经保存的原构思'; await c.vm.send()
  assert.equal(c.vm.submission.value.status, 'saved'); assert.equal(c.vm.streamingText.value, '半条回复')
  assert.equal(c.vm.submission.value.raw, '已经保存的原构思'); assert.match(c.vm.replyNotice.value, /未完整结束/)
  assert.equal(c.events.some(([name]) => name === 'changed'), false); c.dispose()
})

test('缺旧done或伪造user_saved/receipt身份和hash不会解除原稿核对，也不开放安全重试', async () => {
  for (const patch of [{ content_hash: '' }, { request_id: randomUUID() }, { conversation_id: 'other' }, { user_message_id: randomUUID() }, { content: '改稿' }, { accepted: false }, { receipt_replayed: undefined }]) {
    const r = await runtime({ api: { sendMessage: async (id, raw, h, uuid) => {
      await h.onUserSaved({ ...receipt(id, raw, uuid), type: 'user_saved', receipt_replayed: false, ...patch })
      h.onToken('应保留的候选回复'); await h.onDone({ type: 'done' })
    } } }), c = await r.mount()
    c.vm.input.value = '原始稿'; await c.vm.send()
    assert.equal(c.vm.submission.value.status, 'unknown'); assert.equal(c.vm.submission.value.raw, '原始稿')
    assert.equal(c.vm.streamingText.value, '应保留的候选回复'); assert.equal(c.vm.dirty.value, true)
    await c.vm.retrySubmission(); assert.equal(posts(r).length, 1); c.dispose()
  }
})

test('未知仅显式GET核对原UUID，保存证明不清后来的新稿，作者明确新轮使用新UUID同文合法', async () => {
  let accepted
  const r = await runtime({ api: { sendMessage: async (id, raw, h, uuid) => { accepted = receipt(id, raw, uuid); await h.onError('回应丢失') }, getChatSubmission: async () => accepted } }), c = await r.mount()
  c.vm.input.value = '原稿'; await c.vm.send(); const firstId = c.vm.submission.value.requestId
  c.vm.input.value = '后来的新稿'; assert.equal(r.calls.some(row => row.name === 'getChatSubmission'), false)
  await c.vm.checkSubmission(); assert.equal(c.vm.input.value, '后来的新稿'); assert.equal(c.vm.submission.value.status, 'saved')
  assert.deepEqual(r.calls.find(row => row.name === 'getChatSubmission').args, ['conversation-a', firstId])
  c.vm.input.value = '原稿'; await c.vm.send(); assert.equal(posts(r).length, 2); assert.notEqual(posts(r)[1].args[3], firstId)
  c.dispose()
})

test('只有精确原请求404提供显式同UUID/同原文重试，重放receipt不重启回复且不清新稿', async () => {
  let writes = 0
  const r = await runtime({ api: { sendMessage: async (id, raw, h, uuid) => {
    if (++writes === 1) await h.onError('首次回应未知')
    else await h.onReceipt({ ...receipt(id, raw, uuid), type: 'receipt', reply_restarted: false })
  } } }), c = await r.mount()
  c.vm.input.value = '\n 原始精确稿 '; await c.vm.send(); const first = posts(r)[0]
  c.vm.input.value = '未提交的新稿'; await c.vm.retrySubmission(); assert.equal(writes, 1)
  await c.vm.checkSubmission(); assert.equal(c.vm.submission.value.status, 'not_found'); assert.equal(writes, 1)
  await c.vm.retrySubmission(); assert.deepEqual(posts(r)[1].args, first.args)
  assert.equal(c.vm.input.value, '未提交的新稿'); assert.equal(c.vm.submission.value.status, 'saved')
  assert.match(c.vm.replyNotice.value, /没有重新生成/); c.dispose()
})

test('generic404、会话删除404、409及伪回执都不能误当未保存后盲重发', async () => {
  for (const error of [Object.assign(new Error('generic'), { status: 404 }), Object.assign(new Error('原会话删了'), { status: 404, code: 'chat_conversation_not_found' }), Object.assign(new Error('冲突'), { status: 409, code: 'chat_message_request_conflict' }), null]) {
    const r = await runtime({ api: { sendMessage: unknown, getChatSubmission: async () => { if (error) throw error; return [] } } }), c = await r.mount()
    c.vm.input.value = '原发送'; await c.vm.send(); c.vm.input.value = '当前新稿'; await c.vm.checkSubmission(); await c.vm.retrySubmission()
    assert.notEqual(c.vm.submission.value.status, 'not_found'); assert.notEqual(c.vm.submission.value.status, 'saved')
    assert.equal(posts(r).length, 1); assert.equal(c.vm.input.value, '当前新稿'); assert.equal(c.vm.submission.value.raw, '原发送'); c.dispose()
  }
})

test('恢复原稿必须明确确认，确认期间新编辑/改回/会话ABA/卸载不覆盖作者输入', async () => {
  for (const change of ['edit', 'back', 'thread', 'unmount']) {
    const r = await runtime({ api: { sendMessage: unknown } }), c = await r.mount()
    c.vm.input.value = '原稿'; await c.vm.send(); c.vm.input.value = '后来输入'; c.vm.restoreSubmission()
    assert.equal(c.vm.showRestore.value, true)
    if (change === 'edit' || change === 'back') { c.vm.input.value = '确认中新写'; if (change === 'back') c.vm.input.value = '后来输入' }
    if (change === 'thread') { const original = c.props.conversation; c.props.conversation = { id: 'b', novel_id: null }; c.props.conversation = original }
    if (change === 'unmount') c.dispose()
    c.vm.confirmRestoreSubmission(); assert.equal(c.vm.input.value, change === 'edit' ? '确认中新写' : '后来输入')
    assert.equal(c.vm.submission.value.raw, '原稿'); if (change !== 'unmount') c.dispose()
  }
})

test('同编译模块跨卸载pending锁保留，新实例不能第二POST；旧未知完成不清新稿且只读核对可解除', async () => {
  const response = wait(); let saved
  const r = await runtime({ api: { sendMessage: async (id, raw, h, uuid) => { saved = receipt(id, raw, uuid); await response.promise; await h.onError('结果未知') }, getChatSubmission: async () => saved } })
  const first = await r.mount(); first.vm.input.value = '原挂起发送'; const sending = first.vm.send(); await until(() => posts(r).length === 1)
  first.dispose(); const next = await r.mount(); assert.equal(next.vm.streaming.value, true)
  assert.equal(nodes(next.render(), row => row.type?.name === 'NInput')[0].props.disabled, true)
  await next.vm.send(); assert.equal(posts(r).length, 1); response.resolve(); await sending
  next.vm.input.value = '回应未知后新写'; assert.equal(next.vm.submission.value.status, 'unknown')
  await next.vm.checkSubmission(); assert.equal(next.vm.input.value, '回应未知后新写'); assert.equal(next.vm.submission.value.status, 'saved')
  assert.equal(first.events.filter(([name]) => name === 'changed').length, 0); next.dispose()
})

test('旧done历史读取挂起时合法新发送，新UUID/tokens/loading不被旧尾回调或finally清除', async () => {
  const history = wait(), second = wait(); let reading = 0, writes = 0
  const r = await runtime({ api: { listMessages: async () => ++reading === 2 ? history.promise : [], sendMessage: async (id, raw, h, uuid) => {
    await h.onUserSaved({ ...receipt(id, raw, uuid), type: 'user_saved', receipt_replayed: false })
    if (++writes === 1) { h.onToken('第一轮'); await h.onDone(done(id, uuid)) }
    else { h.onToken('新一轮正在输出'); await second.promise; await h.onDone(done(id, uuid)) }
  } } }), c = await r.mount()
  c.vm.input.value = '第一轮'; const firstSend = c.vm.send(); await until(() => reading === 2)
  assert.equal(c.vm.streaming.value, false); c.vm.input.value = '新一轮'; const newSend = c.vm.send(); await until(() => writes === 2)
  const latest = c.vm.submission.value.requestId; history.resolve([]); await firstSend
  assert.equal(c.vm.submission.value.requestId, latest); assert.equal(c.vm.streamingText.value, '新一轮正在输出')
  assert.equal(c.vm.streaming.value, true); assert.equal(c.vm.replyNotice.value, '')
  second.resolve(); await newSend; c.dispose()
})

test('会话A→B→A与binding变化保留原稿，旧错误不提示新来源且其他会话仍可独立发新轮', async () => {
  const response = wait()
  const r = await runtime({ api: { sendMessage: async (_id, _raw, h) => { await response.promise; await h.onError('旧来源失败') } } }), c = await r.mount()
  c.vm.input.value = 'A原稿'; const sending = c.vm.send(); await until(() => posts(r).length === 1)
  const original = c.props.conversation; c.props.conversation = { id: 'b', novel_id: 'novel-b' }; c.vm.input.value = 'B新稿'
  c.props.conversation = { ...original, novel_id: 'new-binding' }; await flush()
  assert.equal(c.vm.submission.value.raw, 'A原稿'); assert.equal(c.vm.streaming.value, true)
  response.resolve(); await sending; assert.equal(c.notices.length, 0)
  c.props.conversation = { id: 'b', novel_id: 'novel-b' }; assert.equal(c.vm.input.value, 'B新稿')
  assert.equal(c.vm.streaming.value, false); assert.equal(c.vm.submission.value, null); c.dispose()
})

test('明确删除会话清页内该原稿但不清其他稿，旧回调不能复活删除状态', async () => {
  const response = wait(), r = await runtime({ api: { sendMessage: async (_id, _raw, h) => { await response.promise; await h.onError('旧响应') } } })
  const c = await r.mount({ conversationIds: ['conversation-a', 'b'] })
  c.vm.input.value = 'A原稿'; const sending = c.vm.send(); await until(() => posts(r).length === 1)
  c.props.conversation = { id: 'b', novel_id: null }; c.vm.input.value = 'B必须保留'
  c.props.conversationIds = ['b']; response.resolve(); await sending
  assert.equal(c.vm.input.value, 'B必须保留'); assert.equal([...r.helper.chatSubmissionStates.keys()].some(key => JSON.parse(key)[0] === 'conversation-a'), false)
  assert.equal(c.vm.dirty.value, true); assert.equal(c.notices.length, 0)
  c.props.conversationIds = []; await c.vm.loadMessages()
  assert.equal(c.vm.input.value, ''); assert.equal(c.vm.dirty.value, false)
  assert.equal(r.helper.chatSubmissionStates.size, 0); c.dispose()
})

test('复制失败有真实只读原稿可手选，移除只在明确确认且确认未再编辑时删除本地意图', async () => {
  const r = await runtime({ api: { sendMessage: unknown } }), c = await r.mount()
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async () => { throw new Error('剪贴板禁止') } } } })
  c.vm.input.value = '唯一原稿'; await c.vm.send(); await c.vm.copySubmission()
  assert.match(c.notices.at(-1).text, /只读原发送稿.*手动/)
  c.vm.removeSubmission(); assert.equal(c.vm.showRestore.value, true); assert.equal(c.vm.submission.value.raw, '唯一原稿')
  c.vm.input.value = '确认期间新的草稿'; c.vm.confirmRestoreSubmission(); assert.equal(c.vm.submission.value.raw, '唯一原稿')
  c.vm.removeSubmission(); c.vm.confirmRestoreSubmission(); assert.equal(c.vm.submission.value, null)
  assert.equal(c.vm.input.value, '确认期间新的草稿'); assert.equal(posts(r).length, 1); c.dispose()
})

test('真实api.ts处理user_saved非terminal、receipt独立terminal，写作EOF/done合同不变', async () => {
  const { api, streamSSE } = await import(url(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')))), originalFetch = globalThis.fetch
  const uuid = randomUUID(), ack = receipt('c', ' 原稿 ', uuid), calls = [], events = []
  let frames = [{ ...ack, type: 'user_saved', receipt_replayed: false }, { type: 'token', content: '完整回复' }, done('c', uuid)]
  globalThis.fetch = async (path, init = {}) => { calls.push({ path, init }); return new Response(init.method === 'POST' ? frames.map(row => 'data: ' + JSON.stringify(row) + '\n\n').join('') : JSON.stringify(ack)) }
  const handlers = { onUserSaved: row => events.push(row.type), onReceipt: row => events.push(row.type), onToken: value => events.push(value), onDone: row => events.push(row.type), onError: (message, code) => events.push(code ?? message) }
  try {
    await api.sendMessage('c', ' 原稿 ', handlers, uuid)
    assert.deepEqual(JSON.parse(calls[0].init.body), { content: ' 原稿 ', request_id: uuid }); assert.deepEqual(events, ['user_saved', '完整回复', 'done'])
    events.length = 0; frames = [{ ...ack, type: 'receipt', reply_restarted: false }]; await api.sendMessage('c', ' 原稿 ', handlers, uuid)
    assert.deepEqual(events, ['receipt']); await api.getChatSubmission('c', uuid); assert.equal(calls.at(-1).path, `/api/v1/conversations/c/messages/requests/${uuid}`)
    events.length = 0; frames = [{ type: 'token', content: '写作半稿' }]; await streamSSE('/write', {}, handlers); assert.deepEqual(events, ['写作半稿', 'STREAM_INCOMPLETE'])
    events.length = 0; frames = [{ type: 'done' }]; await streamSSE('/write', {}, handlers); assert.deepEqual(events, ['done'])
  } finally { globalThis.fetch = originalFetch }
})
