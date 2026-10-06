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
const writerUrl = moduleUrl(compile(await readFile(new URL('../src/autopilotDrafts.ts', import.meta.url), 'utf8')))
const script = compileScript(parse(await readFile(new URL('../src/components/AutopilotPanel.vue', import.meta.url), 'utf8')).descriptor, { id: 'reconnect-test' }).content
let sequence = 0
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settle = async () => { for (let index = 0; index < 20; index += 1) { await Promise.resolve(); await nextTick() } }
const active = (patch = {}) => ({ node_id: 'node-a', title: '第一章', content: '第一段', draft_id: 'active-draft', draft_revision: 2, draft_attempt: 1, draft_persisted: true, ...patch })
const snapshot = (novelId, patch = {}) => ({
  type: 'snapshot', task_id: 'task-a', novel_id: novelId, event_id: 3, status: 'running',
  running: true, paused: false, pause_requested: false, generated: 0, skipped: 0, failed: 0,
  done: 0, total: 4, current_node_id: 'node-a', current_seq: 1, message: '正在生成第一章',
  task_notice: '', terminal_reason: '', active_drafts: [active()], quality_warnings: [], drift_warnings: [], ...patch,
})
const record = (patch = {}) => ({ id: 'old-draft', novel_id: 'novel', node_id: 'node-a', title: '第一章',
  attempt: 1, content: '此前半稿', reason: '上次未完成', state: 'failed', revision: 2, created_at: '', updated_at: '', ...patch })
const event = (id, patch = {}) => ({ type: 'text', task_id: 'task-a', event_id: id, node_id: 'node-a',
  content: '第二段', draft_id: 'active-draft', draft_revision: 3, draft_attempt: 1, draft_persisted: true, ...patch })

function clock() {
  const scheduled = new Map(), delays = []
  let id = 0
  return {
    scheduled, delays,
    setTimeout(fn, delay) { const key = ++id; scheduled.set(key, fn); delays.push(delay); return key },
    clearTimeout(key) { scheduled.delete(key) },
    async tick() { const entry = scheduled.entries().next().value; assert.ok(entry, '应有计划中的重连'); scheduled.delete(entry[0]); entry[1](); await settle() },
  }
}

async function panel(options = {}) {
  const props = reactive({ novelId: options.novelId || `reconnect-novel-${++sequence}` })
  const streams = [], starts = [], puts = [], controls = [], messages = [], hooks = {}, listeners = new Map()
  const timer = clock()
  const api = {
    autopilotStatus: async (novelId) => options.status ? options.status(novelId) : snapshot(novelId),
    startAutopilot: async (novelId, requestId) => { starts.push({ novelId, requestId }); return snapshot(novelId) },
    getQualityCurve: async () => ({ chapters: [], drift_threshold: 0.6, anchor_set: false }),
    listAutopilotDrafts: async () => options.records || [],
    saveAutopilotDraft: async (...args) => puts.push(args),
    deleteAutopilotDraft: async () => ({ deleted: true }),
    pauseAutopilot: async (...args) => { controls.push(['pause', ...args]); return { status: 'paused' } },
    resumeAutopilot: async (...args) => { controls.push(['resume', ...args]); return { status: 'running' } },
    injectAutopilot: async (...args) => { controls.push(['inject', ...args]); return { status: 'injected' } },
    ...options.api,
  }
  globalThis.window = { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  globalThis.HTMLTextAreaElement = class {}
  globalThis.__reconnectHarness = {
    api, streamEvents: options.streamImpl || (async (url, onEvent, streamOptions) => {
      const closed = wait()
      streamOptions.signal.addEventListener('abort', () => closed.resolve(), { once: true })
      const connection = { url, onEvent, options: streamOptions, closed }
      streams.push(connection)
      if (options.onStream) await options.onStream(connection, streams.length)
      else await closed.promise
    }),
    echarts: {}, NButton: {}, NInput: {}, NModal: {}, NProgress: {}, NTag: {},
    useMessage: () => ({ success: (value) => messages.push(value), error: (value) => messages.push(value) }),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn }, timer,
  }
  const source = 'const __harness = globalThis.__reconnectHarness\n' + script
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \* as echarts from ['"]echarts['"]/g, 'const { echarts } = __harness')
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamEvents \} from ['"]\.\.\/api['"]/g, 'const { api, streamEvents } = __harness')
    .replace(/from ['"]\.\.\/autopilotDrafts['"]/g, `from '${writerUrl}'`)
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
    .replace(/\bsetTimeout\(/g, '__harness.timer.setTimeout(')
    .replace(/\bclearTimeout\(/g, '__harness.timer.clearTimeout(')
  const component = (await import(moduleUrl(compile(source) + `\n// ${++sequence}`))).default
  const vm = component.setup(props, { expose() {}, emit() {} })
  hooks.mount()
  await settle()
  return { vm, props, api, streams, starts, puts, controls, messages, hooks, listeners, timer }
}

test('真实 SFC 快照恢复全文并按事件游标与草稿 revision 去重，增量不会翻倍', async () => {
  const context = await panel({ records: [record(), record({ id: 'active-draft', state: 'unconfirmed', content: '第一段' })] })
  const stream = context.streams[0]
  assert.equal(context.starts.length, 0)
  assert.match(stream.url, /\/tasks\/task-a\/events$/)
  assert.equal(context.vm.failedDrafts.value.length, 1)
  await stream.onEvent(event(3))
  await stream.onEvent(event(4, { draft_revision: 2 }))
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段')
  await stream.onEvent(event(5))
  await stream.onEvent(event(5))
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段第二段')
  await stream.onEvent(snapshot(context.props.novelId, { event_id: 7, active_drafts: [active({ content: '完整恢复全文', draft_revision: 5 })] }))
  await stream.onEvent(event(8, { draft_revision: 5 }))
  await stream.onEvent(event(9, { content: '末尾', draft_revision: 6 }))
  assert.equal(context.vm.activeDraftList.value[0].text, '完整恢复全文末尾')
  assert.equal(context.puts.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 刷新与两个标签页只订阅既有任务，卸载不停止后台也不写半稿', async () => {
  const novelId = `shared-novel-${++sequence}`
  const first = await panel({ novelId }), second = await panel({ novelId })
  first.hooks.unmount()
  await settle()
  const reopened = await panel({ novelId })
  assert.equal(first.streams[0].options.signal.aborted, true)
  for (const context of [first, second, reopened]) {
    assert.equal(context.starts.length, 0)
    assert.equal(context.puts.length, 0)
  }
  assert.equal(second.vm.serverRunning.value, true)
  assert.equal(reopened.vm.activeDraftList.value[0].text, '第一段')
  assert.equal(reopened.vm.failedDrafts.value.length, 0)
  second.hooks.unmount(); reopened.hooks.unmount()
})

test('真实 SFC 两标签启动竞态的 409 接入既有 task，后台只创建一次', async () => {
  let latest = null, created = 0
  const api = {
    autopilotStatus: async () => latest || { running: false },
    startAutopilot: async (novelId) => {
      if (latest) throw Object.assign(new Error('已有任务'), { task_id: latest.task_id, status: 409 })
      created += 1; latest = snapshot(novelId); return latest
    },
  }
  const novelId = `racing-novel-${++sequence}`
  const first = await panel({ novelId, api }), second = await panel({ novelId, api })
  await first.vm.start(); await second.vm.start(); await settle()
  assert.equal(created, 1)
  assert.equal(first.vm.currentTaskId.value, second.vm.currentTaskId.value)
  assert.equal(first.streams.length, 1)
  assert.equal(second.streams.length, 1)
  first.hooks.unmount(); second.hooks.unmount()
})

test('真实 SFC POST 回应丢失先核对任务；不明确时显式重试沿用原 request_id', async () => {
  const requests = []
  const context = await panel({ status: async () => ({ running: false }), api: {
    startAutopilot: async (_novel, requestId) => { requests.push(requestId); throw new Error('创建响应丢失') },
  } })
  await context.vm.start()
  assert.equal(context.streams.length, 0)
  assert.match(context.vm.taskNotice.value, /启动结果未确认/)
  await context.vm.start()
  assert.equal(requests.length, 2)
  assert.equal(requests[0], requests[1])
  assert.equal(context.timer.scheduled.size, 0)
  context.hooks.unmount()
  let latest = null, created = 0
  const recovered = await panel({ api: {
    autopilotStatus: async () => latest || { running: false },
    startAutopilot: async (novelId) => { created += 1; latest = snapshot(novelId); throw new Error('服务端已创建，回应丢失') },
  } })
  await recovered.vm.start(); await settle()
  assert.equal(created, 1)
  assert.equal(recovered.vm.currentTaskId.value, 'task-a')
  assert.equal(recovered.streams.length, 1)
  recovered.hooks.unmount()
})

test('真实 SFC 手动核对找回创建结果后，下一轮启动不会复用已完成任务的 request_id', async () => {
  let latest = null, unavailable = false
  const requests = []
  const context = await panel({ api: {
    autopilotStatus: async () => { if (unavailable) throw new Error('状态暂不可用'); return latest || { running: false } },
    startAutopilot: async (novelId, requestId) => {
      requests.push(requestId)
      latest = snapshot(novelId, { task_id: `created-${requests.length}`, running: false, status: 'completed', active_drafts: [] })
      if (requests.length === 1) { unavailable = true; throw new Error('响应丢失') }
      return latest
    },
  } })
  await context.vm.start()
  assert.equal(context.vm.pendingStartRequestId, requests[0])
  unavailable = false
  await context.vm.refreshTaskStatus()
  assert.equal(context.vm.pendingStartRequestId, null)
  await context.vm.start()
  assert.equal(requests.length, 2)
  assert.notEqual(requests[0], requests[1])
  context.hooks.unmount()
})

test('真实 SFC 断流保留实时半稿与后台状态，四次有限退避只重连不 POST', async () => {
  const context = await panel({ onStream: async ({ options }) => options.onError('网络断开', 'STREAM_INCOMPLETE') })
  assert.equal(context.vm.serverRunning.value, true)
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段')
  assert.equal(context.vm.failedDrafts.value.length, 0)
  for (let index = 0; index < 4; index += 1) await context.timer.tick()
  assert.deepEqual(context.timer.delays, [500, 1000, 2000, 4000])
  assert.equal(context.streams.length, 5)
  assert.equal(context.starts.length, 0)
  assert.equal(context.timer.scheduled.size, 0)
  assert.equal(context.vm.connectionState.value, 'disconnected')
  assert.match(context.vm.statusNotice.value, /手动重新连接/)
  context.hooks.unmount()
})

test('真实 SFC 重连用 fresh snapshot 全文恢复，成功结束后清本次 active', async () => {
  const context = await panel({ onStream: async ({ onEvent, options, closed }, index) => {
    if (index === 1) { await options.onError('第一次断流'); return }
    await onEvent(snapshot(context.props.novelId, { event_id: 7, active_drafts: [active({ content: '恢复全文', draft_revision: 4 })] }))
    await closed.promise
  } })
  await context.timer.tick()
  const stream = context.streams[1]
  assert.equal(context.vm.activeDraftList.value[0].text, '恢复全文')
  await stream.onEvent(event(7, { content: '重复', draft_revision: 4 }))
  await stream.onEvent(event(8, { content: '继续', draft_revision: 5 }))
  assert.equal(context.vm.activeDraftList.value[0].text, '恢复全文继续')
  await stream.onEvent(event(9, { type: 'chapter_done', seq: 1 }))
  await stream.onEvent(event(10, { type: 'done', status: 'completed', done: 4, total: 4, message: '全部完成' }))
  stream.closed.resolve(); await settle()
  assert.equal(context.vm.activeDraftList.value.length, 0)
  assert.equal(context.vm.serverRunning.value, false)
  assert.equal(context.vm.done.value, 4)
  assert.equal(context.timer.scheduled.size, 0)
  context.hooks.unmount()
})

test('真实 SFC 终态 status 与后端重启 interrupted 恢复结果，不自动生成', async () => {
  for (const status of ['completed', 'interrupted']) {
    const context = await panel({ status: async (novelId) => snapshot(novelId, {
      status, running: false, done: status === 'completed' ? 4 : 1, active_drafts: [],
      message: status === 'completed' ? '所有章节已完成' : '服务重启后任务已中断',
    }), records: [record({ state: 'unconfirmed' })] })
    assert.equal(context.streams.length, 0)
    assert.equal(context.starts.length, 0)
    assert.equal(context.vm.taskStatus.value, status)
    assert.equal(context.vm.failedDrafts.value[0].unconfirmed, true)
    assert.equal(context.vm.connectionUnconfirmed.value, status === 'interrupted')
    assert.match(context.vm.progressText.value, status === 'completed' ? /已完成/ : /已中断/)
    context.hooks.unmount()
  }
})

test('真实 SFC 状态取样后任务结束：实际 streamEvents 消费终态 snapshot+done 不误报 EOF', async () => {
  const { streamEvents } = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
  const previousFetch = globalThis.fetch
  const novelId = `terminal-race-${sequence}`
  try {
    globalThis.fetch = async () => {
      const terminal = snapshot(novelId, { status: 'completed', running: false, event_id: 9, done: 4, active_drafts: [], message: '任务已完成' })
      return new Response(`data: ${JSON.stringify(terminal)}\n\ndata: ${JSON.stringify({ type: 'done', task_id: 'task-a', event_id: 9, status: 'completed' })}\n\n`)
    }
    const context = await panel({ novelId, streamImpl: streamEvents })
    assert.equal(context.vm.latestTaskTerminal, true)
    assert.equal(context.vm.serverRunning.value, false)
    assert.equal(context.vm.done.value, 4)
    assert.equal(context.vm.connectionUnconfirmed.value, false)
    assert.equal(context.timer.scheduled.size, 0)
    assert.equal(context.messages.length, 0)
    context.hooks.unmount()
  } finally { globalThis.fetch = previousFetch }
})

test('真实 SFC GET 草稿领先未发布事件时不会预追加全文，progress 接管同 UUID 历史卡片', async () => {
  const context = await panel({ status: async (novelId) => snapshot(novelId, { event_id: 0, active_drafts: [] }),
    records: [record({ id: 'active-draft', state: 'unconfirmed', content: '第一段未来段', revision: 5 })] })
  assert.equal(context.vm.failedDrafts.value.length, 1)
  await context.streams[0].onEvent(event(1, { type: 'progress', phase: 'generating', title: '第一章', draft_revision: 1 }))
  await context.streams[0].onEvent(event(2, { content: '第一段', draft_revision: 2 }))
  assert.equal(context.vm.failedDrafts.value.length, 0)
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段')
  await context.vm.loadStoredDrafts()
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段')
  assert.equal(context.vm.failedDrafts.value.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 同节点旧尝试 chapter_done 不清新 active，错误按 UUID 归档且重放不重复', async () => {
  const context = await panel({ records: [record()] })
  await context.streams[0].onEvent(event(4, { type: 'chapter_done', draft_id: 'old-draft', seq: 1 }))
  assert.equal(context.vm.activeDraftList.value[0].draftKey, 'active-draft')
  await context.streams[0].onEvent(event(5, { type: 'error', scope: 'chapter', saved: false, message: '当前章未完成' }))
  await context.streams[0].onEvent(event(5, { type: 'error', scope: 'chapter', saved: false }))
  await context.streams[0].onEvent(event(6, { type: 'error', scope: 'chapter', saved: false }))
  assert.equal(context.vm.failedDrafts.value.length, 2)
  assert.equal(new Set(context.vm.failedDrafts.value.map((draft) => draft.draftKey)).size, 2)
  assert.equal(context.vm.activeDraftList.value.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 确认删除后迟到草稿列表与错误不能复活该 UUID', async () => {
  const context = await panel({ records: [record()] })
  context.vm.requestDiscard(context.vm.failedDrafts.value[0])
  await context.vm.confirmDiscard()
  await context.vm.loadStoredDrafts()
  await context.streams[0].onEvent(event(4, { type: 'error', scope: 'chapter', saved: false, draft_id: 'old-draft' }))
  assert.equal(context.vm.failedDrafts.value.length, 0)
  assert.equal(context.vm.activeDraftList.value[0].draftKey, 'active-draft')
  context.hooks.unmount()
})

test('真实 SFC 监控断开仍可按 task ID 暂停、恢复和注入，作者新输入不被迟到响应清掉', async () => {
  const injected = wait(), controls = []
  const context = await panel({ api: { injectAutopilot: async (...args) => { controls.push(args); await injected.promise } } })
  context.vm.handleConnectionError('网络断开')
  await context.vm.pause()
  assert.equal(context.vm.pauseRequested.value, true)
  await context.vm.resume()
  assert.equal(context.vm.pauseRequested.value, false)
  context.vm.directive.value = '下一章的构思'
  const submitting = context.vm.inject()
  context.vm.directive.value = '作者正在写的新构思'
  injected.resolve(); await submitting
  assert.deepEqual(context.controls.map((args) => args.slice(0, 3)), [
    ['pause', context.props.novelId, 'task-a'], ['resume', context.props.novelId, 'task-a'],
  ])
  assert.deepEqual(controls[0], [context.props.novelId, 'task-a', '下一章的构思'])
  assert.equal(context.vm.directive.value, '作者正在写的新构思')
  assert.equal(context.starts.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 控制广播同步另一个标签页，HTTP 控制确认不跳过尚未到达的正文', async () => {
  const context = await panel({ api: { pauseAutopilot: async () => ({
    type: 'control', task_id: 'task-a', event_id: 5, action: 'pause', paused: false, pause_requested: true,
  }) } })
  await context.vm.pause()
  assert.equal(context.vm.eventCursor, 3, 'HTTP 确认不能抢先推进订阅游标')
  await context.streams[0].onEvent(event(4))
  await context.streams[0].onEvent(event(5, { type: 'control', action: 'pause', paused: false, pause_requested: true }))
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段第二段')
  assert.equal(context.vm.pauseRequested.value, true)
  await context.streams[0].onEvent(event(6, { type: 'paused' }))
  assert.equal(context.vm.paused.value, true)
  await context.streams[0].onEvent(event(7, { type: 'control', action: 'resume', paused: false, pause_requested: false }))
  assert.equal(context.vm.paused.value, false)
  assert.equal(context.vm.pauseRequested.value, false)
  context.hooks.unmount()
})

test('真实 SFC 跨作品旧流与控制回应不会污染新 task，新启动迟到回应也被忽略', async () => {
  const paused = wait()
  const context = await panel({ status: async (novelId) => snapshot(novelId, { task_id: novelId.endsWith('-new') ? 'task-b' : 'task-a' }),
    api: { pauseAutopilot: async () => paused.promise } })
  const oldStream = context.streams[0]
  const pauseRequest = context.vm.pause()
  context.props.novelId += '-new'; await settle()
  await oldStream.onEvent(event(99, { content: '旧作品迟到正文' }))
  paused.resolve(); await pauseRequest
  assert.equal(context.vm.currentTaskId.value, 'task-b')
  assert.equal(context.vm.activeDraftList.value[0].text, '第一段')
  assert.equal(context.vm.pauseRequested.value, false)
  assert.equal(oldStream.options.signal.aborted, true)
  context.hooks.unmount()
  const created = wait()
  const late = await panel({ status: async () => ({ running: false }), api: { startAutopilot: async () => created.promise } })
  const oldNovelId = late.props.novelId
  const starting = late.vm.start()
  late.props.novelId += '-new'; await settle()
  created.resolve(snapshot(oldNovelId)); await starting
  assert.equal(late.vm.currentTaskId.value, '')
  assert.equal(late.streams.length, 0)
  late.hooks.unmount()
})

test('真实 SFC 卸载清重连计时器和本页订阅，后台任务与权威 active 不被改写', async () => {
  const context = await panel({ onStream: async ({ options }) => options.onError('断流') })
  assert.equal(context.timer.scheduled.size, 1)
  context.hooks.unmount()
  await settle()
  assert.equal(context.timer.scheduled.size, 0)
  await context.streams[0].onEvent(event(50, { content: '卸载后的迟到片段' }))
  assert.equal(context.puts.length, 0)
  assert.equal(context.starts.length, 0)
  assert.equal(context.vm.failedDrafts.value.length, 0)
})
