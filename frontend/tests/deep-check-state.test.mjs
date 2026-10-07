import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'
import { metadataFixture, memoryStorage } from './chapter-fixtures.mjs'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'], ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../types', '../src/types.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const apiModule = await import(moduleUrl(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'))))
const flush = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const chapter = (id = 'chapter-a', novelId = 'novel-a') => ({ id, novel_id: novelId, outline_node_id: '', seq: id === 'chapter-a' ? 1 : 2,
  title: id, content: '林岚看到旧信。林岚知道了秘密。', directive: '', summary: '', status: 'draft', tension_score: null, drift_score: null,
  created_at: '', updated_at: '' })
const result = (patch = {}) => ({ status: 'complete', conflicts: [], rule_conflicts: [],
  chunks_total: 1, chunks_eligible: 1, chunks_attempted: 1, chunks_checked: 1, chunks_failed: 0,
  chunks_skipped: 0, chunks_skipped_rule: 0, chunks_skipped_no_entity: 0, chunks_unchecked: 0, truncated: false, warnings: [], ...patch })
const semantic = (patch = {}) => ({ rule: 'semantic', type: '人设矛盾', excerpt: '林岚看到旧信', reason: '与已保存的人物设定矛盾', severity: 'high', chunk_index: 0, ...patch })
const rule = (patch = {}) => ({ rule: 'known_info', message: '角色知道了不该知道的信息', start: 0, end: 2, entity_id: 'character-a', ...patch })
let sequence = 0

async function panel(options = {}) {
  const props = reactive({ novelId: 'novel-a', initialChapterId: 'chapter-a', ...options.props })
  const messages = [], requests = [], writes = [], hooks = {}
  globalThis.localStorage = memoryStorage()
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  const harness = {
    api: {
      listChapterMetadata: async (novelId) => [chapter('chapter-a', novelId), chapter('chapter-b', novelId)].map(metadataFixture),
      getChapter: async (novelId, id) => chapter(id, novelId),
      checkConsistencyDeep: async (...args) => { requests.push(structuredClone(args)); return options.check ? options.check(...args) : result() },
      updateChapter: async (...args) => { writes.push(args); throw new Error('检查不能写作者稿') },
      createChapter: async (...args) => { writes.push(args); throw new Error('检查不能新建作者稿') },
    },
    streamSSE: async () => { throw new Error('检查不应生成正文') },
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const name of ['NButton', 'NCheckbox', 'NEmpty', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NModal', 'NSelect', 'NTag']) harness[name] = name.toLowerCase()
  const key = `__deepCheckState${++sequence}`; globalThis[key] = harness
  const descriptor = parse(await readFile(new URL('../src/components/WritingPanel.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `deep-${sequence}` })
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
  await hooks.mount?.(); await flush()
  const template = compileTemplate({ source: descriptor.template.content, id: `deep-${sequence}`, filename: 'WritingPanel.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${sequence}`))
  return { vm, props, messages, requests, writes, render: () => rendered.render({}, [], props, proxyRefs(vm), {}, {}),
    dispose: () => { hooks.unmount?.(); scope.stop(); delete globalThis[key] } }
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
const section = (context) => nodes(context.render(), (node) => node.props?.['aria-label'] === '语义深度检查结果')[0]
const emptyDescription = (context) => nodes(section(context), (node) => node.type === 'nempty')[0]?.props.description

test('真实API深度检查保持POST路径和未保存稿载荷，不保存作者字段', async () => {
  const previous = globalThis.fetch, requests = []
  globalThis.fetch = async (url, options) => { requests.push({ url, ...options }); return new Response(JSON.stringify(result())) }
  try {
    const response = await apiModule.api.checkConsistencyDeep('novel-a', '作者未保存正文')
    assert.equal(response.status, 'complete'); assert.equal(requests[0].url, '/api/v1/novels/novel-a/consistency/deep')
    assert.equal(requests[0].method, 'POST'); assert.deepEqual(JSON.parse(requests[0].body), { content: '作者未保存正文' })
  } finally { globalThis.fetch = previous }
})

test('全部模型分块失败不绿色通过，真实模板明确未完成与失败覆盖', async () => {
  const context = await panel({ check: async () => result({ status: 'failed', chunks_checked: 0, chunks_failed: 1, warnings: ['第1块检查失败，请重试'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.messages.some((row) => row.type === 'success'), false)
  assert.equal(context.vm.deepResultConfirmed.value, true)
  assert.match(textOf(section(context)), /检查失败/)
  assert.match(textOf(section(context)), /失败 1 块/)
  assert.match(emptyDescription(context), /未完成/)
  assert.equal(context.writes.length, 0); context.dispose()
})

test('完整合法空结果仅承诺已检查范围，覆盖和人工校对始终可见', async () => {
  const context = await panel()
  const snapshot = context.vm.savedSnapshot.value, body = context.vm.content.value
  await context.vm.doDeepCheck()
  assert.deepEqual(context.requests, [['novel-a', body]])
  assert.deepEqual(context.messages, [{ type: 'success', text: '已检查范围内未发现明显冲突，仍需人工校对' }])
  assert.equal(context.vm.deepClean.value, true); assert.equal(context.vm.deepResultConfirmed.value, true)
  assert.match(textOf(section(context)), /已完成可核对范围/); assert.match(textOf(section(context)), /可核对 1 块.*尝试 1 块.*已完成 1 块/)
  assert.match(emptyDescription(context), /已检查范围内.*仍需人工校对/)
  assert.equal(nodes(section(context), (node) => node.type === 'ntag' && node.props.type === 'success').length, 1)
  assert.equal(context.vm.content.value, body); assert.deepEqual(context.vm.savedSnapshot.value, snapshot)
  assert.equal(context.writes.length, 0); context.dispose()
})

test('部分失败仍保留真实语义冲突和引文定位，失败块不计成功也不伪称clean', async () => {
  const finding = semantic(), context = await panel({ check: async () => result({ status: 'partial',
    chunks_total: 2, chunks_eligible: 2, chunks_attempted: 2, chunks_failed: 1, conflicts: [finding], warnings: ['第2块检查失败，请重试'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.deepEqual(context.vm.deepConflicts.value, [finding])
  const output = textOf(section(context))
  assert.match(output, /部分完成/); assert.match(output, /已完成 1 块.*失败 1 块/)
  assert.match(output, /第2块检查失败/); assert.match(output, /人设矛盾.*林岚看到旧信.*人物设定/)
  assert.equal(context.messages[0].type, 'warning'); assert.equal(nodes(section(context), (node) => node.type === 'ntag' && node.props.type === 'success').length, 0)
  let located
  context.vm.contentRef.value = { focus() {}, setSelectionRange: (...range) => { located = range }, scrollHeight: 100 }
  nodes(section(context), (node) => !!node.props?.onClick)[0].props.onClick()
  assert.deepEqual(located, [0, finding.excerpt.length]); assert.equal(context.writes.length, 0)
  context.dispose()
})

test('部分失败空冲突仅显示未完成提示，不能把失败块当未发现问题', async () => {
  const context = await panel({ check: async () => result({ status: 'partial', chunks_total: 2, chunks_eligible: 2,
    chunks_attempted: 2, chunks_failed: 1, warnings: ['第2块返回无效结构'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepClean.value, false); assert.match(emptyDescription(context), /部分范围未完成核对/)
  assert.equal(context.messages.some((row) => row.type === 'success'), false)
  assert.match(textOf(section(context)), /返回无效结构/); context.dispose()
})

test('无相关设定的零eligible明确未执行与跳过原因，零调用不等于检查通过', async () => {
  const context = await panel({ check: async () => result({ status: 'skipped', chunks_eligible: 0, chunks_attempted: 0,
    chunks_checked: 0, chunks_skipped: 1, chunks_skipped_no_entity: 1, warnings: ['正文没有出现可核对的实体名称'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.match(textOf(section(context)), /未执行语义检查/)
  assert.match(textOf(section(context)), /可核对 0 块.*尝试 0 块.*无相关设定 1 块/)
  assert.match(emptyDescription(context), /没有可核对分块/); assert.equal(context.messages[0].type, 'warning')
  context.dispose()
})

test('规则命中跳过块直接显示规则冲突，不冒称语义空结果已通过', async () => {
  const finding = rule(), context = await panel({ check: async () => result({ status: 'skipped', chunks_eligible: 0,
    chunks_attempted: 0, chunks_checked: 0, chunks_skipped: 1, chunks_skipped_rule: 1,
    rule_conflicts: [finding], warnings: ['规则命中块未重复执行语义检查'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.deepEqual(context.vm.deepRuleConflicts.value, [finding])
  const output = textOf(section(context))
  assert.match(output, /未执行语义检查/); assert.match(output, /规则命中 1 块/)
  assert.match(output, /规则层一致性冲突.*known_info.*角色知道了不该知道的信息/)
  assert.equal(nodes(section(context), (node) => node.props?.['aria-label'] === '深度检查发现的规则冲突').length, 1)
  assert.equal(context.vm.conflicts.value.length, 0); assert.equal(context.messages[0].type, 'warning')
  context.dispose()
})

test('9个eligible只查8块时部分完成，未查1块与8块上限在真实模板可见', async () => {
  const context = await panel({ check: async () => result({ status: 'partial', chunks_total: 9, chunks_eligible: 9,
    chunks_attempted: 8, chunks_checked: 8, chunks_unchecked: 1, truncated: true, warnings: ['第9块未检查'] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.equal(context.vm.deepClean.value, false)
  assert.match(textOf(section(context)), /可核对 9 块.*尝试 8 块.*已完成 8 块.*未查 1 块/)
  assert.match(textOf(section(context)), /部分正文未检查（上限 8 块）/)
  assert.match(emptyDescription(context), /部分范围未完成核对/); assert.equal(context.messages[0].type, 'warning')
  context.dispose()
})

test('可核对范围完成但规则层有冲突时不绿色clean，规则与跳过范围同显', async () => {
  const context = await panel({ check: async () => result({ chunks_total: 2, chunks_skipped: 1, chunks_skipped_rule: 1, rule_conflicts: [rule()] }) })
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.equal(context.vm.deepMeta.value.status, 'complete')
  assert.equal(context.vm.deepClean.value, false); assert.equal(context.messages[0].type, 'warning')
  assert.match(emptyDescription(context), /规则层发现问题/)
  assert.equal(nodes(section(context), (node) => node.type === 'ntag' && node.props.type === 'success').length, 0)
  context.dispose()
})

test('旧backend缺状态/计数/数组不绿，保留合法真实返回冲突且提示核对版本', async () => {
  for (const payload of [
    { conflicts: [], chunks_checked: 1, chunks_skipped: 0, truncated: false },
    { conflicts: [semantic()], chunks_checked: 1, chunks_skipped: 0, truncated: false },
    { conflicts: [semantic()], rule_conflicts: [rule()], status: 'complete' },
  ]) {
    const context = await panel({ check: async () => payload })
    await context.vm.doDeepCheck()
    assert.equal(context.vm.deepResultConfirmed.value, false); assert.equal(context.vm.deepCoverageConfirmed.value, false)
    assert.equal(context.messages[0].type, 'warning'); assert.equal(context.vm.deepClean.value, false)
    assert.deepEqual(context.vm.deepConflicts.value, payload.conflicts); assert.deepEqual(context.vm.deepRuleConflicts.value, payload.rule_conflicts ?? [])
    assert.match(textOf(section(context)), /未完整确认.*服务版本/)
    assert.equal(textOf(section(context)).includes('共 0 块'), false)
    context.dispose()
  }
})

test('非法root/counts/status/truncated/数组形状不能完整确认，也不会抛错覆盖稿件', async () => {
  for (const payload of [null, [], '完成', result({ chunks_total: -1 }), result({ chunks_checked: 0.5 }), result({ chunks_eligible: NaN }),
    result({ chunks_total: 2 }), result({ chunks_attempted: 9 }), result({ status: 'skipped' }), result({ truncated: true }),
    result({ chunks_skipped: 1 }), result({ chunks_failed: 1 }), result({ chunks_unchecked: 1 }),
    result({ warnings: '警告' }), result({ warnings: [null] }), result({ conflicts: {} }), result({ rule_conflicts: undefined }),
    result({ chunks_total: 9, chunks_eligible: 9, chunks_attempted: 1, chunks_checked: 1, chunks_unchecked: 8, truncated: true, status: 'partial' })]) {
    const context = await panel({ check: async () => payload }), body = context.vm.content.value
    await context.vm.doDeepCheck()
    assert.equal(context.vm.deepResultConfirmed.value, false); assert.equal(context.vm.deepClean.value, false)
    assert.equal(context.messages.some((row) => row.type === 'success'), false); assert.match(textOf(section(context)), /未完整确认/)
    assert.equal(context.vm.content.value, body); assert.equal(context.writes.length, 0)
    context.dispose()
  }
})

test('非法冲突项不进可核对列表，合法项保留但整份回应不能伪称完整', async () => {
  for (const patch of [{ conflicts: [semantic(), semantic({ severity: 'fatal' })] }, { conflicts: [semantic({ excerpt: '别处的正文' })] },
    { conflicts: [semantic({ chunk_index: 1 })] }, { conflicts: [semantic({ chunk_index: -1 })] }, { conflicts: [semantic({ rule: 'other' })] },
    { conflicts: [semantic({ reason: '' })] }, { conflicts: [semantic({ type: '' })] },
    { rule_conflicts: [rule({ end: 999 })], chunks_total: 2, chunks_skipped: 1, chunks_skipped_rule: 1 },
    { rule_conflicts: [rule({ start: -1 })], chunks_total: 2, chunks_skipped: 1, chunks_skipped_rule: 1 },
    { rule_conflicts: [rule({ entity_id: '' })], chunks_total: 2, chunks_skipped: 1, chunks_skipped_rule: 1 }]) {
    const context = await panel({ check: async () => result(patch) })
    await context.vm.doDeepCheck()
    assert.equal(context.vm.deepResultConfirmed.value, false); assert.equal(context.messages[0].type, 'warning')
    assert.deepEqual(context.vm.deepConflicts.value, patch.conflicts?.length === 2 ? [semantic()] : [])
    assert.deepEqual(context.vm.deepRuleConflicts.value, []); context.dispose()
  }
})

test('规则offset按Unicode码点验证，emoji与中文合法冲突保留', async () => {
  const payload = result({ status: 'skipped', chunks_eligible: 0, chunks_attempted: 0, chunks_checked: 0,
    chunks_skipped: 1, chunks_skipped_rule: 1, rule_conflicts: [rule({ start: 1, end: 3 })] })
  const context = await panel({ check: async () => payload }); context.vm.content.value = '😀林岚'
  await context.vm.doDeepCheck()
  assert.equal(context.vm.deepResultConfirmed.value, true); assert.equal(context.vm.deepRuleConflicts.value.length, 1)
  context.dispose()
})

test('重检挂起先清旧结果，waiting模板不显示上一次clean；网络失败明确本次未完成', async () => {
  const pending = deferred(); let count = 0
  const context = await panel({ check: async () => ++count === 1 ? result({ conflicts: [semantic()] }) : pending.promise })
  await context.vm.doDeepCheck(); assert.equal(context.vm.deepConflicts.value.length, 1)
  const checking = context.vm.doDeepCheck()
  assert.equal(context.vm.deepMeta.value, null); assert.deepEqual(context.vm.deepConflicts.value, [])
  assert.match(textOf(section(context)), /正在检查当前稿件，本次结果尚未返回/)
  assert.equal(textOf(section(context)).includes('人设矛盾'), false)
  await context.vm.doDeepCheck(); assert.equal(context.requests.length, 2)
  pending.reject(new Error('模型连接未完成')); await checking
  assert.equal(context.vm.deepResultConfirmed.value, false); assert.equal(context.vm.deepLoading.value, false)
  assert.match(textOf(section(context)), /请求未完成/); assert.match(emptyDescription(context), /请求未完成/)
  assert.equal(context.messages.at(-1).type, 'error'); context.dispose()
})

test('正文/标题/指令/作品/章节ABA以及卸载不接纳旧深度回应或错误', async () => {
  for (const change of ['content', 'title', 'directive', 'novel', 'chapter', 'unmount']) for (const outcome of ['success', 'failure']) {
    const pending = deferred(), context = await panel({ check: async () => pending.promise })
    const checking = context.vm.doDeepCheck()
    if (['content', 'title', 'directive'].includes(change)) { const original = context.vm[change].value; context.vm[change].value = '新作者输入'; context.vm[change].value = original }
    else if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await flush() }
    else if (change === 'chapter') { await context.vm.selectChapter(metadataFixture(chapter('chapter-b')), true); await context.vm.selectChapter(metadataFixture(chapter()), true) }
    else context.dispose()
    if (outcome === 'success') pending.resolve(result({ conflicts: [semantic()] })); else pending.reject(new Error('旧请求失败'))
    await checking
    assert.equal(context.vm.deepMeta.value, null); assert.equal(context.vm.deepResultConfirmed.value, false)
    assert.deepEqual(context.vm.deepConflicts.value, []); assert.deepEqual(context.vm.deepRuleConflicts.value, [])
    assert.equal(context.messages.length, 0)
    if (change !== 'unmount') context.dispose()
  }
})

test('旧正文请求完成不停止新正文的loading，也不倒退最新完整结果', async () => {
  const old = deferred(), current = deferred(); let count = 0
  const context = await panel({ check: async () => ++count === 1 ? old.promise : current.promise })
  const first = context.vm.doDeepCheck(); context.vm.content.value += '作者新增句子。'; const second = context.vm.doDeepCheck()
  old.resolve(result({ status: 'failed', chunks_checked: 0, chunks_failed: 1 })); await first
  assert.equal(context.vm.deepLoading.value, true); assert.equal(context.vm.deepMeta.value, null); assert.equal(context.messages.length, 0)
  current.resolve(result()); await second
  assert.equal(context.vm.deepClean.value, true); assert.equal(context.messages.length, 1)
  assert.equal(context.vm.content.value.endsWith('作者新增句子。'), true); context.dispose()
})
