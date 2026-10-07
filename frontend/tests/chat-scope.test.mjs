import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createHash, randomUUID } from 'node:crypto'
import { compileScript, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const typesUrl = moduleUrl(compile(await readFile(new URL('../src/types.ts', import.meta.url), 'utf8')))
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { for (let i = 0; i < 12; i += 1) { await new Promise(resolve => setTimeout(resolve, 1)); await nextTick() } }
const conversation = (id, novel = `novel-${id}`) => ({ id, novel_id: novel, title: id })
const chatMessage = (id, content = id) => ({ id: `message-${id}`, conversation_id: id, role: 'assistant', content, meta: null, created_at: '' })
let sequence = 0
const chatReceipt = stream => ({ type: 'user_saved', request_id: stream.requestId, user_message_id: stream.requestId,
  conversation_id: stream.id, content: stream.text, content_hash: createHash('sha256').update(stream.text).digest('hex'), accepted: true, receipt_replayed: false })
const chatDone = stream => ({ type: 'done', request_id: stream.requestId, conversation_id: stream.id,
  user_message_id: stream.requestId, assistant_message_id: randomUUID(), title: '实际回复保存夹具' })

async function component(name, props, api, extras = {}) {
  const hooks = {}, events = [], notices = [], listeners = new Map()
  globalThis.window = { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  const harness = {
    api: { ...api, ...(name === 'ChatWindow' ? {
      // Preserve existing array/deferred fixtures under an explicit new window contract.
      listMessageWindow: async id => ({ conversation_id: id,
        novel_id: props.conversation?.id === id ? props.conversation.novel_id : `novel-${id}`,
        items: await api.listMessages(id), has_more: false, next_before_message_id: null }),
      listMessageActionsBatch: async (id, ids) => ({ conversation_id: id,
        novel_id: props.conversation?.id === id ? props.conversation.novel_id : `novel-${id}`,
        actions_by_message_id: Object.fromEntries(ids.map(messageId => [messageId, []])) }),
    } : {}) }, ...extras,
    NButton: {}, NDropdown: {}, NEmpty: {}, NInput: {}, NForm: {}, NFormItem: {}, NModal: {}, NSelect: {},
    useMessage: () => Object.fromEntries(['success', 'error', 'warning'].map((type) => [type, (text) => notices.push({ type, text })])),
    useDialog: () => extras.dialog ?? { warning() {} },
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  globalThis.__chatScopeHarness = harness
  const chatHelperUrl = extras.chatHelperUrl ?? moduleUrl(compile((await readFile(new URL('../src/utils/chatSubmission.ts', import.meta.url), 'utf8'))
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// chat-registry-${sequence + 1}`)
  const projectHelperUrl = moduleUrl(compile((await readFile(new URL('../src/utils/projectDraft.ts', import.meta.url), 'utf8'))
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// project-registry-${sequence + 1}`)
  const managementHelperUrl = moduleUrl(compile((await readFile(new URL('../src/utils/conversationManagement.ts', import.meta.url), 'utf8'))
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// management-registry-${sequence + 1}`)
  const script = compileScript(parse(await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor, { id: name }).content
  const source = 'const __harness = globalThis.__chatScopeHarness\n' + script
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/import \* as echarts from ['"]echarts(?:\/core)?['"]/g, 'const echarts = { use() {}, ...__harness.echarts }')
    .replace(/import \{([^}]+)\} from ['"]echarts\/(?:charts|components|renderers)['"]/g, 'const {$1} = {}')
    .replace(/from ['"]\.\.\/types['"]/g, `from '${typesUrl}'`)
    .replace(/from ['"]\.\.\/utils\/chatSubmission['"]/g, `from '${chatHelperUrl}'`)
    .replace(/from ['"]\.\.\/utils\/projectDraft['"]/g, `from '${projectHelperUrl}'`)
    .replace(/from ['"]\.\.\/utils\/conversationManagement['"]/g, `from '${managementHelperUrl}'`)
    .replace(/import (\w+) from ['"]\.\/\w+\.vue['"]/g, 'const $1 = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const definition = (await import(moduleUrl(compile(source) + `\n//${++sequence}`))).default
  const scope = effectScope()
  const vm = scope.run(() => definition.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
  await settle()
  return { vm, props, hooks, notices, events, listeners, stop: () => { hooks.unmount?.(); scope.stop() } }
}

async function chat(options = {}) {
  const loads = [], streams = []
  const api = {
    listMessages: async (id) => {
      const pending = wait(); loads.push({ id, ...pending }); return pending.promise
    },
    sendMessage: async (id, text, handlers, requestId) => {
      const pending = wait(), stream = { id, text, handlers, requestId, ...pending }; streams.push(stream)
      await handlers.onUserSaved(chatReceipt(stream)); return pending.promise
    },
    ...options.api,
  }
  const context = await component('ChatWindow', reactive({ conversation: conversation('a'), novels: [] }), api)
  return { ...context, loads, streams }
}

test('真实聊天SFC的迟到A列表不进入B，A→B→A只接受最新请求', async () => {
  const c = await chat()
  c.props.conversation = conversation('b'); await settle()
  c.loads[0].resolve([chatMessage('a')]); await settle()
  assert.deepEqual(c.vm.messages.value, [])
  c.loads[1].resolve([chatMessage('b')]); await settle()
  assert.deepEqual(c.vm.messages.value.map((m) => m.content), ['b'])
  c.props.conversation = conversation('a'); await settle()
  const older = c.vm.loadMessages()
  c.loads[3].resolve([chatMessage('a', '最新')]); await older
  c.loads[2].resolve([chatMessage('a', '较旧')]); await settle()
  assert.equal(c.vm.messages.value[0].content, '最新')
  c.stop()
})

test('真实聊天SFC按会话保存未发送输入，发送A后B仍可编辑且不串流', async () => {
  const c = await chat()
  c.loads[0].resolve([chatMessage('a')]); await settle()
  c.vm.input.value = 'A的新构思'
  assert.equal(c.vm.dirty.value, true)
  const sending = c.vm.send(); await settle()
  assert.equal(c.streams[0].id, 'a')
  c.props.conversation = conversation('b'); await settle()
  c.loads[1].resolve([chatMessage('b')]); await settle()
  assert.equal(c.vm.streaming.value, false)
  c.vm.input.value = 'B草稿'
  c.streams[0].handlers.onToken('A回复片段')
  assert.equal(c.vm.streamingText.value, '')
  assert.equal(c.vm.messages.value[0].conversation_id, 'b')
  c.props.conversation = conversation('a'); await settle()
  assert.equal(c.vm.streaming.value, true)
  assert.equal(c.vm.streamingText.value, 'A回复片段')
  const done = c.streams[0].handlers.onDone(chatDone(c.streams[0]))
  await settle()
  c.loads[2].resolve([chatMessage('a', 'A完成回复')]); await done
  c.streams[0].resolve(); await sending
  c.props.conversation = conversation('b'); await settle()
  assert.equal(c.vm.input.value, 'B草稿')
  assert.equal(c.vm.dirty.value, true)
  assert.equal(c.events.some(([name, value]) => name === 'dirty-change' && value), true)
  c.stop()
})

test('真实聊天SFC发送使发送前的历史读取失效，不抹掉乐观消息', async () => {
  const c = await chat()
  c.vm.input.value = '新输入'
  const sending = c.vm.send(); await settle()
  c.loads[0].resolve([]); await settle()
  assert.equal(c.vm.messages.value[0].content, '新输入')
  c.streams[0].resolve(); await sending
  c.stop()
})

test('真实聊天SFC旧会话错误不提示在新会话，失败片段可回原会话复制', async () => {
  const c = await chat()
  c.loads[0].resolve([]); await settle()
  c.vm.input.value = 'A输入'
  const sending = c.vm.send(); await settle()
  c.streams[0].handlers.onToken('未保存片段')
  c.props.conversation = conversation('b'); await settle()
  const failing = c.streams[0].handlers.onError('A连接断了'); await settle()
  c.loads[2].resolve([]); await failing
  assert.equal(c.notices.length, 0)
  assert.equal(c.vm.streamingText.value, '')
  c.props.conversation = conversation('a'); await settle()
  assert.equal(c.vm.streamingText.value, '未保存片段')
  assert.equal(c.vm.streaming.value, false)
  c.streams[0].resolve(); await sending
  c.stop()
})

test('真实聊天SFC同会话移动作品后使用新作用域，卸载后的回应没有UI副作用', async () => {
  const c = await chat()
  c.vm.input.value = '原作品构思'
  const sending = c.vm.send(); await settle()
  c.props.conversation = conversation('a', 'novel-b'); await settle()
  c.streams[0].handlers.onToken('旧作品正文')
  assert.equal(c.vm.streamingText.value, '')
  assert.equal(c.vm.input.value, '')
  c.stop()
  const before = c.events.length
  c.loads[0].reject(new Error('迟到加载失败'))
  c.loads[1].resolve([chatMessage('a')]); await settle()
  await c.streams[0].handlers.onDone(chatDone(c.streams[0]))
  c.streams[0].resolve(); await sending
  assert.equal(c.events.length, before)
  assert.equal(c.notices.length, 0)
  assert.equal(c.listeners.has('beforeunload'), false)
})

test('真实ChatView仅在删除作品二次确认后写入，409不冒称成功且可重试', async () => {
  let confirmation, calls = 0, busy = true
  const novel = { id: 'a', title: '测试作品' }
  const c = await component('ChatView', reactive({}), {
    listNovels: async () => [novel], listConversations: async () => [],
    deleteNovel: async (id) => { calls += 1; assert.equal(id, 'a'); if (busy) throw new Error('自动驾驶任务仍在运行') },
  }, { dialog: { warning: (options) => { confirmation = options } } })
  c.vm.removeNovel(novel)
  assert.equal(calls, 0)
  assert.match(confirmation.content, /全部设定、大纲、章节、版本和作品会话/)
  assert.match(confirmation.content, /不可恢复/)
  assert.equal(await confirmation.onPositiveClick(), false)
  assert.equal(c.notices.some((item) => item.type === 'success'), false)
  busy = false
  await confirmation.onPositiveClick()
  assert.equal(calls, 2)
  assert.equal(c.notices.some((item) => item.text === '作品已删除'), true)
  c.stop()
})

test('真实聊天SFC完成后历史加载失败保留片段，等待期间卸载不能emit', async () => {
  for (const unmount of [false, true]) {
    const c = await chat()
    c.loads[0].resolve([]); await settle()
    c.vm.input.value = '输入'
    const sending = c.vm.send(); await settle()
    c.streams[0].handlers.onToken('完整回复')
    const done = c.streams[0].handlers.onDone(chatDone(c.streams[0])); await settle()
    if (unmount) c.stop()
    const eventCount = c.events.length
    c.loads[1].reject(new Error('读取失败')); await done
    assert.equal(c.vm.streamingText.value, '完整回复')
    if (!unmount) assert.match(c.vm.replyNotice.value, /已保存.*读取未完成/)
    assert.equal(c.events.filter(([name]) => name === 'changed').length, unmount ? 0 : 1)
    if (unmount) assert.equal(c.events.length, eventCount)
    c.streams[0].resolve(); await sending
    if (!unmount) c.stop()
  }
})

test('真实聊天SFC全局会话移动作品保留未发送输入，不留下不可见脏状态', async () => {
  const c = await chat()
  c.props.conversation = conversation('a', null); await settle()
  c.vm.input.value = '还没发送的完整构思'
  c.props.conversation = conversation('a', 'novel-new'); await settle()
  assert.equal(c.vm.input.value, '还没发送的完整构思')
  c.vm.input.value = ''
  assert.equal(c.vm.dirty.value, false)
  c.stop()
})

test('真实聊天SFC流式会话移动作品后仍禁止同会话并发发送，其他会话可发送', async () => {
  const c = await chat()
  c.loads[0].resolve([]); await settle()
  c.vm.input.value = '第一条'
  const first = c.vm.send(); await settle()
  c.props.conversation = conversation('a', 'novel-new'); await settle()
  assert.equal(c.vm.streaming.value, true)
  c.vm.input.value = '第二条'
  await c.vm.send()
  assert.equal(c.streams.length, 1)
  assert.equal(c.vm.input.value, '第二条')
  c.props.conversation = conversation('b'); await settle()
  assert.equal(c.vm.streaming.value, false)
  c.vm.input.value = 'B输入'
  const second = c.vm.send(); await settle()
  assert.deepEqual(c.streams.map((item) => item.id), ['a', 'b'])
  c.streams[0].resolve(); c.streams[1].resolve()
  await Promise.all([first, second]); c.stop()
})

test('真实聊天SFC明确删除会话后清其未发送输入，其他会话输入保留', async () => {
  const c = await chat()
  c.vm.input.value = 'A尚未发送'
  c.props.conversation = conversation('b'); await settle()
  c.props.conversationIds = ['b']; await settle()
  assert.equal(c.vm.dirty.value, false)
  c.vm.input.value = 'B尚未发送'
  c.props.conversationIds = ['b', 'c']; await settle()
  assert.equal(c.vm.input.value, 'B尚未发送')
  assert.equal(c.vm.dirty.value, true)
  c.stop()
})

test('真实ChatView离开前保护未发送输入，取消不导航，确认才导航', async () => {
  let confirmation
  const c = await component('ChatView', reactive({}), {}, { dialog: { warning: (options) => { confirmation = options } } })
  c.vm.chatDirty.value = true
  c.vm.enterWorkbench('a')
  assert.equal(c.events.length, 0)
  assert.match(confirmation.content, /未保存输入.*待核对原稿/)
  assert.equal(confirmation.positiveText, '保留并继续')
  confirmation.onPositiveClick()
  assert.deepEqual(c.events, [['enter-workbench', 'a']])
  c.stop()
})

test('真实ChatView已确认删除后刷新失败仍移除作品和会话，旧读取不能复活', async () => {
  let confirmation, reads = 0
  const oldLoad = wait()
  const c = await component('ChatView', reactive({}), {
    listNovels: async () => { reads += 1; if (reads === 1) return oldLoad.promise; throw new Error('刷新失败') },
    listConversations: async () => [conversation('a')], deleteNovel: async () => {},
  }, { dialog: { warning: (options) => { confirmation = options } } })
  c.vm.novels.value = [{ id: 'novel-a', title: 'A' }, { id: 'b', title: 'B' }]
  c.vm.conversations.value = [conversation('a'), conversation('b')]
  c.vm.selectedId.value = 'a'
  const stale = c.vm.loadAll()
  c.vm.removeNovel({ id: 'novel-a', title: 'A' })
  await confirmation.onPositiveClick()
  oldLoad.resolve([{ id: 'novel-a', title: 'A' }]); await stale
  assert.deepEqual(c.vm.novels.value.map((n) => n.id), ['b'])
  assert.deepEqual(c.vm.conversations.value.map((n) => n.id), ['b'])
  assert.equal(c.vm.selectedId.value, 'b')
  c.stop()
})

test('真实图谱SFC加载捕获固定作品，旧回应及卸载不渲染，初始空作品后来可初始化', async () => {
  const loads = [], renders = [], charts = []
  const c = await component('KnowledgeGraph', reactive({ novelId: '' }), {
    listEntities: (id, path) => { const pending = wait(); loads.push({ id, path, ...pending }); return pending.promise },
  }, { echarts: { init: () => {
    const chart = { clear() {}, on() {}, resize() {}, dispose() {}, setOption: (data) => renders.push(data) }
    charts.push(chart); return chart
  } } })
  c.props.novelId = 'a'; c.vm.chartRef.value = {}; await settle()
  assert.equal(loads.length, 6)
  c.props.novelId = 'b'; await settle()
  assert.equal(loads.length, 12)
  for (const item of loads.slice(0, 6)) { assert.equal(item.id, 'a'); item.resolve([]) }
  await settle(); assert.equal(renders.length, 0)
  for (const item of loads.slice(6)) item.resolve(item.path === 'characters' ? [{ id: 'b', name: 'B人物' }] : [])
  await settle(); assert.equal(renders.length, 1)
  assert.equal(charts.length, 1)
  const pending = c.vm.loadAndRender(); await settle(); c.stop()
  for (const item of loads.slice(12)) item.resolve([])
  await pending
  assert.equal(renders.length, 1)
})
