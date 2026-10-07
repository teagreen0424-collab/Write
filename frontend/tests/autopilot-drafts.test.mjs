import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import { nextTick, reactive } from 'vue'
import ts from 'typescript'

const transpile = (source) => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const writerUrl = dataUrl(transpile(await readFile(new URL('../src/autopilotDrafts.ts', import.meta.url), 'utf8')))
const wordLimitsUrl = dataUrl(transpile(await readFile(new URL('../src/utils/wordLimits.ts', import.meta.url), 'utf8')))
const injectionUrl = dataUrl(transpile(await readFile(new URL('../src/utils/autopilotInjection.ts', import.meta.url), 'utf8')))
const { AutopilotDraftWriter } = await import(writerUrl)
const componentSource = await readFile(new URL('../src/components/AutopilotPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(componentSource)
const componentScript = compileScript(descriptor, { id: 'autopilot-persistence-test' }).content
let instanceId = 0
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const settle = async () => { for (let index = 0; index < 16; index += 1) { await Promise.resolve(); await nextTick() } }
const snapshot = (novelId, patch = {}) => ({
  type: 'snapshot', task_id: `task-${novelId}`, novel_id: novelId, event_id: 0,
  status: 'running', running: true, paused: false, pause_requested: false,
  generated: 0, skipped: 0, failed: 0, done: 0, total: 4,
  current_node_id: '', current_seq: 0, message: '后台运行中', task_notice: '', terminal_reason: '',
  active_drafts: [], quality_warnings: [], drift_warnings: [], ...patch,
})
const draft = (content = '已收到的片段。', patch = {}) => ({
  node_id: 'node-a', title: '第一章', attempt: 1, content, reason: '', state: 'active', revision: 0, ...patch,
})
const savedRecord = (patch = {}) => ({
  id: '00000000-0000-4000-a000-000000000001', novel_id: 'novel', ...draft(),
  state: 'unconfirmed', revision: 2, created_at: '', updated_at: '', ...patch,
})

async function panel(overrides = {}) {
  const messages = [], puts = [], deletes = [], hooks = {}, listeners = new Map(), emissions = []
  const props = reactive({ novelId: `novel-${++instanceId}` })
  const records = overrides.records || []
  let streamCalls = 0
  const api = {
    getQualityCurve: async () => ({ chapters: [], drift_threshold: 0.6, anchor_set: false }),
    autopilotStatus: async () => ({ running: false }),
    startAutopilot: async (novelId) => snapshot(novelId),
    listAutopilotDrafts: async () => records,
    saveAutopilotDraft: async (novelId, id, data) => { puts.push({ novelId, id, ...data }); return savedRecord({ id, novel_id: novelId, ...data }) },
    deleteAutopilotDraft: async (novelId, id, revision) => { deletes.push({ novelId, id, revision }); return { id, deleted: true, revision } },
    ...overrides.api,
  }
  globalThis.window = { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  globalThis.HTMLTextAreaElement = class {}
  globalThis.__autoHarness = {
    api, streamEvents: async (...args) => {
      streamCalls += 1
      if (overrides.stream) await overrides.stream(...args)
      else await args[1]({ type: 'done', status: 'completed' })
    },
    echarts: { init: () => ({ setOption() {}, dispose() {} }) },
    NButton: {}, NInput: {}, NInputNumber: {}, NModal: {}, NProgress: {}, NTag: {},
    useMessage: () => ({ success: (text) => messages.push(text), error: (text) => messages.push(text) }),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  const source = componentScript
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \* as echarts from ['"]echarts(?:\/core)?['"]/g, 'const echarts = { use() {}, ...globalThis.__autoHarness.echarts }')
    .replace(/import \{([^}]+)\} from ['"]echarts\/(?:charts|components|renderers)['"]/g, 'const {$1} = {}')
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = globalThis.__autoHarness')
    .replace(/import \{ api, streamEvents \} from ['"]\.\.\/api['"]/g, 'const { api, streamEvents } = globalThis.__autoHarness')
    .replace(/from ['"]\.\.\/autopilotDrafts['"]/g, `from '${writerUrl}'`)
    .replace(/from ['"]\.\.\/utils\/wordLimits['"]/g, `from '${wordLimitsUrl}'`)
    .replace(/from ['"]\.\.\/utils\/autopilotInjection['"]/g, `from '${injectionUrl}'`)
    .replace(/\bonMounted\(/g, 'globalThis.__autoHarness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, 'globalThis.__autoHarness.onBeforeUnmount(')
  const component = (await import(dataUrl(transpile(source) + `\n// ${instanceId}`))).default
  const vm = component.setup(props, { expose() {}, emit: (...args) => emissions.push(args) })
  hooks.mount()
  await settle()
  return { vm, props, api, puts, deletes, messages, hooks, listeners, emissions, get streamCalls() { return streamCalls } }
}

test('串行队列合并等待中的正文，删除等迟到 PUT 完成且同 ID 不会复活', async () => {
  const first = deferred(), calls = []
  const writer = new AutopilotDraftWriter('novel-a', {
    save: async (novelId, id, body) => { calls.push(['save', novelId, id, body.content, body.revision]); await first.promise },
    remove: async (novelId, id, revision) => calls.push(['delete', novelId, id, revision]),
  }, 1000)
  writer.save('draft-a', draft('第一段'))
  writer.save('draft-a', draft('第一段第二段'))
  const deleted = writer.remove('draft-a', 0)
  writer.save('draft-a', draft('迟到片段'))
  assert.equal(calls.length, 1)
  first.resolve()
  assert.equal(await deleted, true)
  await writer.flush()
  assert.deepEqual(calls.map((call) => call[0]), ['save', 'delete'])
  assert.equal(calls[0][3], '第一段')
  assert.ok(calls[1][3] > calls[0][4])
})

test('写库失败不冒充成功，同版本正文可幂等重试且按作品分开', async () => {
  let fail = true
  const calls = [], states = []
  const writer = new AutopilotDraftWriter('novel-b', {
    save: async (novelId, id, body) => { calls.push({ novelId, id, ...body }); if (fail) throw new Error('数据库不可写') },
    remove: async () => {},
  }, 1000)
  writer.subscribe((_id, status) => states.push(status.state))
  writer.save('draft-b', draft())
  await writer.flush()
  assert.equal(states.at(-1), 'error')
  fail = false
  await writer.retry()
  assert.equal(states.at(-1), 'saved')
  assert.equal(calls[1].revision, calls[0].revision)
  assert.equal(calls[1].novelId, 'novel-b')
  assert.equal(calls[1].content, calls[0].content)
})

test('多个节点及正文尾部写入仍严格串行，flush 不等待节流时间', async () => {
  let inFlight = 0, maximum = 0
  const calls = []
  const writer = new AutopilotDraftWriter('novel-c', {
    save: async (_novel, id, body) => { maximum = Math.max(maximum, ++inFlight); await Promise.resolve(); calls.push({ id, ...body }); inFlight -= 1 },
    remove: async () => {},
  }, 60000)
  writer.save('a', draft('一'))
  writer.save('a', draft('一二'))
  writer.save('a', draft('一二三'))
  writer.save('b', draft('另一章', { node_id: 'node-b' }))
  await writer.flush()
  assert.equal(maximum, 1)
  assert.equal(calls.filter((call) => call.id === 'a').at(-1).content, '一二三')
  assert.equal(calls.filter((call) => call.id === 'a').length, 2)
})

test('真实 SFC 重开恢复 active 为未确认稿，不自动生成也不写正式章', async () => {
  const context = await panel({ records: [savedRecord()] })
  const { vm } = context
  assert.equal(vm.draftsLoaded.value, true)
  assert.equal(vm.failedDrafts.value[0].text, '已收到的片段。')
  assert.equal(vm.failedDrafts.value[0].unconfirmed, true)
  assert.equal(vm.failedDrafts.value[0].saveState, 'saved')
  assert.equal(vm.activeDraftList.value.length, 0)
  assert.equal(context.streamCalls, 0)
  assert.equal(context.puts.length, 0)
  assert.equal(vm.hasPendingOutput.value, false, '数据库已暂存的片段无需按将丢失的脏稿处理')
  context.hooks.unmount()
})

test('真实 SFC 服务端逐片暂存元数据不重复 PUT，成功只清本次 active', async () => {
  const context = await panel({ records: [savedRecord({ state: 'failed', id: 'old-failed' })] })
  const { vm } = context
  const metadata = { draft_id: 'server-draft', draft_attempt: 2, draft_revision: 1, draft_persisted: true }
  vm.handleEvent({ type: 'progress', phase: 'generating', node_id: 'node-a', title: '第一章', ...metadata })
  vm.handleEvent({ type: 'text', node_id: 'node-a', content: '新片段', ...metadata, draft_revision: 2 })
  assert.equal(vm.activeDraftList.value[0].text, '新片段')
  assert.equal(vm.activeDraftList.value[0].saveState, 'saved')
  vm.handleEvent({ type: 'chapter_done', node_id: 'node-a', seq: 1, ...metadata })
  assert.equal(vm.activeDraftList.value.length, 0)
  assert.equal(vm.failedDrafts.value.length, 1)
  assert.equal(context.puts.length, 0)
  assert.equal(context.deletes.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 无元数据断流片段自动暂存，归档未知状态并可跨重开恢复', async () => {
  const records = []
  const context = await panel({ records, api: {
    saveAutopilotDraft: async (novelId, id, body) => {
      const record = savedRecord({ id, novel_id: novelId, ...body })
      const index = records.findIndex((item) => item.id === id)
      if (index < 0) records.push(record)
      else records[index] = record
      return record
    },
  } })
  const { vm } = context
  vm.handleEvent({ type: 'progress', phase: 'generating', node_id: 'node-a', title: '第一章' })
  vm.handleEvent({ type: 'text', node_id: 'node-a', content: '中断前的半稿。' })
  vm.handleConnectionError('网络断开', 'STREAM_INCOMPLETE')
  await vm.draftWriter.flush()
  assert.equal(records[0].content, '中断前的半稿。')
  assert.equal(records[0].state, 'unconfirmed')
  assert.equal(vm.failedDrafts.value[0].saveState, 'saved')
  context.hooks.unmount()
  const reopened = await panel({ records })
  assert.equal(reopened.vm.failedDrafts.value[0].text, '中断前的半稿。')
  assert.equal(reopened.streamCalls, 0)
  reopened.hooks.unmount()
})

test('真实 SFC 自动暂存失败显示可复制提示，较旧库正文不能覆盖未存新片段', async () => {
  let records = []
  const context = await panel({ api: {
    listAutopilotDrafts: async () => records,
    saveAutopilotDraft: async () => { throw new Error('磁盘写入失败') },
  } })
  const { vm } = context
  vm.handleEvent({ type: 'text', node_id: 'node-a', content: '未送达的新片段。' })
  vm.handleConnectionError('断流', 'STREAM_INCOMPLETE')
  await vm.draftWriter.flush()
  const incomplete = vm.failedDrafts.value[0]
  records = [savedRecord({ id: incomplete.draftKey, content: '较旧片段。' })]
  await vm.loadStoredDrafts()
  assert.equal(vm.failedDrafts.value[0].text, '未送达的新片段。')
  assert.equal(vm.failedDrafts.value[0].saveState, 'error')
  assert.match(vm.storageNotice.value, /磁盘写入失败.*复制/)
  assert.equal(vm.hasPendingOutput.value, true)
  context.hooks.unmount()
})

test('真实 SFC 恢复读取失败阻止起跑，重读成功才放行', async () => {
  let fail = true
  const context = await panel({ api: { listAutopilotDrafts: async () => { if (fail) throw new Error('无法读取'); return [] } } })
  assert.equal(context.vm.draftsLoaded.value, false)
  await context.vm.start()
  assert.equal(context.streamCalls, 0)
  assert.match(context.vm.recoveryNotice.value, /无法读取/)
  fail = false
  await context.vm.retryDraftStorage()
  assert.equal(context.vm.draftsLoaded.value, true)
  await context.vm.start()
  await settle()
  assert.equal(context.streamCalls, 1)
  context.hooks.unmount()
})

test('真实 SFC 删除失败继续保留草稿，成功只删确认的那一份', async () => {
  let fail = true
  const records = [savedRecord({ id: 'draft-one', state: 'failed' }), savedRecord({ id: 'draft-two', node_id: 'node-b' })]
  const context = await panel({ records, api: { deleteAutopilotDraft: async () => { if (fail) throw new Error('本章仍在生成，请等待'); return { deleted: true } } } })
  const { vm } = context
  vm.requestDiscard(vm.failedDrafts.value[0])
  await vm.confirmDiscard()
  assert.equal(vm.failedDrafts.value.length, 2)
  assert.equal(vm.showDiscard.value, true)
  assert.match(vm.storageNotice.value, /本章仍在生成/)
  fail = false
  await vm.confirmDiscard()
  assert.equal(vm.failedDrafts.value.length, 1)
  assert.equal(vm.failedDrafts.value[0].draftKey, 'draft-two')
  context.hooks.unmount()
})

test('真实 SFC 确认放弃先等自身迟到正文保存，再删除同一 UUID', async () => {
  const firstWrite = deferred(), calls = []
  const context = await panel({ api: {
    saveAutopilotDraft: async (_novelId, id, body) => {
      calls.push({ kind: 'save', id, content: body.content })
      if (calls.length === 1) await firstWrite.promise
    },
    deleteAutopilotDraft: async (_novelId, id) => { calls.push({ kind: 'delete', id }); return { deleted: true } },
  } })
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '第一段' })
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '第二段' })
  context.vm.handleConnectionError('断流')
  context.vm.requestDiscard(context.vm.failedDrafts.value[0])
  const confirming = context.vm.confirmDiscard()
  await settle()
  assert.equal(calls.some((call) => call.kind === 'delete'), false)
  assert.equal(context.vm.discardPending.value, true)
  firstWrite.resolve()
  await confirming
  assert.deepEqual(calls.map((call) => call.kind), ['save', 'save', 'delete'])
  assert.equal(calls[1].content, '第一段第二段')
  assert.equal(calls[2].id, calls[1].id)
  assert.equal(context.vm.failedDrafts.value.length, 0)
  context.hooks.unmount()
})

test('真实 SFC 跨作品迟到恢复不会污染新作品，旧写队列仍使用旧 novel ID', async () => {
  const lateRead = deferred(), oldPut = deferred(), puts = []
  let readCount = 0
  const context = await panel({ api: {
    listAutopilotDrafts: async () => ++readCount === 1 ? lateRead.promise : [],
    saveAutopilotDraft: async (novelId, id, body) => { puts.push({ novelId, id, ...body }); await oldPut.promise },
  } })
  const oldNovel = context.props.novelId
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '旧作品半稿。' })
  context.props.novelId = 'new-novel'
  await settle()
  lateRead.resolve([savedRecord({ novel_id: oldNovel })])
  oldPut.resolve()
  await settle()
  assert.equal(context.vm.failedDrafts.value.length, 0)
  assert.equal(context.vm.draftsLoaded.value, true)
  assert.ok(puts.every((call) => call.novelId === oldNovel))
  context.hooks.unmount()
})

test('真实 SFC 后台状态刷新更新暂存并监控已有任务，不创建第二个任务', async () => {
  let record = savedRecord({ state: 'failed', reason: '长度截断' })
  const context = await panel({ api: {
    listAutopilotDrafts: async () => [record],
    autopilotStatus: async (novelId) => snapshot(novelId),
  }, stream: async () => new Promise(() => {}) })
  await context.vm.start()
  assert.equal(context.streamCalls, 1, '首次恢复只订阅已有后台任务')
  record = { ...record, content: '后台继续后的最新片段。', revision: 3 }
  await context.vm.refreshTaskStatus()
  assert.equal(context.vm.failedDrafts.value[0].text, record.content)
  assert.equal(context.streamCalls, 1, '状态刷新不能重复订阅')
  context.hooks.unmount()
})

test('真实 SFC 重试成功仍保留同节点此前失败稿和原 UUID', async () => {
  const old = savedRecord({ state: 'failed', reason: '长度截断' })
  const context = await panel({ records: [old], stream: async (_url, onEvent) => {
    const meta = { draft_id: 'new-attempt', draft_attempt: 2, draft_revision: 1, draft_persisted: true }
    await onEvent({ type: 'progress', phase: 'generating', node_id: old.node_id, title: old.title, ...meta })
    await onEvent({ type: 'text', node_id: old.node_id, content: '本轮完整正文。', ...meta, draft_revision: 2 })
    await onEvent({ type: 'chapter_done', node_id: old.node_id, seq: 1, ...meta })
    await onEvent({ type: 'done', status: 'completed', done: 1, total: 1 })
  } })
  await context.vm.start()
  await settle()
  assert.equal(context.streamCalls, 1)
  assert.equal(context.vm.failedDrafts.value.length, 1)
  assert.equal(context.vm.failedDrafts.value[0].draftKey, old.id)
  assert.equal(context.vm.failedDrafts.value[0].text, old.content)
  assert.equal(context.vm.activeDraftList.value.length, 0)
  assert.equal(context.deletes.length, 0, '不得删除此前同节点的失败历史')
  context.hooks.unmount()
})

test('真实 SFC 最新任务已完成时历史未知稿恢复不能把头部改回未确认', async () => {
  const old = savedRecord({ state: 'unconfirmed', reason: '上次连接未收到保存确认' })
  const context = await panel({ records: [old], stream: async (_url, onEvent) => {
    await onEvent({ type: 'done', status: 'completed', done: 4, total: 4, message: '已有章节已全部跳过，任务完成' })
  } })
  assert.equal(context.vm.connectionUnconfirmed.value, true)
  await context.vm.start()
  await settle()
  await context.vm.loadStoredDrafts()
  assert.equal(context.vm.failedDrafts.value.length, 1)
  assert.equal(context.vm.failedDrafts.value[0].unconfirmed, true)
  assert.equal(context.vm.failedDrafts.value[0].text, old.content)
  assert.equal(context.vm.connectionUnconfirmed.value, false)
  assert.equal(context.vm.running.value, false)
  assert.equal(context.vm.done.value, 4)
  assert.equal(context.vm.total.value, 4)
  assert.match(context.vm.progressText.value, /任务完成/)
  context.hooks.unmount()
})

test('真实 SFC 新一轮传输错误重置终态确认，头部正确显示本轮未知', async () => {
  const context = await panel({ records: [savedRecord({ state: 'unconfirmed' })] })
  context.vm.handleEvent({ type: 'done', status: 'completed', done: 1, total: 1 })
  assert.equal(context.vm.latestTaskTerminal, true)
  context.vm.handleConnectionError('本轮网络断开', 'STREAM_INCOMPLETE')
  await context.vm.loadStoredDrafts()
  assert.equal(context.vm.latestTaskTerminal, false)
  assert.equal(context.vm.connectionUnconfirmed.value, true)
  assert.match(context.vm.progressText.value, /本轮网络断开/)
  context.hooks.unmount()
})

test('真实 SFC 正文已保存的收尾致命错误仍是任务终态，不归入质量提示', async () => {
  const context = await panel({ records: [savedRecord({ state: 'unconfirmed' })] })
  const notice = '正文均已保存，但恢复缓存或会话收尾失败，请核对暂存片段。'
  context.vm.running.value = true
  context.vm.handleEvent({ type: 'error', scope: 'task', fatal: true, saved: true, message: notice })
  assert.equal(context.vm.latestTaskTerminal, true)
  assert.equal(context.vm.running.value, false)
  assert.equal(context.vm.taskNotice.value, notice)
  assert.equal(context.vm.progressText.value, notice)
  assert.equal(context.vm.qualityWarnings.value.length, 0)
  assert.equal(context.vm.connectionUnconfirmed.value, false, '正文已保存不能冒称保存未知')
  await context.vm.loadStoredDrafts()
  assert.equal(context.vm.failedDrafts.value[0].unconfirmed, true, '历史缓存状态仍按卡片显示')
  assert.equal(context.vm.connectionUnconfirmed.value, false)
  context.hooks.unmount()
})

test('真实 SFC 迟到读取不能盖掉新一轮页面状态，最新恢复请求才生效', async () => {
  const stale = deferred(), latest = deferred()
  let reads = 0
  const context = await panel({ api: { listAutopilotDrafts: async () => {
    reads += 1
    return reads === 1 ? [] : reads === 2 ? stale.promise : latest.promise
  } } })
  const firstRead = context.vm.loadStoredDrafts()
  const secondRead = context.vm.loadStoredDrafts()
  await settle()
  latest.resolve([savedRecord({ content: '最新恢复内容。' })])
  await secondRead
  stale.resolve([savedRecord({ content: '迟到的旧内容。' })])
  await firstRead
  assert.equal(context.vm.failedDrafts.value[0].text, '最新恢复内容。')
  assert.equal(context.vm.checkingDrafts.value, false)
  context.hooks.unmount()
})

test('真实 SFC 后台独立运行可安全离开，未确认本地片段仍有 beforeunload 提示', async () => {
  const context = await panel({ records: [savedRecord()] })
  let prevented = 0
  const event = { preventDefault: () => { prevented += 1 }, returnValue: undefined }
  context.listeners.get('beforeunload')(event)
  assert.equal(prevented, 0)
  context.vm.serverRunning.value = true
  context.listeners.get('beforeunload')(event)
  assert.equal(prevented, 0)
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '本页尚未暂存的片段。' })
  context.listeners.get('beforeunload')(event)
  assert.equal(prevented, 1)
  assert.equal(event.returnValue, '')
  context.hooks.unmount()
})

test('真实 SFC 卸载清除监听并阻止迟到存储状态污染界面', async () => {
  const write = deferred()
  const context = await panel({ api: { saveAutopilotDraft: async () => write.promise } })
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '未完成内容。' })
  assert.equal(context.listeners.size, 1)
  context.hooks.unmount()
  assert.equal(context.listeners.size, 0)
  write.reject(new Error('迟到写入失败'))
  await context.vm.draftWriter.flush()
  assert.equal(context.vm.storageNotice.value, '')
})
