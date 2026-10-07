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
const chapterUrl = moduleUrl(compile(await readFile(new URL('../src/utils/chapter.ts', import.meta.url), 'utf8')))
const limitsUrl = moduleUrl(compile(await readFile(new URL('../src/utils/wordLimits.ts', import.meta.url), 'utf8')))
let sequence = 0
const flush = async () => { for (let i = 0; i < 15; i++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const changes = [
  { type: 'foreshadowing', action: 'create', data: { content: '待修正伏笔', related_characters: ['未知'] } },
  { type: 'character', action: 'create', data: { name: '已创建人物' } },
]
const preview = (patch = {}) => ({ changes: structuredClone(changes), matched: [], targets: [], reason: '', ...patch })

async function mount(name, props, api) {
  const messages = [], emitted = [], hooks = {}
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.__suggestionHarness = {
    api, streamSSE: async () => {}, ChapterRevisionPanel: {},
    NButton: {}, NCheckbox: {}, NEmpty: {}, NInput: {}, NInputNumber: {}, NList: {}, NListItem: {}, NModal: {}, NSelect: {}, NSpin: {}, NTag: {},
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((kind) => [kind, (text) => messages.push({ kind, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  const compiled = compileScript(parse(await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor,
    { id: 'suggestion-apply-test' }).content
  const source = 'const __harness = globalThis.__suggestionHarness\n' + compiled
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api(?:, streamSSE)? \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/from ['"]\.\.\/utils\/chapter['"]/g, `from '${chapterUrl}'`)
    .replace(/from ['"]\.\.\/utils\/wordLimits['"]/g, `from '${limitsUrl}'`)
    .replace(/import ChapterRevisionPanel from ['"]\.\/ChapterRevisionPanel.vue['"]/g, 'const { ChapterRevisionPanel } = __harness')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(moduleUrl(compile(source) + `\n// ${++sequence}`))).default
  const reactiveProps = reactive(props)
  const vm = component.setup(reactiveProps, { expose() {}, emit: (...args) => emitted.push(args) })
  hooks.mount?.(); await flush()
  return { vm, messages, emitted, props: reactiveProps, unmount: () => hooks.unmount?.() }
}

async function card(apply, patch = {}) {
  const requests = []
  const api = {
    ideaChat: async () => preview(patch),
    ideaApply: async (...args) => { requests.push(args); return apply(...args) },
  }
  return { ...await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '作者构思' }, api), requests }
}

async function writing(apply) {
  const requests = []
  const api = {
    listChapters: async () => [{ id: 'chapter-a', novel_id: 'novel-a', title: '第一章', content: '正文', directive: '', seq: 1,
      summary: '', outline_node_id: '', status: 'draft', tension_score: null, drift_score: null }],
    // 受控新后端契约，真实source hash的计算由后端回归覆盖。
    settleChapter: async () => ({ chapter_id: 'chapter-a', source_fingerprint: 'a'.repeat(64), summary: '摘要',
      suggestions: structuredClone(changes).map(({ type, data }) => ({ type, data })), drift: null, conflicts: [] }),
    settleApply: async (...args) => {
      requests.push(args)
      const result = await apply(...args)
      return { ...result,
        created: result.created.map((row, index) => ({ receipt_id: `fixture-receipt-${index}`, replayed: false, deleted: false, ...row })),
        new_count: result.new_count ?? result.created.filter((row) => !row.replayed).length,
        replayed_count: result.replayed_count ?? result.created.filter((row) => row.replayed).length,
      }
    },
  }
  const mounted = await mount('WritingPanel', { novelId: 'novel-a', initialChapterId: 'chapter-a' }, api)
  await mounted.vm.doSettle()
  return { ...mounted, requests }
}

test('真实建议卡全部失败保留原输入与具体原因，不emit已落地', async () => {
  const state = await card(async () => ({ applied: [], failed: [
    { index: 0, type: 'foreshadowing', error: '未知人物不存在' },
    { index: 1, type: 'character', error: '保存失败' },
  ] }))
  const before = [...state.vm.dataTexts.value]
  await state.vm.apply()
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.vm.dataTexts.value, before)
  assert.deepEqual(state.vm.selected.value, [true, true])
  assert.equal(state.vm.errors.value[0], '未知人物不存在')
  assert.equal(state.messages.filter((item) => item.kind === 'success').length, 0)
})

test('真实建议卡部分成功后仅重试失败项，最终汇总成功而不重复创建', async () => {
  let attempt = 0
  const state = await card(async () => ++attempt === 1
    ? { applied: [{ index: 1, type: 'character', action: 'create', id: 'created-person' }],
      failed: [{ index: 0, type: 'foreshadowing', error: '引用不存在' }] }
    : { applied: [{ index: 0, type: 'foreshadowing', action: 'create', id: 'created-clue' }], failed: [] })
  await state.vm.apply()
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.vm.selected.value, [true, false])
  assert.deepEqual(state.vm.completed.value, [false, true])
  state.vm.dataTexts.value[0] = JSON.stringify({ content: '已修正伏笔', related_characters: ['created-person'] })
  await state.vm.apply()
  assert.equal(state.requests[1][1].length, 1)
  assert.equal(state.requests[1][1][0].type, 'foreshadowing')
  assert.deepEqual(state.requests[1][1][0].data.related_characters, ['created-person'])
  assert.deepEqual(state.emitted[0], ['applied', [
    { type: 'character', action: 'create', id: 'created-person' },
    { type: 'foreshadowing', action: 'create', id: 'created-clue' },
  ]])
})

test('真实建议卡在筛选后的请求中仍映射正确原卡片索引', async () => {
  const state = await card(async () => ({ applied: [{ index: 1, type: 'location', action: 'create', id: 'place' }],
    failed: [{ index: 0, type: 'foreshadowing', error: '待修正' }] }), {
    changes: [...structuredClone(changes), { type: 'location', action: 'create', data: { name: '城' } }],
  })
  state.vm.selected.value[1] = false
  await state.vm.apply()
  assert.deepEqual(state.vm.completed.value, [false, false, true])
  assert.equal(state.vm.errors.value[0], '待修正')
  assert.deepEqual(state.emitted, [])
})

test('真实建议卡旧格式零成功不假emit，预览失败可编辑后确认', async () => {
  const state = await card(async () => ({ applied: [] }), {
    failed: [{ index: 0, type: 'foreshadowing', error: '存在多个同名人物，请填写ID' }],
  })
  assert.match(state.vm.errors.value[0], /同名/)
  await state.vm.apply()
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.vm.completed.value, [false, false])
  assert.equal(state.vm.appliedSummary.value.length, 0)
})

test('真实原子提炼卡拒绝错误后保留全部编辑，成功重试才emit', async () => {
  let fail = true
  const state = await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '构思', settingsOnly: true,
    conversationId: 'conversation', messageId: 'message' }, {
    previewMessageSettings: async () => preview(),
    applyMessageSettings: async () => {
      if (fail) throw new Error('关联人物不属于当前作品')
      return { applied: [
        { index: 0, type: 'foreshadowing', action: 'create', id: 'clue' },
        { index: 1, type: 'character', action: 'create', id: 'person' },
      ], failed: [] }
    },
  })
  await state.vm.apply()
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.vm.completed.value, [false, false])
  assert.deepEqual(state.vm.selected.value, [true, true])
  fail = false
  await state.vm.apply()
  assert.equal(state.emitted[0][1].length, 2)
})

test('真实定稿建议全部失败不关闭弹窗，显示错误并保留JSON修正入口', async () => {
  const state = await writing(async () => ({ created: [], failed: [
    { index: 0, type: 'foreshadowing', error: '关联人物不存在' },
    { index: 1, type: 'character', error: '人物保存失败' },
  ] }))
  await state.vm.confirmSettle()
  assert.equal(state.vm.showSettle.value, true)
  assert.deepEqual(state.vm.checkedSuggestion.value, [true, true])
  assert.equal(state.vm.settleErrors.value[0], '关联人物不存在')
  assert.equal(state.vm.settledCount.value, 0)
  assert.equal(state.messages.filter((item) => item.kind === 'success').length, 0)
})

test('真实定稿建议部分成功锁定已保存项，修正重试仅发送失败项并汇总', async () => {
  let attempt = 0
  const state = await writing(async () => ++attempt === 1
    ? { created: [{ index: 1, type: 'character', id: 'person' }], failed: [{ index: 0, type: 'foreshadowing', error: '关联人物不存在' }] }
    : { created: [{ index: 0, type: 'foreshadowing', id: 'clue' }], failed: [] })
  await state.vm.confirmSettle()
  assert.equal(state.vm.showSettle.value, true)
  assert.deepEqual(state.vm.checkedSuggestion.value, [true, false])
  assert.deepEqual(state.vm.settleCompleted.value, [false, true])
  state.vm.settleDataTexts.value[0] = JSON.stringify({ content: '修正伏笔', related_characters: ['person'] })
  await state.vm.confirmSettle()
  assert.equal(state.requests[1][2].length, 1)
  assert.deepEqual(state.requests[1][2][0].data.related_characters, ['person'])
  assert.equal(state.vm.settledCount.value, 2)
  assert.equal(state.vm.showSettle.value, false)
  assert.ok(state.messages.some((item) => item.kind === 'success' && item.text.includes('2 条')))
})

test('真实定稿建议空成功仍保留弹窗，重复确认不会并发写入', async () => {
  const pending = deferred()
  const state = await writing(async () => pending.promise)
  const first = state.vm.confirmSettle()
  await state.vm.confirmSettle()
  assert.equal(state.requests.length, 1)
  pending.resolve({ created: [] })
  await first
  assert.equal(state.vm.showSettle.value, true)
  assert.deepEqual(state.vm.checkedSuggestion.value, [true, true])
  assert.equal(state.messages.filter((item) => item.kind === 'success').length, 0)
})

test('真实建议卡作者放弃失败项后只完成已保存汇总，不重新写入', async () => {
  const state = await card(async () => ({ applied: [{ index: 1, type: 'character', action: 'create', id: 'person' }],
    failed: [{ index: 0, type: 'foreshadowing', error: '未知引用' }] }))
  await state.vm.apply()
  state.vm.selected.value[0] = false
  await state.vm.apply()
  assert.equal(state.requests.length, 1)
  assert.deepEqual(state.emitted[0], ['applied', [{ type: 'character', action: 'create', id: 'person' }]])
})

test('真实建议卡部分成功后关闭回显已保存项，未应用项不会假成功或重试', async () => {
  const state = await card(async () => ({ applied: [{ index: 1, type: 'character', action: 'create', id: 'person' }],
    failed: [{ index: 0, type: 'foreshadowing', error: '未知引用' }] }))
  await state.vm.apply()
  state.vm.cancelOrFinish()
  assert.equal(state.requests.length, 1)
  assert.deepEqual(state.emitted, [['applied', [{ type: 'character', action: 'create', id: 'person' }]]])
  assert.ok(state.messages.some((item) => item.text.includes('1 条建议未应用')))
})

test('真实建议卡未保存任何项时关闭只取消', async () => {
  const state = await card(async () => ({ applied: [], failed: [{ index: 0, type: 'foreshadowing', error: '未知引用' }] }))
  await state.vm.apply()
  state.vm.cancelOrFinish()
  assert.deepEqual(state.emitted, [['cancel']])
})

test('真实建议卡切换来源后旧预览不得覆盖新请求或结束其loading', async () => {
  const old = deferred(), current = deferred()
  const state = await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '来源' }, {
    ideaChat: (novelId) => novelId === 'novel-a' ? old.promise : current.promise,
  })
  state.props.novelId = 'novel-b'
  await flush()
  await state.vm.apply()
  old.resolve(preview({ reason: '旧作品' })); await flush()
  assert.equal(state.vm.result.value, null)
  assert.equal(state.vm.loading.value, true)
  assert.deepEqual(state.emitted, [])
  current.resolve(preview({ reason: '新作品' })); await flush()
  assert.equal(state.vm.result.value.reason, '新作品')
  assert.equal(state.vm.loading.value, false)
})

test('真实建议卡迟到预览失败在切换与卸载后不取消新卡或报错', async () => {
  for (const unload of [false, true]) {
    const old = deferred()
    const state = await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '来源' }, {
      ideaChat: (novelId) => novelId === 'novel-a' ? old.promise : Promise.resolve(preview({ reason: '新作品' })),
    })
    if (unload) state.unmount()
    else { state.props.novelId = 'novel-b'; await flush() }
    old.reject(new Error('旧预览失败')); await flush()
    assert.deepEqual(state.emitted, [])
    assert.deepEqual(state.messages, [])
    if (!unload) assert.equal(state.vm.result.value.reason, '新作品')
  }
})

test('真实建议卡确认使用解析时冻结来源，迟到成功不标记新来源', async () => {
  const pending = deferred(), requests = []
  const state = await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '原始构思' }, {
    ideaChat: async () => preview(),
    ideaApply: (...args) => { requests.push(args); return pending.promise },
  })
  const applying = state.vm.apply()
  state.props.novelId = 'novel-b'; await flush()
  pending.resolve({ applied: [{ index: 1, type: 'character', action: 'create', id: 'person-a' }], failed: [] })
  await applying
  assert.equal(requests[0][0], 'novel-a')
  assert.equal(requests[0][2], '原始构思')
  assert.deepEqual(state.vm.completed.value, [false, false])
  assert.deepEqual(state.vm.appliedSummary.value, [])
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.messages, [])
})

test('真实提炼卡确认使用原会话消息，切换后旧失败不干扰新应用', async () => {
  const old = deferred(), current = deferred(), requests = []
  const state = await mount('ApplySuggestionCard', { novelId: 'novel-a', text: '构思', settingsOnly: true,
    conversationId: 'conversation-a', messageId: 'message-a' }, {
    previewMessageSettings: async () => preview(),
    applyMessageSettings: (...args) => { requests.push(args); return args[1] === 'message-a' ? old.promise : current.promise },
  })
  const first = state.vm.apply()
  state.props.messageId = 'message-b'; await flush()
  const second = state.vm.apply()
  assert.deepEqual(requests.map((item) => item.slice(0, 2)), [['conversation-a', 'message-a'], ['conversation-a', 'message-b']])
  old.reject(new Error('旧确认失败')); await first
  assert.equal(state.vm.applying.value, true)
  assert.deepEqual(state.messages, [])
  assert.deepEqual(state.emitted, [])
  current.resolve({ applied: [{ index: 1, type: 'character', action: 'create', id: 'person-b' }], failed: [] })
  await second
  assert.equal(state.vm.applying.value, false)
  assert.equal(state.emitted[0][1][0].id, 'person-b')
})

test('真实建议卡卸载后确认成功不再通知父消息', async () => {
  const pending = deferred()
  const state = await card(async () => pending.promise)
  const applying = state.vm.apply()
  state.unmount()
  pending.resolve({ applied: [{ index: 1, type: 'character', action: 'create', id: 'person' }], failed: [] })
  await applying
  assert.deepEqual(state.emitted, [])
  assert.deepEqual(state.messages, [])
})
