import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
let harness = (await readFile(new URL('./manual-writing.test.mjs', import.meta.url), 'utf8')).split('\ntest(')[0]
for (const name of ['vue', '@vue/compiler-sfc', 'typescript']) harness = harness.replaceAll(`from '${name}'`, `from '${import.meta.resolve(name)}'`)
harness = harness.replace("from './chapter-fixtures.mjs'", `from '${new URL('./chapter-fixtures.mjs', import.meta.url).href}'`)
  .replace('globalThis.__manualWriting = harness', "harness.NModal = { name: 'NModal' }; globalThis.__manualWriting = harness")
  .replaceAll('import.meta.url', JSON.stringify(new URL('./manual-writing.test.mjs', import.meta.url).href))
  .replaceAll("import.meta.resolve('vue')", JSON.stringify(import.meta.resolve('vue')))
const { writing, wait, chapter, settle } = await import(moduleUrl(harness + '\nexport { writing, wait, chapter, settle }'))

const sourceFields = row => ({ title: row.title, content: row.content, directive: row.directive })
const candidate = (payload, replacement) => Array.from(payload.expected_author_fields.content).slice(0, payload.selection_start).join('')
  + replacement + Array.from(payload.expected_author_fields.content).slice(payload.selection_end).join('')
function done(payload, replacement, patch = {}) {
  return { type: 'done', preview: true, content: candidate(payload, replacement),
    chapter: chapter('novel-a', payload.expected_author_fields), expected_author_fields: { ...payload.expected_author_fields },
    selection_start: payload.selection_start, selection_end: payload.selection_end, ...patch }
}
const updates = context => context.requests.filter(row => row.name === 'updateChapter')
function nodes(node, predicate, found = []) {
  if (!node || typeof node !== 'object') return found
  if (predicate(node)) found.push(node)
  if (Array.isArray(node.children)) node.children.forEach(child => nodes(child, predicate, found))
  else if (node.children && typeof node.children === 'object') for (const slot of Object.values(node.children)) if (typeof slot === 'function') {
    const children = slot(); (Array.isArray(children) ? children : [children]).forEach(child => nodes(child, predicate, found))
  }
  return found
}
const textOf = node => typeof node === 'string' ? node : Array.isArray(node) ? node.map(textOf).join('') : typeof node?.children === 'string' ? node.children
  : Array.isArray(node?.children) ? node.children.map(textOf).join('')
    : node?.children && typeof node.children === 'object' ? Object.values(node.children).filter(value => typeof value === 'function').map(fn => textOf(fn())).join('') : ''
function button(context, label) { return nodes(context.render(), row => row.type === 'nbutton' && textOf(row).trim() === label)[0] }
const rewriteChapter = patch => chapter('novel-a', { content: '原稿🙂\u0000前缀。\n　相同段落。　\n中间。\n　相同段落。　\n未选尾声。', directive: '原章前指令', ...patch })
async function preview(options = {}) {
  let payload, callbacks
  const saved = rewriteChapter(options.chapter)
  const replacement = options.replacement ?? '\n　候选🙂\u0000段落。　\n'
  const context = await writing({ props: { initialChapterId: saved.id }, api: {
    listChapterMetadata: async () => [saved], getChapter: async () => saved,
    updateChapter: async (_novel, _id, data) => ({ ...saved, content: data.content, status: 'draft', summary: '', tension_score: null, drift_score: null }),
    ...options.api,
  }, streamSSE: async (url, body, handlers) => {
    payload = structuredClone(body); callbacks = handlers
    if (options.stream) await options.stream(url, body, handlers)
    else { handlers.onToken(replacement); await handlers.onDone(done(body, replacement, options.patch)) }
  } })
  const passage = options.passage ?? '　相同段落。　'
  const start = saved.content.lastIndexOf(passage)
  context.vm.contentRef.value = { selectionStart: start, selectionEnd: start + passage.length }
  if (!options.defer) await context.vm.doRewrite()
  return { ...context, saved, replacement, payload: () => payload, callbacks: () => callbacks }
}

test('真实选区第二个重复段保留首尾空白，emoji/NUL之前的UTF16位置转换为Python码点并冻结三作者字段', async () => {
  const context = await preview(), payload = context.payload(), original = context.saved.content
  assert.equal(payload.passage, '　相同段落。　')
  assert.equal(payload.selection_start, Array.from(original.slice(0, original.lastIndexOf(payload.passage))).length)
  assert.notEqual(payload.selection_start, original.lastIndexOf(payload.passage))
  assert.deepEqual(payload.expected_author_fields, sourceFields(context.saved))
  assert.equal(context.vm.previewGuarded.value, true); assert.equal(context.vm.content.value, original)
  assert.equal(button(context, '应用').props.disabled, false)
  await context.vm.applyPreview()
  const update = updates(context)[0]
  assert.deepEqual(update.args, ['novel-a', 'novel-a-chapter', {
    content: candidate(payload, context.replacement), revision_source: 'rewrite_apply', expected_author_fields: sourceFields(context.saved),
  }])
  assert.ok(update.args[2].content.includes('前缀。\n　相同段落。　\n中间。'))
  assert.ok(update.args[2].content.endsWith(context.replacement + '\n未选尾声。'))
  assert.equal(context.vm.content.value, update.args[2].content); assert.equal(context.vm.isDirty.value, false)
  context.dispose()
})

test('缺旧协议或错来源/范围/正文的回应保留候选可复制，但真实模板及直接handler均禁应用', async () => {
  for (const patch of [
    { expected_author_fields: undefined }, { selection_start: undefined }, { selection_end: undefined }, { type: 'token' },
    { preview: false }, { selection_start: true }, { selection_end: 1.5 }, { chapter: undefined },
    { chapter: chapter('novel-b') }, { chapter: chapter('novel-a', { title: '另一个标题' }) },
    { expected_author_fields: { title: '旧标题', content: '旧稿', directive: '' } }, { content: '错拼第一处或丢首尾' },
  ]) {
    const context = await preview({ patch })
    assert.equal(context.vm.previewGuarded.value, false); assert.equal(context.vm.showPreview.value, true)
    assert.equal(context.vm.content.value, context.saved.content); assert.equal(context.vm.previewNew.value, context.replacement)
    assert.match(context.vm.previewError.value, /原稿.*选区.*核对/)
    assert.equal(button(context, '应用').props.disabled, true)
    assert.ok(button(context, '重新读取章节核对'))
    assert.equal(nodes(context.render(), row => row.type === 'textarea' && row.props?.['aria-label'] === '改写候选段落')[0].props.value, context.replacement)
    await context.vm.applyPreview(); assert.equal(updates(context).length, 0)
    context.dispose()
  }
})

test('没有选中或只有空白不请求模型，精确原文不按trim后的范围猜测', async () => {
  const context = await preview({ defer: true })
  for (const range of [{ selectionStart: 0, selectionEnd: 0 }, { selectionStart: -1, selectionEnd: 3 }, { selectionStart: 0, selectionEnd: 1.5 }]) {
    context.vm.contentRef.value = range; await context.vm.doRewrite(); assert.equal(context.payload(), undefined)
  }
  const blank = context.saved.content.indexOf('\n')
  context.vm.contentRef.value = { selectionStart: blank, selectionEnd: blank + 1 }
  await context.vm.doRewrite(); assert.equal(context.payload(), undefined)
  assert.equal(context.vm.content.value, context.saved.content); context.dispose()
})

test('生成期间作者新输入或改回原值不接受旧预览，冻结的模型payload保持精确原稿', async () => {
  for (const changedBack of [false, true]) {
    const response = wait(), context = await preview({ defer: true, stream: async (_url, body, handlers) => {
      handlers.onToken('保留的候选输出'); await response.promise; handlers.onDone(done(body, '保留的候选输出'))
    } })
    const generating = context.vm.doRewrite(); await settle()
    const original = context.vm.content.value
    context.vm.content.value = '新的作者输入'; if (changedBack) context.vm.content.value = original
    response.resolve(); await generating
    assert.deepEqual(context.payload().expected_author_fields, sourceFields(context.saved))
    assert.equal(context.vm.content.value, changedBack ? original : '新的作者输入')
    assert.equal(context.vm.showPreview.value, false); assert.equal(context.vm.streamText.value, '保留的候选输出')
    assert.equal(context.vm.hasIncompleteOutput.value, true); await context.vm.applyPreview(); assert.equal(updates(context).length, 0)
    context.dispose()
  }
})

test('作品/章节ABA与卸载使旧预览完成和错误静默，不污染新稿', async () => {
  for (const change of ['novel', 'chapter', 'unmount']) for (const outcome of ['done', 'conflict']) {
    const response = wait(), context = await preview({ defer: true, stream: async (_url, body, handlers) => {
      await response.promise
      if (outcome === 'done') { handlers.onToken('旧候选'); handlers.onDone(done(body, '旧候选')) }
      else handlers.onError('旧来源已变化', 'chapter_write_conflict')
    } })
    const generating = context.vm.doRewrite(); await settle()
    if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (change === 'chapter') { await context.vm.startNew(true); await context.vm.selectChapter(context.saved, true) }
    if (change === 'unmount') context.dispose()
    context.vm.content.value = '当前作者新稿'; response.resolve(); await generating
    assert.equal(context.vm.content.value, '当前作者新稿'); assert.equal(context.vm.showPreview.value, false)
    assert.equal(context.messages.length, 0); assert.equal(updates(context).length, 0)
    if (change !== 'unmount') context.dispose()
  }
})

test('应用收到真实来源409保留原文/候选/等待期新输入，禁止重发且复制精确稿件', async () => {
  const response = wait(), copied = []
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async value => copied.push(value) } } })
  const context = await preview({ api: { updateChapter: async () => response.promise } })
  const payload = context.payload(), full = candidate(payload, context.replacement), applying = context.vm.applyPreview()
  context.vm.title.value = '等待期间新标题'; context.vm.directive.value = '新的指令'; context.vm.content.value = '绝不能清除的新稿'
  response.reject(Object.assign(new Error('另一窗口改稿'), { status: 409, code: 'chapter_write_conflict' })); await applying
  assert.equal(context.vm.content.value, '绝不能清除的新稿'); assert.equal(context.vm.title.value, '等待期间新标题')
  assert.equal(context.vm.previewFullContent.value, full); assert.equal(context.vm.previewNew.value, context.replacement)
  assert.equal(context.vm.previewContext.value.source.content, context.saved.content)
  assert.equal(context.vm.showPreview.value, true); assert.equal(context.vm.previewApplying.value, false)
  assert.match(context.vm.previewError.value, /未应用.*保留.*核对/)
  assert.equal(button(context, '应用').props.disabled, true); await context.vm.applyPreview(); assert.equal(updates(context).length, 1)
  await context.vm.copyRewritePreview(context.vm.previewFullContent.value); await context.vm.copyRewritePreview(context.vm.previewContext.value.source.content)
  assert.deepEqual(copied, [full, context.saved.content]); assert.equal(context.vm.isDirty.value, true)
  context.dispose()
})

test('模型前source409有可达核对入口，未生成也不保存，读取须作者确认且取消保留当前稿', async () => {
  const context = await preview({ stream: async (_url, _body, handlers) => handlers.onError('原稿已变化', 'chapter_write_conflict') })
  assert.equal(context.vm.showPreview.value, true); assert.equal(context.vm.previewGuarded.value, false)
  assert.ok(button(context, '重新读取章节核对')); const reads = context.requests.filter(row => row.name === 'getChapter').length
  const refreshing = context.vm.refreshPreviewChapter(); await settle()
  assert.equal(context.vm.showDiscard.value, true); assert.equal(context.vm.discardConfirmText.value, '读取已保存章节')
  context.vm.finishDiscard(false); await refreshing
  assert.equal(context.requests.filter(row => row.name === 'getChapter').length, reads)
  assert.equal(context.vm.content.value, context.saved.content); assert.equal(context.vm.showPreview.value, true)
  assert.equal(updates(context).length, 0); context.dispose()
})

test('读取核对的确认期间及GET等待新编辑/改回不覆盖作者输入，旧来源读取静默', async () => {
  for (const stage of ['confirmation', 'request', 'aba', 'unmount']) {
    const reading = wait(); let calls = 0
    const context = await preview({ patch: { expected_author_fields: undefined }, api: { getChapter: async () => ++calls === 1 ? rewriteChapter() : reading.promise } })
    const refreshing = context.vm.refreshPreviewChapter(); await settle()
    if (stage !== 'confirmation') { context.vm.finishDiscard(true); await settle() }
    if (stage === 'aba') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
    if (stage === 'unmount') context.dispose()
    const old = context.vm.content.value; context.vm.content.value = '刷新途中输入'; if (stage === 'request') context.vm.content.value = old
    if (stage === 'confirmation') context.vm.finishDiscard(true)
    reading.resolve(chapter('novel-a', { content: '另一窗口已保存正文' })); await refreshing
    assert.equal(context.vm.content.value, stage === 'request' ? old : '刷新途中输入')
    assert.equal(context.messages.some(row => /已读取章节/.test(row.text)), false)
    if (stage === 'confirmation') assert.equal(calls, 1)
    if (stage !== 'unmount') context.dispose()
  }
})

test('明确读取新详情不把原来源改成最新来套用旧候选，而是关闭旧候选后重新选段', async () => {
  let calls = 0
  const fresh = chapter('novel-a', { title: '新的已存标题', content: '最新已保存正文🙂', directive: '最新指令' })
  const context = await preview({ patch: { selection_end: undefined }, api: { getChapter: async () => ++calls === 1 ? rewriteChapter() : fresh } })
  const original = context.vm.previewContext.value.source.content
  const refreshing = context.vm.refreshPreviewChapter(); await settle(); context.vm.finishDiscard(true); await refreshing
  assert.notEqual(original, fresh.content); assert.equal(context.vm.content.value, fresh.content)
  assert.equal(context.vm.isDirty.value, false); assert.equal(context.vm.previewContext.value, null)
  assert.equal(context.vm.showPreview.value, false); await context.vm.applyPreview(); assert.equal(updates(context).length, 0)
  context.dispose()
})

test('详情核对失败不丢候选且可重新读取；手动复制失败仍有只读候选输入框', async () => {
  let calls = 0
  const context = await preview({ patch: { expected_author_fields: undefined }, api: { getChapter: async () => ++calls === 1 ? rewriteChapter() : Promise.reject(new Error('详情断网')) } })
  const full = context.vm.previewFullContent.value, refreshing = context.vm.refreshPreviewChapter()
  await settle(); context.vm.finishDiscard(true); await refreshing
  assert.equal(context.vm.previewFullContent.value, full); assert.equal(context.vm.previewRefreshing.value, false)
  assert.match(context.vm.previewError.value, /读取未完成.*仍保留/)
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async () => { throw new Error('拒绝复制') } } } })
  await context.vm.copyRewritePreview(full)
  assert.match(context.messages.at(-1).text, /只读.*手动.*复制/)
  assert.equal(nodes(context.render(), row => row.type === 'textarea' && row.props?.['aria-label'] === '改写候选全文')[0].props.value, full)
  context.dispose()
})

test('已确认应用后目录失败准确报告已保存，保留真实草稿基线并阻止双击及Esc误取消', async () => {
  const response = wait(); let reads = 0
  const context = await preview({ api: {
    listChapterMetadata: async () => ++reads === 1 ? [rewriteChapter()] : Promise.reject(new Error('目录503')),
    updateChapter: async () => response.promise,
  } })
  const full = context.vm.previewFullContent.value, applying = context.vm.applyPreview()
  await context.vm.applyPreview(); context.vm.handlePreviewVisibility(false); context.vm.discardPreview()
  assert.equal(context.vm.showPreview.value, true); assert.equal(context.vm.previewApplying.value, true)
  const modal = nodes(context.render(), row => row.type?.name === 'NModal' && row.props?.title === '改写预览（尚未落库）')[0]
  assert.equal(modal.props['close-on-esc'], false)
  response.resolve(chapter('novel-a', { content: full, status: 'draft', summary: '', tension_score: null, drift_score: null })); await applying
  assert.equal(updates(context).length, 1); assert.equal(context.vm.savedSnapshot.value.content, full)
  assert.equal(context.vm.selectedChapter.value.status, 'draft'); assert.equal(context.vm.isDirty.value, false)
  assert.match(context.messages.at(-1).text, /已应用改写.*刷新失败/); assert.equal(context.vm.showPreview.value, false)
  context.dispose()
})

test('应用成功后目录读回最新状态评分，末尾不得用旧保存回应倒退目录事实', async () => {
  const directory = wait(); let reads = 0, server = rewriteChapter()
  const context = await preview({ api: {
    listChapterMetadata: async () => ++reads === 2 ? directory.promise : [server],
    updateChapter: async (_novel, _id, data) => { server = { ...server, ...data, status: 'draft', summary: '', tension_score: null, drift_score: null }; return server },
  } })
  const applying = context.vm.applyPreview(); await settle()
  const savedContent = server.content
  directory.resolve([{ ...server, status: 'finalized', tension_score: 9, drift_score: 0.12 }]); await applying
  assert.equal(context.vm.content.value, savedContent)
  assert.equal(context.vm.savedSnapshot.value.content, savedContent)
  assert.equal(context.vm.selectedChapter.value.content, savedContent)
  assert.equal(context.vm.selectedChapter.value.status, 'finalized')
  assert.equal(context.vm.selectedChapter.value.tension_score, 9)
  assert.equal(context.vm.chapters.value[0].drift_score, 0.12)
  assert.equal(context.vm.isDirty.value, false); context.dispose()
})

test('真实API客户端保留rewrite_apply的冻结expected三字段和409机器码，普通手写保存合同不变', async () => {
  const compiled = ts.transpileModule(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  }).outputText
  const { api } = await import(moduleUrl(compiled)), requests = [], originalFetch = globalThis.fetch
  globalThis.fetch = async (url, options) => { requests.push({ url, options }); return new Response(JSON.stringify({ detail: '原稿变化', code: 'chapter_write_conflict' }), { status: 409 }) }
  try {
    const source = { title: '原标题', content: '🙂\u0000原稿', directive: '原指令' }
    await assert.rejects(api.updateChapter('novel-a', 'chapter-a', { content: '候选', revision_source: 'rewrite_apply', expected_author_fields: source }), error => error.status === 409 && error.code === 'chapter_write_conflict')
    assert.equal(requests[0].url, '/api/v1/novels/novel-a/chapters/chapter-a')
    assert.deepEqual(JSON.parse(requests[0].options.body).expected_author_fields, source)
    await assert.rejects(api.updateChapter('novel-a', 'chapter-a', { content: '作者明确手写', revision_source: 'manual_save' }))
    assert.equal('expected_author_fields' in JSON.parse(requests[1].options.body), false)
  } finally { globalThis.fetch = originalFetch }
})
