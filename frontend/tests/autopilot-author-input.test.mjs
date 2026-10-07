import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../autopilotDrafts', '../src/autopilotDrafts.ts'], ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../types', '../src/types.ts'], ['../utils/asyncScope', '../src/utils/asyncScope.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const flush = async () => { for (let index = 0; index < 20; index++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const snapshot = (novelId, patch = {}) => ({ type: 'snapshot', novel_id: novelId, task_id: `${novelId}-task`, event_id: 1,
  status: 'running', running: true, paused: false, pause_requested: false, generated: 0, skipped: 0, failed: 0, done: 0, total: 3,
  current_node_id: '', current_seq: 0, message: '受控后台正在运行', task_notice: '', terminal_reason: '', active_drafts: [],
  quality_warnings: [], drift_warnings: [], word_limits: null, ...patch })
const control = (taskId = 'novel-a-task', patch = {}) => ({ type: 'control', action: 'inject', task_id: taskId,
  event_id: 2, paused: false, pause_requested: false, message: '已接收章前指令，将在下一章应用', ...patch })
let sequence = 0
async function panel(options = {}) {
  const requests = [], connections = [], copied = [], timer = { setTimeout: () => 1, clearTimeout() {} }
  let activeHooks, activeMessages
  const api = {
    autopilotStatus: async id => snapshot(id), listAutopilotDrafts: async () => [],
    getQualityCurve: async () => ({ chapters: [], drift_threshold: 0.6, anchor_set: false }),
    injectAutopilot: async (_id, taskId) => control(taskId),
    pauseAutopilot: async (_id, taskId) => control(taskId, { action: 'pause', pause_requested: true }),
    resumeAutopilot: async (_id, taskId) => control(taskId, { action: 'resume' }),
    ...options.api,
  }
  const key = `__autopilotAuthor${++sequence}`
  globalThis[key] = {
    api: Object.fromEntries(Object.entries(api).map(([name, fn]) => [name, (...args) => { requests.push({ name, args: structuredClone(args) }); return fn(...args) }])),
    streamEvents: async (url, onEvent, options) => { const closed = deferred(); connections.push({ url, onEvent, options, closed }); options.signal.addEventListener('abort', () => closed.resolve(), { once: true }); await closed.promise },
    echarts: { init: () => ({ setOption() {}, dispose() {} }) },
    useMessage: () => { const messages = activeMessages; return Object.fromEntries(['success', 'warning', 'error', 'info'].map(type => [type, text => messages.push({ type, text })])) },
    onMounted: fn => { activeHooks.mount = fn }, onBeforeUnmount: fn => { activeHooks.unmount = fn }, timer,
  }
  for (const name of ['NButton', 'NInput', 'NInputNumber', 'NModal', 'NProgress', 'NTag']) globalThis[key][name] = name.toLowerCase()
  const descriptor = parse(await readFile(new URL('../src/components/AutopilotPanel.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `autopilot-author-${sequence}` })
  let source = `const __harness = globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \* as echarts from ['"]echarts(?:\/core)?['"]/g, 'const echarts = { use() {}, ...__harness.echarts }')
    .replace(/import \{([^}]+)\} from ['"]echarts\/(?:charts|components|renderers)['"]/g, 'const {$1} = {}')
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api, streamEvents \} from ['"]\.\.\/api['"]/g, 'const { api, streamEvents } = __harness')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
    .replace(/\bsetTimeout\(/g, '__harness.timer.setTimeout(').replace(/\bclearTimeout\(/g, '__harness.timer.clearTimeout(')
  for (const [name, url] of Object.entries(dependencies)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source)))).default
  const template = compileTemplate({ source: descriptor.template.content, id: `autopilot-author-${sequence}`, filename: 'AutopilotPanel.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
  async function mountAgain(overrides = {}) {
    const props = reactive({ novelId: 'novel-a', ...options.props, ...overrides }), messages = [], emitted = [], hooks = {}, listeners = new Map()
    activeHooks = hooks; activeMessages = messages
    const instanceWindow = { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
    globalThis.window = instanceWindow
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => copied.push(text) } } })
    globalThis.HTMLTextAreaElement = class {}
    const scope = effectScope(), vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
    await hooks.mount?.(); await flush()
    let disposed = false
    return { vm, props, messages, emitted, listeners, requests, connections, copied, api, mountAgain,
      render: () => rendered.render({}, [], props, proxyRefs(vm), {}, {}),
      dispose() { if (disposed) return; disposed = true; globalThis.window = instanceWindow; hooks.unmount?.(); scope.stop() },
    }
  }
  return mountAgain()
}
const injectionRequests = context => context.requests.filter(request => request.name === 'injectAutopilot')
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

test('同task注入在途禁双击和新构思提交，真实模板busy，200保留作者新输入供下一次明确提交', async (t) => {
  const response = deferred(); let calls = 0
  const context = await panel({ api: { injectAutopilot: async () => ++calls === 1 ? response.promise : control('novel-a-task', { event_id: 3 }) } })
  t.after(() => { response.resolve(control()); context.dispose() })
  context.vm.directive.value = '首次构思'; const first = context.vm.inject()
  context.vm.directive.value = '等待期间的新构思'; const duplicate = context.vm.inject()
  assert.equal(injectionRequests(context).length, 1)
  const button = nodes(context.render(), node => node.type === 'nbutton' && textOf(node) === '注入')[0]
  assert.equal(button.props.disabled, true); assert.equal(button.props.loading, true)
  response.resolve(control()); await Promise.all([first, duplicate])
  assert.equal(context.vm.directive.value, '等待期间的新构思')
  await context.vm.inject(); assert.equal(injectionRequests(context).length, 2)
  assert.equal(injectionRequests(context)[1].args[2], '等待期间的新构思'); assert.equal(context.vm.directive.value, '')
  context.dispose()
})

test('未提交构思含空白纳dirty和beforeunload，同module重挂与跨作品往返分别保留输入且从不自动注入', async () => {
  const context = await panel(); context.vm.directive.value = '  作者尚未提交的构思  '; await flush()
  let prevented = false; context.listeners.get('beforeunload')({ preventDefault() { prevented = true } })
  assert.equal(prevented, true); assert.equal(context.emitted.filter(([name]) => name === 'dirty-change').at(-1)[1], true)
  context.props.novelId = 'novel-b'; await flush(); assert.equal(context.vm.directive.value, '')
  context.vm.directive.value = 'B 的构思'; context.props.novelId = 'novel-a'; await flush(); assert.equal(context.vm.directive.value, '  作者尚未提交的构思  ')
  context.dispose(); const fresh = await context.mountAgain()
  assert.equal(fresh.vm.directive.value, '  作者尚未提交的构思  '); assert.equal(injectionRequests(fresh).length, 0)
  fresh.vm.directive.value = '   '; await flush()
  assert.equal(fresh.emitted.filter(([name]) => name === 'dirty-change').at(-1)[1], true)
  fresh.dispose()
})

test('注入等待期间构思A改B又改回A，author revision仍保留新输入，不被旧200清空', async () => {
  const response = deferred(), context = await panel({ api: { injectAutopilot: async () => response.promise } })
  context.vm.directive.value = '作者构思A'; const saving = context.vm.inject()
  context.vm.directive.value = '作者构思B'; context.vm.directive.value = '作者构思A'
  response.resolve(control()); await saving
  assert.equal(context.vm.directive.value, '作者构思A'); assert.equal(context.vm.authorDirty.value, true)
  assert.equal(context.vm.injecting.value, false); assert.equal(context.vm.directiveSubmission.value.state, 'confirmed')
  assert.match(textOf(context.render()), /上一次构思已接收，当前输入已保留/); context.dispose()
})

test('同编译module卸载重挂共享pending锁，旧200只记录确认不清新setup的作者稿或toast', async () => {
  const response = deferred(); let calls = 0
  const old = await panel({ api: { injectAutopilot: async () => ++calls === 1 ? response.promise : control('novel-a-task', { event_id: 3 }) } })
  old.vm.directive.value = '原次提交'; const saving = old.vm.inject(); old.dispose()
  const fresh = await old.mountAgain()
  assert.equal(fresh.vm.injecting.value, true); assert.equal(fresh.vm.directive.value, '原次提交')
  fresh.vm.directive.value = '重挂后的新构思'; await fresh.vm.inject(); assert.equal(injectionRequests(fresh).length, 1)
  const button = nodes(fresh.render(), node => node.type === 'nbutton' && textOf(node) === '注入')[0]
  assert.equal(button.props.disabled, true)
  response.resolve(control()); await saving
  assert.equal(fresh.vm.injecting.value, false); assert.equal(fresh.vm.directive.value, '重挂后的新构思')
  assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
  await fresh.vm.inject(); assert.equal(injectionRequests(fresh).length, 2); assert.equal(fresh.vm.directive.value, '')
  fresh.dispose()
})

test('novel ABA保留原task锁与各自构思，B可独立注入，旧A200不清返回A的新输入', async () => {
  const response = deferred()
  const context = await panel({ api: { injectAutopilot: async (id, taskId) => id === 'novel-a' ? response.promise : control(taskId) } })
  context.vm.directive.value = 'A原构思'; const saving = context.vm.inject()
  context.props.novelId = 'novel-b'; await flush(); context.vm.directive.value = 'B构思'; await context.vm.inject()
  assert.equal(context.vm.directive.value, ''); context.messages.length = 0
  context.props.novelId = 'novel-a'; await flush(); context.vm.directive.value = '返回A的新构思'
  await context.vm.inject(); assert.equal(injectionRequests(context).length, 2)
  response.resolve(control()); await saving
  assert.equal(context.vm.directive.value, '返回A的新构思'); assert.equal(context.messages.length, 0)
  assert.equal(context.vm.injecting.value, false); context.dispose()
})

test('新task允许独立提交且旧task晚200不清新锁输入，task身份ABA也使旧清输入失效', async () => {
  const oldResponse = deferred(), newResponse = deferred()
  const context = await panel({ api: { injectAutopilot: async (_id, taskId) => taskId === 'novel-a-task' ? oldResponse.promise : newResponse.promise } })
  context.vm.directive.value = '旧task构思'; const oldSaving = context.vm.inject()
  context.vm.applySnapshot(snapshot('novel-a', { task_id: 'new-task', event_id: 1 }))
  context.vm.directive.value = '新task构思'; const newSaving = context.vm.inject()
  assert.equal(injectionRequests(context).length, 2); assert.equal(context.vm.injecting.value, true)
  oldResponse.resolve(control()); await oldSaving
  assert.equal(context.vm.injecting.value, true); assert.equal(context.vm.directive.value, '新task构思'); assert.equal(context.messages.length, 0)
  newResponse.resolve(control('new-task')); await newSaving
  assert.equal(context.vm.directive.value, ''); context.dispose()
  const response = deferred(), aba = await panel({ api: { injectAutopilot: async () => response.promise } })
  aba.vm.directive.value = '作者保留的构思'; const saving = aba.vm.inject()
  aba.vm.applySnapshot(snapshot('novel-a', { task_id: 'intermediate-task', event_id: 1 }))
  aba.vm.applySnapshot(snapshot('novel-a', { event_id: 1 }))
  response.resolve(control()); await saving
  assert.equal(aba.vm.directive.value, '作者保留的构思'); assert.equal(aba.messages.length, 0); aba.dispose()
})

test('未知网络5xx408及429保留原提交和新稿，状态GET与重挂都不能伪确认或自动重复注入', async () => {
  for (const status of [undefined, 500, 502, 408, 429]) {
    const context = await panel({ api: { injectAutopilot: async () => { throw Object.assign(new Error('回应未知'), status ? { status } : {}) } } })
    context.vm.directive.value = '本次原提交'; await context.vm.inject(); context.vm.directive.value = '未知后作者的新稿'
    assert.equal(context.vm.injectionUnknown.value, true); assert.equal(context.vm.directiveSubmission.value.text, '本次原提交')
    await context.vm.inject(); await context.vm.refreshTaskStatus(); await context.vm.inject(); assert.equal(injectionRequests(context).length, 1)
    assert.equal(context.messages.some(message => message.type === 'success'), false)
    const section = nodes(context.render(), node => node.props?.['aria-label'] === '构思注入结果待核对')[0]
    assert.match(textOf(section), /可能已经被后台接收/); assert.match(textOf(section), /公开任务状态不能核对具体构思/)
    const original = nodes(section, node => node.type === 'textarea' && node.props?.['aria-label'] === '本次提交的构思原文')[0]
    assert.equal(original.props.value, '本次原提交'); assert.equal(original.props.readonly, '')
    const oldCalls = context.requests.length; context.dispose(); const fresh = await context.mountAgain()
    assert.equal(fresh.vm.injectionUnknown.value, true); assert.equal(fresh.vm.directive.value, '未知后作者的新稿')
    await fresh.vm.inject(); assert.equal(injectionRequests(fresh).length, 1)
    assert.equal(fresh.requests.slice(oldCalls).some(request => request.name === 'injectAutopilot'), false)
    await fresh.vm.copyDirectiveText(fresh.vm.directiveSubmission.value.text); await fresh.vm.copyDirectiveText(fresh.vm.directive.value)
    assert.deepEqual(fresh.copied, ['本次原提交', '未知后作者的新稿']); fresh.dispose()
  }
})

test('未知注入只封原task，pause resume和实时稿监控仍可用，新task须作者明确点击才注入', async () => {
  let calls = 0
  const context = await panel({ api: { injectAutopilot: async (_id, taskId) => { if (++calls === 1) throw new Error('原回应丢失'); return control(taskId) } } })
  context.vm.directive.value = '未知原构思'; await context.vm.inject()
  await context.vm.pause(); assert.equal(context.vm.pauseRequested.value, true)
  await context.vm.resume(); assert.equal(context.vm.pauseRequested.value, false)
  context.vm.handleEvent({ type: 'text', node_id: 'node-a', content: '后台继续保留的片段', draft_id: 'server-draft', draft_revision: 1, draft_attempt: 1, draft_persisted: true })
  assert.equal(context.vm.activeDraftList.value[0].text, '后台继续保留的片段'); assert.equal(injectionRequests(context).length, 1)
  context.vm.applySnapshot(snapshot('novel-a', { task_id: 'next-task', event_id: 1 }))
  assert.equal(context.vm.injectionUnknown.value, false); assert.equal(injectionRequests(context).length, 1)
  context.vm.directive.value = '新task明确的构思'; await context.vm.inject()
  assert.equal(injectionRequests(context).length, 2); assert.equal(injectionRequests(context)[1].args[1], 'next-task'); context.dispose()
})

test('明确4xx拒绝释放自己的task锁且保留输入，只在作者再次点击时重试', async () => {
  for (const status of [400, 401, 403, 404, 409, 422]) {
    let calls = 0
    const context = await panel({ api: { injectAutopilot: async (_id, taskId) => { if (++calls === 1) throw Object.assign(new Error('明确拒绝'), { status }); return control(taskId) } } })
    context.vm.directive.value = '保留以便修正'; await context.vm.inject()
    assert.equal(context.vm.directive.value, '保留以便修正'); assert.equal(context.vm.injecting.value, false); assert.equal(context.vm.injectionUnknown.value, false)
    assert.equal(context.messages[0].type, 'error'); assert.equal(injectionRequests(context).length, 1)
    context.vm.directive.value = '作者已修正并再次提交'; await context.vm.inject()
    assert.equal(injectionRequests(context).length, 2); assert.equal(context.vm.directive.value, ''); context.dispose()
  }
})

test('200确认须完整匹配type action task novel与合法新event，缺失或错来源一律未知且输入不清', async () => {
  const bad = [null, [], {}, control('wrong-task'), control('novel-a-task', { type: 'done' }), control('novel-a-task', { action: 'pause' }),
    control('novel-a-task', { novel_id: 'novel-b' }), control('novel-a-task', { event_id: 1 }), control('novel-a-task', { event_id: 1.5 }),
    control('novel-a-task', { event_id: NaN }), control('novel-a-task', { paused: undefined }), control('novel-a-task', { pause_requested: 'false' }),
    control('novel-a-task', { message: '' })]
  for (const result of bad) {
    const context = await panel({ api: { injectAutopilot: async () => result } })
    context.vm.directive.value = '回应异常仍保留的构思'; await context.vm.inject()
    assert.equal(context.vm.directive.value, '回应异常仍保留的构思'); assert.equal(context.vm.injectionUnknown.value, true)
    assert.equal(context.messages.some(message => message.type === 'success'), false)
    await context.vm.inject(); assert.equal(injectionRequests(context).length, 1); context.dispose()
  }
})

test('旧实例网络未知或明确拒绝跨remount完成只更新共享事实，不toast或清新输入', async () => {
  for (const status of [undefined, 403]) {
    const response = deferred()
    const old = await panel({ api: { injectAutopilot: async () => { await response.promise; throw Object.assign(new Error('旧实例回应'), status ? { status } : {}) } } })
    old.vm.directive.value = '原构思'; const saving = old.vm.inject(); old.dispose()
    const fresh = await old.mountAgain(); fresh.vm.directive.value = '新setup的作者输入'
    assert.equal(fresh.vm.injecting.value, true)
    response.resolve(); await saving
    assert.equal(fresh.vm.injecting.value, false); assert.equal(fresh.vm.injectionUnknown.value, status === undefined)
    assert.equal(fresh.vm.directive.value, '新setup的作者输入'); assert.equal(old.messages.length, 0); assert.equal(fresh.messages.length, 0)
    fresh.dispose()
  }
})

test('任务结束后的晚200不清构思或冒称本页继续注入，空scope和空白稿不发送', async () => {
  const response = deferred(), context = await panel({ api: { injectAutopilot: async () => response.promise } })
  context.vm.directive.value = '任务结束仍保留的构思'; const saving = context.vm.inject()
  context.vm.applySnapshot(snapshot('novel-a', { status: 'completed', running: false, event_id: 3 }))
  response.resolve(control()); await saving
  assert.equal(context.vm.directive.value, '任务结束仍保留的构思'); assert.equal(context.messages.length, 0)
  context.props.novelId = ''; await flush(); await context.vm.inject(); assert.equal(injectionRequests(context).length, 1)
  context.props.novelId = 'novel-b'; await flush(); context.vm.directive.value = '   '; await context.vm.inject()
  assert.equal(injectionRequests(context).length, 1); assert.match(context.messages.at(-1).text, /请输入构思/); context.dispose()
})

test('工作台实际导航消费构思dirty，取消保留输入；确认离页后同module重挂仍恢复且无自动注入', async () => {
  let source = (await readFile(new URL('./workbench-state.test.mjs', import.meta.url), 'utf8')).split('\ntest(')[0]
  for (const name of ['vue', '@vue/compiler-sfc', 'typescript']) source = source.replaceAll(`from '${name}'`, `from '${import.meta.resolve(name)}'`)
  source = source.replaceAll("from './chapter-fixtures.mjs'", `from '${new URL('./chapter-fixtures.mjs', import.meta.url).href}'`)
    .replaceAll('import.meta.url', JSON.stringify(new URL('./workbench-state.test.mjs', import.meta.url).href))
    .replaceAll("import.meta.resolve('vue')", JSON.stringify(import.meta.resolve('vue')))
  const { panel: workbench } = await import(moduleUrl(source + '\nexport { panel }'))
  const context = await panel(); context.vm.directive.value = '准备注入但还没提交的构思'; await flush()
  const parent = await workbench('WorkbenchView', { props: { initialTab: 'autopilot', initialNovelId: 'novel-a' } })
  parent.vm.autopilotDirty.value = context.emitted.filter(([name]) => name === 'dirty-change').at(-1)[1]
  parent.vm.changeTab('writing')
  assert.equal(parent.vm.activeTab.value, 'autopilot'); assert.equal(parent.vm.showNavigationConfirm.value, true)
  assert.match(parent.vm.navigationWarning.value, /未提交构思在当前页面内保留/); assert.match(parent.vm.navigationWarning.value, /完整刷新或关闭页面前请复制/)
  parent.vm.cancelNavigation(); assert.equal(parent.vm.activeTab.value, 'autopilot'); assert.equal(context.vm.directive.value, '准备注入但还没提交的构思')
  parent.vm.changeTab('writing'); parent.vm.confirmNavigation(); context.dispose()
  assert.equal(parent.vm.activeTab.value, 'writing'); const fresh = await context.mountAgain()
  assert.equal(fresh.vm.directive.value, '准备注入但还没提交的构思'); assert.equal(injectionRequests(fresh).length, 0)
  parent.dispose(); fresh.dispose()
})
