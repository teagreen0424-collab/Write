import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import test from 'node:test'
import ts from 'typescript'

const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
let source = (await readFile(new URL('./autopilot-author-input.test.mjs', import.meta.url), 'utf8')).split('\ntest(')[0]
for (const name of ['vue', '@vue/compiler-sfc', 'typescript', 'node:crypto']) source = source.replaceAll(`from '${name}'`, `from '${import.meta.resolve(name)}'`)
source = source.replaceAll('import.meta.url', JSON.stringify(new URL('./autopilot-author-input.test.mjs', import.meta.url).href))
  .replaceAll("import.meta.resolve('vue')", JSON.stringify(import.meta.resolve('vue')))
const { panel, receipt, snapshot, deferred, flush, storage, injectionRequests, nodes, textOf } = await import(url(source + '\nexport { panel, receipt, snapshot, deferred, flush, storage, injectionRequests, nodes, textOf }'))
const helper = await import(url(ts.transpileModule(await readFile(new URL('../src/utils/autopilotInjection.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText))
const unknown = () => { throw Object.assign(new Error('真实提交回应丢失'), { status: 503 }) }
const records = cache => [...cache.rows.values()].map(value => JSON.parse(value))

test('原提交UUID精确正文SHA在POST前持久保存，等待期间与未知后新稿持久更新，双击不重发', async () => {
  const cache = storage(), response = deferred(); let frozen
  const context = await panel({ storage: cache, api: { injectAutopilot: async (...args) => {
    frozen = args; const record = records(cache)[0]
    assert.equal(record.requestId, args[3]); assert.equal(record.text, args[2])
    assert.equal(record.hash, createHash('sha256').update(args[2]).digest('hex'))
    await response.promise; return unknown()
  } } })
  context.vm.directive.value = '  中文🙂\u0000原构思  '; const saving = context.vm.inject()
  context.vm.directive.value = '等待期间的新稿'; await flush(); await context.vm.inject()
  assert.equal(injectionRequests(context).length, 1); assert.equal(records(cache)[0].currentDraft, '等待期间的新稿')
  response.resolve(); await saving; context.vm.directive.value = '未知之后又修稿'
  assert.equal(records(cache)[0].currentDraft, '未知之后又修稿'); assert.equal(records(cache)[0].currentDraftEdited, true)
  assert.equal(records(cache)[0].text, frozen[2]); assert.match(frozen[3], /^[0-9a-f-]{36}$/)
  assert.equal(context.vm.injectionUnknown.value, true); context.dispose()
})

test('刷新仅展示缓存且按原task门禁，显式恢复需要确认，新编辑或taskABA使旧恢复确认失效', async () => {
  const cache = storage(), first = await panel({ storage: cache, api: { injectAutopilot: async () => unknown() } })
  first.vm.directive.value = '原提交'; await first.vm.inject(); first.vm.directive.value = '缓存的后续稿'; first.dispose()
  const fresh = await panel({ storage: cache })
  assert.equal(fresh.vm.directive.value, ''); assert.equal(injectionRequests(fresh).length, 0)
  assert.equal(fresh.requests.some(row => row.name === 'getAutopilotInjection'), false)
  assert.equal(fresh.vm.injectionUnknown.value, true)
  assert.ok(nodes(fresh.render(), row => row.props?.['aria-label'] === '待核对构思记录').length)
  fresh.vm.directive.value = '刷新后新写的稿'; const pending = fresh.vm.injectionRecoveries.value[0]
  fresh.vm.restoreInjection(pending); assert.equal(fresh.vm.showRestoreInjection.value, true)
  fresh.vm.directive.value = '打开确认后又编辑'; fresh.vm.confirmRestoreInjection()
  assert.equal(fresh.vm.directive.value, '打开确认后又编辑')
  assert.equal(pending.intent.currentDraft, '缓存的后续稿')
  fresh.vm.restoreInjection(pending)
  fresh.vm.applySnapshot(snapshot('novel-a', { task_id: 'another-task' })); fresh.vm.applySnapshot(snapshot('novel-a'))
  fresh.vm.confirmRestoreInjection(); assert.equal(fresh.vm.directive.value, '打开确认后又编辑')
  fresh.vm.restoreInjection(pending); fresh.vm.confirmRestoreInjection()
  assert.equal(fresh.vm.directive.value, '缓存的后续稿'); assert.equal(injectionRequests(fresh).length, 0)
  fresh.dispose()
})

test('未恢复唯一新稿直接GET确认仍保留cache和恢复列表，不覆盖typed稿，不重装旧控制状态或游标', async () => {
  const cache = storage(), response = deferred(); let originalAck
  const first = await panel({ storage: cache, api: { injectAutopilot: async (...args) => { originalAck = receipt(...args); await response.promise; return unknown() } } })
  first.vm.directive.value = '原构思A'; const writing = first.vm.inject(); first.vm.directive.value = '仅缓存保留的新稿'
  await flush(); response.resolve(); await writing; first.dispose()
  const fresh = await panel({ storage: cache, api: {
    autopilotStatus: async id => snapshot(id, { event_id: 99, paused: true, pause_requested: true }),
    getAutopilotInjection: async () => ({ ...originalAck, event_id: 1, paused: false, pause_requested: false, receipt_replayed: true }),
  } })
  assert.equal(fresh.vm.directive.value, ''); await fresh.vm.checkInjection(fresh.vm.injectionRecoveries.value[0])
  assert.equal(fresh.vm.injectionUnknown.value, false); assert.equal(fresh.vm.directive.value, '')
  assert.equal(records(cache)[0].currentDraft, '仅缓存保留的新稿')
  assert.equal(fresh.vm.injectionRecoveries.value[0].state, 'confirmed')
  assert.equal(fresh.vm.paused.value, true); assert.equal(fresh.vm.pauseRequested.value, true)
  assert.match(textOf(fresh.render()), /原构思已记录/)
  assert.equal(injectionRequests(fresh).length, 0)
  fresh.vm.directive.value = '核对后新typed稿'; const retained = fresh.vm.injectionRecoveries.value[0]
  fresh.vm.restoreInjection(retained); assert.equal(fresh.vm.directive.value, '核对后新typed稿')
  assert.equal(fresh.vm.showRestoreInjection.value, true); fresh.vm.confirmRestoreInjection()
  assert.equal(fresh.vm.directive.value, '仅缓存保留的新稿'); assert.equal(cache.rows.size, 0)
  assert.ok(fresh.messages.some(row => row.type === 'success' && /已记录/.test(row.text)))
  assert.equal(fresh.messages.some(row => /已注入|已经应用|已保存章节/.test(row.text)), false); fresh.dispose()
})

test('仅原nonce精确notfound GET404允许原payload显式重试，同稿新操作合法新nonce且不按文本去重', async () => {
  const context = await panel({ api: { injectAutopilot: async (...args) => injectionRequests(context).length === 1 ? unknown() : receipt(...args) } })
  context.vm.directive.value = '原冻结稿'; await context.vm.inject(); context.vm.directive.value = '作者的新稿'
  const original = injectionRequests(context)[0].args
  await context.vm.checkInjection(context.vm.directiveSubmission.value)
  assert.equal(injectionRequests(context).length, 1); assert.equal(context.vm.directiveSubmission.value.canRetry, true)
  assert.ok(nodes(context.render(), node => textOf(node) === '原请求重试').length)
  await context.vm.retryInjection(context.vm.directiveSubmission.value)
  assert.deepEqual(injectionRequests(context)[1].args, original); assert.equal(context.vm.directive.value, '作者的新稿')
  assert.equal(context.vm.injectionUnknown.value, false)
  context.vm.directive.value = '原冻结稿'; await context.vm.inject()
  assert.equal(injectionRequests(context)[2].args[2], original[2]); assert.notEqual(injectionRequests(context)[2].args[3], original[3])
  context.dispose()
  for (const code of ['task_not_found', 'novel_not_found', undefined]) {
    const blocked = await panel({ api: { injectAutopilot: async () => unknown(), getAutopilotInjection: async () => { throw Object.assign(new Error('不存在'), { status: 404, code }) } } })
    blocked.vm.directive.value = '原任务不能改绑'; await blocked.vm.inject(); await blocked.vm.checkInjection(blocked.vm.directiveSubmission.value)
    assert.equal(blocked.vm.directiveSubmission.value.canRetry, false); await blocked.vm.retryInjection(blocked.vm.directiveSubmission.value)
    assert.equal(injectionRequests(blocked).length, 1); blocked.dispose()
  }
})

test('核对200必须原nonce原文hash及accepted/replayed完整匹配，旧event整数合法而坏ACK不会解除门禁', async () => {
  const patches = [{ request_id: 'wrong' }, { directive: 'wrong' }, { directive_hash: '0'.repeat(64) }, { accepted: false },
    { receipt_replayed: undefined }, { receipt_replayed: false }, { novel_id: 'wrong' }, { task_id: 'wrong' },
    { event_id: 0 }, { event_id: 1.5 }, { paused: undefined }, { action: 'pause' }, { message: '' }]
  for (const patch of patches) {
    let original
    const context = await panel({ api: { injectAutopilot: async (...args) => { original = receipt(...args); return unknown() },
      getAutopilotInjection: async () => ({ ...original, receipt_replayed: true, ...patch }) } })
    context.vm.directive.value = '原文'; await context.vm.inject(); context.vm.directive.value = '新稿'
    await context.vm.checkInjection(context.vm.directiveSubmission.value)
    assert.equal(context.vm.injectionUnknown.value, true); assert.equal(context.vm.directive.value, '新稿')
    assert.equal(context.messages.some(row => row.type === 'success'), false); assert.equal(injectionRequests(context).length, 1)
    context.dispose()
  }
})

test('GET在途双击与同module重挂共锁，旧核对只记录原事实，不清新setup作者稿或解新task锁', async () => {
  const response = deferred(); let original
  const old = await panel({ api: { injectAutopilot: async (...args) => { original = receipt(...args); return unknown() }, getAutopilotInjection: async () => response.promise } })
  old.vm.directive.value = '旧提交'; await old.vm.inject(); const reading = old.vm.checkInjection(old.vm.directiveSubmission.value)
  await old.vm.checkInjection(old.vm.directiveSubmission.value); old.dispose(); const fresh = await old.mountAgain()
  fresh.vm.directive.value = '新setup作者稿'; await fresh.vm.inject()
  assert.equal(injectionRequests(fresh).length, 1); assert.equal(fresh.requests.filter(row => row.name === 'getAutopilotInjection').length, 1)
  response.resolve({ ...original, receipt_replayed: true }); await reading
  assert.equal(fresh.vm.directive.value, '新setup作者稿'); assert.equal(fresh.vm.injectionUnknown.value, false)
  assert.equal(fresh.messages.length, 0); assert.equal(old.messages.filter(row => row.type === 'success').length, 0); fresh.dispose()
})

test('旧task待核对cache只门禁原task；当前新task独立，确认一nonce不会移除同作与其他作品记录', async () => {
  const cache = storage(); let original
  const first = await panel({ storage: cache, api: { injectAutopilot: async (...args) => { original = receipt(...args); return unknown() } } })
  first.vm.directive.value = '原任务稿'; await first.vm.inject(); first.dispose()
  const record = records(cache)[0], other = { ...record, requestId: crypto.randomUUID(), taskId: 'other-task', novelId: 'novel-b' }
  helper.storeAutopilotInjection(other)
  const fresh = await panel({ storage: cache, api: { autopilotStatus: async id => snapshot(id, { task_id: 'current-new-task' }),
    getAutopilotInjection: async () => ({ ...original, receipt_replayed: true }) } })
  assert.equal(fresh.vm.injectionUnknown.value, false); fresh.vm.directive.value = '当前新任务稿'; await fresh.vm.inject()
  assert.equal(injectionRequests(fresh)[0].args[1], 'current-new-task')
  await fresh.vm.checkInjection(fresh.vm.injectionRecoveries.value[0])
  assert.ok(records(cache).some(row => row.novelId === 'novel-b')); assert.ok(records(cache).some(row => row.requestId === record.requestId))
  assert.equal(fresh.vm.currentTaskId.value, 'current-new-task'); fresh.dispose()
})

test('初次cache满和后续draft更新失败保留页内稿/原nonce，copy失败有readonly原文且不重复toast', async () => {
  for (const firstWriteFails of [true, false]) {
    const cache = storage(), response = deferred(); let writes = 0
    const set = cache.setItem; cache.setItem = (...args) => { if (firstWriteFails || ++writes > 1) throw new Error('quota'); return set(...args) }
    const context = await panel({ storage: cache, api: { injectAutopilot: async () => { await response.promise; return unknown() } } })
    context.vm.directive.value = '原提交'; const saving = context.vm.inject(); await flush()
    context.vm.directive.value = '必须复制的新稿'; response.resolve(); await saving
    assert.equal(context.vm.directive.value, '必须复制的新稿'); assert.match(context.vm.injectionStorageNotice.value, /未能保存最新.*复制/)
    const noticeCount = context.messages.length; context.vm.directive.value = '又更新的新稿'; assert.equal(context.messages.length, noticeCount)
    navigator.clipboard.writeText = async () => { throw new Error('denied') }
    await context.vm.copyDirectiveText(context.vm.directiveSubmission.value.intent.text)
    assert.match(context.messages.at(-1).text, /手动选中复制/)
    assert.ok(nodes(context.render(), node => node.type === 'textarea' && node.props?.readonly === '' && node.props.value === '原提交').length)
    assert.equal(injectionRequests(context).length, 1); context.dispose()
  }
})

test('损坏cache不会自动恢复或删除，合法shape但SHA篡改核对前拦截零GET零POST', async () => {
  const cache = storage(), prefix = 'lingmo:autopilot-injection:v1:novel-a:'
  cache.setItem(prefix + crypto.randomUUID(), '{broken')
  const context = await panel({ storage: cache })
  assert.match(context.vm.injectionStorageNotice.value, /损坏/); assert.equal(cache.rows.size, 1)
  assert.equal(context.vm.directive.value, ''); assert.equal(injectionRequests(context).length, 0); context.dispose()
  const first = await panel({ storage: cache, api: { injectAutopilot: async () => unknown() } })
  first.vm.directive.value = '原文'; await first.vm.inject(); first.dispose()
  const valid = records({ rows: new Map([...cache.rows].filter(([, value]) => value !== '{broken')) })[0]
  valid.hash = '0'.repeat(64); helper.storeAutopilotInjection(valid)
  const fresh = await panel({ storage: cache }); await fresh.vm.checkInjection(fresh.vm.directiveSubmission.value)
  assert.equal(fresh.requests.some(row => row.name === 'getAutopilotInjection'), false)
  assert.equal(injectionRequests(fresh).length, 0); assert.equal(fresh.vm.injectionUnknown.value, true)
  assert.match(fresh.vm.directiveSubmission.value.notice, /校验信息不一致/); fresh.dispose()
})

test('UTF8SHA和同源逐UUID缓存helper严格读取scope/edited/形状，异常storage不冒称持久保存', async () => {
  const text = '中文🙂\u0000\r\n exact ', hash = await helper.hashAutopilotDirective(text)
  assert.equal(hash, createHash('sha256').update(text).digest('hex'))
  const cache = storage(); globalThis.localStorage = cache
  const intent = { version: 1, novelId: '小说/a', taskId: 'task', requestId: crypto.randomUUID(), text, hash,
    currentDraft: text, currentDraftEdited: false, createdAt: 10 }
  assert.equal(helper.storeAutopilotInjection(intent), true); assert.equal(helper.listAutopilotInjections('小说/a').records.length, 1)
  assert.equal(helper.listAutopilotInjections('小说').records.length, 0)
  helper.storeAutopilotInjection({ ...intent, requestId: crypto.randomUUID(), currentDraft: 'changed', currentDraftEdited: false })
  assert.equal(helper.listAutopilotInjections('小说/a').invalid, 1)
  assert.equal(helper.removeAutopilotInjection(intent.novelId, intent.requestId), true); assert.equal(cache.rows.size, 1)
  globalThis.localStorage = { get length() { throw new Error('denied') }, setItem() { throw new Error('quota') }, removeItem() { throw new Error('denied') } }
  assert.equal(helper.listAutopilotInjections('小说/a').unavailable, true)
  assert.equal(helper.storeAutopilotInjection(intent), false); assert.equal(helper.removeAutopilotInjection(intent.novelId, intent.requestId), false)
})

test('原GET跨作品ABA和任务ABA迟到确认仅记原事实，当前新稿保留且旧错误不污染新作品', async () => {
  for (const boundary of ['novel', 'task', 'unmount']) {
    const response = deferred(); let original
    const context = await panel({ api: { injectAutopilot: async (...args) => { original = receipt(...args); return unknown() },
      getAutopilotInjection: async () => response.promise } })
    context.vm.directive.value = '原稿'; await context.vm.inject(); context.messages.length = 0
    const checking = context.vm.checkInjection(context.vm.directiveSubmission.value); await flush()
    if (boundary === 'novel') { context.props.novelId = 'novel-b'; await flush(); context.props.novelId = 'novel-a'; await flush() }
    if (boundary === 'task') { context.vm.applySnapshot(snapshot('novel-a', { task_id: 'middle' })); context.vm.applySnapshot(snapshot('novel-a')) }
    context.vm.directive.value = '迟到核对也必须保留的稿'
    if (boundary === 'unmount') context.dispose()
    response.resolve({ ...original, receipt_replayed: true }); await checking
    assert.equal(context.vm.directive.value, '迟到核对也必须保留的稿'); assert.equal(context.messages.length, 0)
    assert.ok(context.vm.injectionRecoveries.value.some(row => row.retainedDraft))
    context.dispose()
  }
})

test('实际api函数POST携UUID与exact原文，原GET只读路径独立且404code保持可判定', async () => {
  const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
  const { api } = await import(url(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
  const previous = globalThis.fetch, requests = [], id = crypto.randomUUID(), text = ' exact 中文\u0000 '
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options })
    return requests.length < 3 ? new Response(JSON.stringify(receipt('novel', 'task', text, id, { receipt_replayed: requests.length === 2 })))
      : new Response(JSON.stringify({ detail: '未查到原记录', code: 'autopilot_injection_not_found' }), { status: 404 })
  }
  try {
    await api.injectAutopilot('novel', 'task', text, id); await api.getAutopilotInjection('novel', 'task', id)
    assert.deepEqual(JSON.parse(requests[0].options.body), { directive: text, request_id: id })
    assert.equal(requests[0].options.method, 'POST'); assert.match(requests[0].path, /tasks\/task\/inject$/)
    assert.equal(requests[1].path, `/api/v1/novels/novel/autopilot/tasks/task/injections/${id}`)
    assert.equal(requests[1].options.body, undefined); assert.equal(requests[1].options.method, undefined)
    await assert.rejects(api.getAutopilotInjection('novel', 'task', id), error => error.status === 404 && error.code === 'autopilot_injection_not_found')
  } finally { globalThis.fetch = previous }
})
