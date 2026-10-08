import assert from 'node:assert/strict'
import { createHash, webcrypto } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { cardRuntime, chapter, deferred, nodes, settle } from './chapter-generation-fixtures.mjs'

const moduleURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
let harness = (await readFile(new URL('./manual-writing.test.mjs', import.meta.url), 'utf8')).split('\ntest(')[0]
for (const name of ['vue', '@vue/compiler-sfc', 'typescript']) harness = harness.replaceAll(`from '${name}'`, `from '${import.meta.resolve(name)}'`)
harness = harness.replace("from './chapter-fixtures.mjs'", `from '${new URL('./chapter-fixtures.mjs', import.meta.url).href}'`)
  .replace('globalThis.__manualWriting = harness', 'harness.NModal = { name: "NModal" }; globalThis.__manualWriting = harness')
  .replaceAll('import.meta.url', JSON.stringify(new URL('./manual-writing.test.mjs', import.meta.url).href))
  .replaceAll("import.meta.resolve('vue')", JSON.stringify(import.meta.resolve('vue')))
const { writing } = await import(moduleURL(harness + '\nexport { writing }'))
const sha = text => createHash('sha256').update(text, 'utf8').digest('hex')
const metadata = (row, patch = {}) => ({ status: 'completed', novel_id: row.novel_id, chapter_id: row.id,
  subject: 'saved_chapter', content_sha256: sha(row.content), offset_unit: 'unicode_codepoint', conflicts: [], warnings: [], ...patch })
const mark = { rule: 'foreshadowing', message: '已消费伏笔再次出现', start: 1, end: 3, entity_id: '伏笔源' }
const textOf = node => typeof node === 'string' ? node : Array.isArray(node) ? node.map(textOf).join('') : typeof node?.children === 'string' ? node.children
  : Array.isArray(node?.children) ? node.children.map(textOf).join('')
    : node?.children && typeof node.children === 'object' ? Object.values(node.children).filter(value => typeof value === 'function').map(fn => textOf(fn())).join('') : ''
function allNodes(node, predicate, found = []) {
  if (Array.isArray(node)) { for (const child of node) allNodes(child, predicate, found); return found }
  if (!node || typeof node !== 'object') return found
  if (predicate(node)) found.push(node)
  if (Array.isArray(node.children)) for (const child of node.children) allNodes(child, predicate, found)
  else if (node.children && typeof node.children === 'object') for (const slot of Object.values(node.children)) if (typeof slot === 'function') allNodes(slot(), predicate, found)
  return found
}
const statusNode = (context, label) => allNodes(context.render(), node => node.props?.['aria-label'] === label)[0]
async function heldSHA(scenario) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto'), entered = deferred(), gate = deferred()
  const inputs = []
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { subtle: { digest: async (algorithm, bytes) => {
    inputs.push(new TextDecoder().decode(bytes)); entered.resolve(); await gate.promise
    return webcrypto.subtle.digest(algorithm, bytes)
  } } } })
  try { await scenario({ entered: entered.promise, release: () => gate.resolve(), inputs }) }
  finally { gate.resolve(); if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor); else delete globalThis.crypto }
}

test('生成实际done的完整正文规则结果可见，已知保存与规则冲突分别展示', async () => {
  const saved = chapter('novel-a', { content: '😀伏笔完整稿\r\n\0' })
  const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
    h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
  } })
  await context.vm.doWrite()
  assert.equal(context.vm.content.value, saved.content); assert.equal(context.vm.isDirty.value, false)
  const result = statusNode(context, '生成草稿规则检查')
  assert.ok(result); assert.match(textOf(result), /规则检查完成/); assert.match(textOf(result), /仍需作者校对/)
  assert.match(textOf(result), /已消费伏笔再次出现/); assert.equal(context.vm.streamError.value, '')
  context.dispose()
})

test('卡片正常保存自动关闭后Bubble仍能查看规则结果和重开完整记录', async () => {
  const saved = chapter('novel-a', { content: '😀伏笔保存稿', directive: '作者构思' })
  const runtime = await cardRuntime({ api: { writeMessageChapter: async (_cid, _mid, _payload, h) => {
    h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
  } } })
  const parent = await runtime.mountBubble(); parent.vm.showWriting.value = true
  const context = await runtime.mount({}, (event, ...args) => { if (event === 'applied') parent.vm.onChapterApplied(...args) })
  await context.vm.write(); await settle()
  assert.equal(parent.vm.showWriting.value, false); assert.equal(context.vm.candidateStatus.value, 'saved')
  const result = statusNode(parent, '聊天写章规则检查')
  assert.ok(result); assert.match(textOf(result), /已消费伏笔再次出现/); assert.match(textOf(result), /仍需作者校对/)
  assert.ok(statusNode(parent, '写章构思与候选'))
  context.stop(); parent.stop()
})

test('码点冲突和俗套范围在实际segments按emoji完整边界渲染，越界不制造span', async () => {
  const text = '😀伏笔，仿佛。'
  const context = await writing({ api: {
    checkConsistency: async () => ({ conflicts: [mark, { ...mark, start: -1, end: 2 }, { ...mark, start: 40, end: 41 }] }),
    scanCliche: async () => ({ hits: [{ term: '仿佛', start: 4, end: 6 }], total: 1, density: 1, by_category: {} }),
  } })
  context.vm.content.value = text; await context.vm.doConsistency(); await context.vm.doScanCliche()
  assert.deepEqual(context.vm.segments.value.filter(row => row.kind === 'conflict').map(row => row.text), ['伏笔'])
  assert.deepEqual(context.vm.segments.value.filter(row => row.kind === 'cliche').map(row => row.text), ['仿佛'])
  assert.equal(context.vm.segments.value.map(row => row.text).join(''), text)
  context.dispose()
})

test('损坏或缺失metadata不否认真实保存，未知规则和不完整检查不能伪装零冲突通过', async () => {
  const saved = chapter('novel-a', { content: '😀伏笔\0\r\n精确正文' })
  const valid = metadata(saved)
  for (const raw of [undefined, null, {}, { ...valid, status: 'other' }, { ...valid, novel_id: 'novel-b' },
    { ...valid, chapter_id: 'other' }, { ...valid, subject: 'preview_candidate' }, { ...valid, subject: 'existing_action' },
    { ...valid, content_sha256: sha('另一正文') }, { ...valid, content_sha256: valid.content_sha256.toUpperCase() },
    { ...valid, offset_unit: 'utf16' }, { ...valid, conflicts: undefined }, { ...valid, warnings: 'bad' },
    ...[{ ...mark, rule: 'future_rule' }, { ...mark, entity_id: '' }, { ...mark, start: true },
      { ...mark, end: 2.5 }, { ...mark, end: 999 }].map(conflict => ({ ...valid, conflicts: [conflict] })),
    { ...valid, status: 'failed', conflicts: [mark] }, { ...valid, status: 'skipped' }]) {
    const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: raw })
    } })
    try {
      await context.vm.doWrite()
      assert.equal(context.vm.selectedId.value, saved.id); assert.equal(context.vm.content.value, saved.content)
      assert.equal(context.vm.isDirty.value, false); assert.equal(context.vm.streamError.value, '')
      assert.equal(context.vm.generationRuleCheck.value.status, 'unconfirmed')
      assert.match(textOf(statusNode(context, '生成草稿规则检查')), /尚未确认/)
      assert.equal(context.vm.conflicts.value.length, 0)
    } finally { context.dispose() }
  }
})

test('明确规则失败和完成空结果都有限提示仍需校对，实际保存不重试且不自动深检', async () => {
  for (const status of ['failed', 'completed']) {
    const saved = chapter('novel-a', { content: '普通完整正文😀\0\r\n' })
    const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { status, warnings: status === 'failed' ? ['规则服务未完成'] : [] }) })
    } })
    try {
      await context.vm.doWrite()
      assert.equal(context.vm.generationRuleCheck.value.status, status)
      assert.equal(context.vm.streamError.value, ''); assert.equal(context.vm.isDirty.value, false)
      const result = textOf(statusNode(context, '生成草稿规则检查'))
      assert.match(result, status === 'failed' ? /规则检查未完成/ : /未发现已列规则冲突/)
      assert.match(result, /仍需作者校对/)
      assert.equal(context.requests.some(row => /check|scan|update|create/i.test(row.name)), false)
    } finally { context.dispose() }
  }
})

test('真实WebCrypto等待期间正文新稿或自然改回使旧检查失效，保存基线和后来作者稿保持', async () => {
  for (const editback of [false, true]) await heldSHA(async ({ entered, release, inputs }) => {
    const saved = chapter('novel-a', { content: '😀伏笔已保存稿\0\r\n' })
    const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
    } })
    try {
      const pending = context.vm.doWrite(); await entered
      assert.equal(context.vm.savedSnapshot.value.content, saved.content)
      assert.equal(context.vm.generationRuleCheck.value.status, 'verifying')
      context.vm.content.value = '作者新稿😀\0'; if (editback) context.vm.content.value = saved.content
      release(); await pending
      assert.deepEqual(inputs, [saved.content])
      assert.equal(context.vm.content.value, editback ? saved.content : '作者新稿😀\0')
      assert.equal(context.vm.generationRuleCheck.value, null); assert.deepEqual(context.vm.conflicts.value, [])
      assert.equal(context.vm.savedSnapshot.value.content, saved.content); assert.equal(context.vm.streamError.value, '')
    } finally { context.dispose() }
  })
})

test('真实SHA等待中的作品ABA、选章ABA与卸载旧done不显示提示或清下一轮输出', async () => {
  for (const change of ['novel', 'chapter', 'unmount']) await heldSHA(async ({ entered, release }) => {
    const saved = chapter('novel-a', { content: '😀伏笔已保存稿' })
    const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
    } })
    let disposed = false
    try {
      const pending = context.vm.doWrite(); await entered
      const notices = context.messages.length
      if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle() }
      if (change === 'chapter') { await context.vm.startNew(true); await context.vm.selectChapter(saved, true) }
      if (change === 'unmount') { context.dispose(); disposed = true }
      context.vm.content.value = '当前源作者唯一稿'; context.vm.streamText.value = '下一轮输出原样保留'
      release(); await pending
      assert.equal(context.vm.generationRuleCheck.value, null); assert.equal(context.vm.content.value, '当前源作者唯一稿')
      assert.equal(context.vm.streamText.value, '下一轮输出原样保留'); assert.equal(context.messages.length, notices)
    } finally { if (!disposed) context.dispose() }
  })
})

test('Card实际SHA等待中改构思或改回不自动关闭，完整检查只绑定实际保存章', async () => {
  for (const editback of [false, true]) await heldSHA(async ({ entered, release, inputs }) => {
    const saved = chapter('novel-a', { content: '😀伏笔实际保存章', directive: '作者构思' })
    const runtime = await cardRuntime({ api: { writeMessageChapter: async (_cid, _mid, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
    } } })
    const context = await runtime.mount()
    try {
      const pending = context.vm.write(); await entered
      context.vm.directive.value = '后来构思'; if (editback) context.vm.directive.value = '作者构思'
      release(); await pending
      assert.equal(context.vm.unsavedEdits.value, true); assert.equal(context.events.length, 0)
      assert.equal(context.vm.state.value.ruleCheck.status, 'completed'); assert.deepEqual(inputs, [saved.content])
      assert.equal(context.vm.directive.value, editback ? '作者构思' : '后来构思')
      assert.match(textOf(statusNode(context, '聊天写章完整规则检查')), /已消费伏笔再次出现/)
    } finally { context.stop() }
  })
})

test('Card旧A实际SHA重挂只推进原共享记录，B独立且原实例不提示或emit', async () => {
  await heldSHA(async ({ entered, release }) => {
    const saved = chapter('novel-a', { content: '😀伏笔保存章', directive: '作者构思' })
    const runtime = await cardRuntime({ api: { writeMessageChapter: async (_cid, _mid, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
    } } })
    const old = await runtime.mount(); const pending = old.vm.write(); await entered
    old.vm.directive.value = '原源后来稿'; old.stop()
    const fresh = await runtime.mount(), foreign = await runtime.mountBubble({ novelId: 'novel-b', message: {
      id: 'message-b', conversation_id: 'conversation-b', role: 'assistant', content: 'B', meta: null, created_at: '' } })
    try {
      await fresh.vm.write(); assert.equal(runtime.calls.filter(row => row.name === 'writeMessageChapter').length, 1)
      release(); await pending
      assert.equal(fresh.vm.directive.value, '原源后来稿'); assert.equal(fresh.vm.state.value.ruleCheck.status, 'completed')
      assert.equal(old.events.length, 0); assert.equal(fresh.events.length, 0); assert.equal(fresh.notices.length, 0)
      assert.equal(statusNode(foreign, '聊天写章规则检查'), undefined)
    } finally { fresh.stop(); foreign.stop() }
  })
})

test('early已有动作skipped检查不把本次不同token或后来构思当已保存正文', async () => {
  const saved = chapter('novel-a', { content: '已有章实际正文😀', directive: '作者构思' })
  const runtime = await cardRuntime({ api: { writeMessageChapter: async (_cid, _mid, _payload, h) => {
    h.onToken('本次不同候选'); await h.onDone({ chapter: saved, consistency: metadata(saved, { status: 'skipped', subject: 'existing_action' }) })
  } } })
  const context = await runtime.mount()
  try {
    await context.vm.write()
    assert.equal(context.vm.candidateDiffers.value, true); assert.equal(context.events.length, 0)
    assert.equal(context.vm.state.value.ruleCheck.status, 'skipped'); assert.equal(context.vm.state.value.ruleCheck.content, saved.content)
    const result = statusNode(context, '聊天写章完整规则检查')
    assert.match(textOf(result), /本次未重新进行规则检查/); assert.match(textOf(result), /仍需作者校对/)
    assert.equal(nodes(context.render(), row => row.props?.['aria-label'] === '规则检查对应的已保存正文')[0].props.value, saved.content)
  } finally { context.stop() }
})

test('改写候选全文检查只在预览展示，原文与原始码点选区不改且不自动apply', async () => {
  const saved = chapter('novel-a', { content: '原稿😀选段。余文\0', directive: '原指令' })
  const replacement = '候选😀伏笔'; let submitted
  const context = await writing({ props: { initialChapterId: saved.id }, api: {
    listChapterMetadata: async () => [saved], getChapter: async () => saved,
  }, streamSSE: async (_path, payload, h) => {
    submitted = payload; const points = Array.from(saved.content)
    const candidate = points.slice(0, payload.selection_start).join('') + replacement + points.slice(payload.selection_end).join('')
    const start = Array.from(candidate).indexOf('伏')
    h.onToken(replacement); await h.onDone({ type: 'done', preview: true, chapter: saved, content: candidate,
      expected_author_fields: { title: saved.title, directive: saved.directive, content: saved.content },
      selection_start: payload.selection_start, selection_end: payload.selection_end,
      consistency: metadata({ ...saved, content: candidate }, { subject: 'preview_candidate', conflicts: [{ ...mark, start, end: start + 2 }] }) })
  } })
  try {
    context.vm.contentRef.value = { selectionStart: saved.content.indexOf('选段'), selectionEnd: saved.content.indexOf('选段') + 2 }
    await context.vm.doRewrite()
    assert.equal(submitted.selection_start, Array.from(saved.content.slice(0, saved.content.indexOf('选段'))).length)
    assert.equal(context.vm.previewRuleCheck.value.status, 'completed'); assert.equal(context.vm.previewGuarded.value, true)
    assert.match(textOf(statusNode(context, '改写候选规则检查')), /候选尚未应用/)
    assert.match(textOf(statusNode(context, '改写候选规则检查')), /已消费伏笔再次出现/)
    assert.equal(context.vm.content.value, saved.content); assert.equal(context.vm.isDirty.value, false)
    assert.equal(context.vm.generationRuleCheck.value, null); assert.deepEqual(context.vm.conflicts.value, [])
    assert.equal(context.requests.some(row => row.name === 'updateChapter'), false)
  } finally { context.dispose() }
})

test('续写done先确认保存和规则结果，后置目录失败不清结果或保存基线', async () => {
  const original = chapter('novel-a', { content: '原正文😀', directive: '续写指令' })
  const saved = { ...original, content: original.content + '伏笔续写稿\0' }; let reads = 0
  const start = Array.from(original.content).length
  const context = await writing({ props: { initialChapterId: original.id }, api: {
    listChapterMetadata: async () => { if (++reads > 1) throw new Error('目录后置读取失败'); return [original] }, getChapter: async () => original,
  }, streamSSE: async (_path, _payload, h) => {
    h.onToken('伏笔续写稿\0'); await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [{ ...mark, start, end: start + 2 }] }) })
  } })
  try {
    await context.vm.doContinue()
    assert.equal(context.vm.savedSnapshot.value.content, saved.content); assert.equal(context.vm.content.value, saved.content)
    assert.equal(context.vm.generationRuleCheck.value.status, 'completed'); assert.equal(context.vm.streamError.value, '')
    assert.ok(context.messages.some(row => /续写已保存.*列表刷新失败/.test(row.text)))
    assert.deepEqual(context.vm.segments.value.filter(row => row.kind === 'conflict').map(row => row.text), ['伏笔'])
  } finally { context.dispose() }
})

test('实际digest故障只令检查范围未确认，done已保存正文不变为unknown或错误', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { subtle: { digest: async () => { throw new Error('浏览器摘要不可用') } } } })
  const saved = chapter('novel-a', { content: 'SHA故障也已保存😀\0' })
  const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
    h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: metadata(saved) })
  } })
  try {
    await context.vm.doWrite()
    assert.equal(context.vm.generationRuleCheck.value.status, 'unconfirmed'); assert.equal(context.vm.streamError.value, '')
    assert.equal(context.vm.selectedId.value, saved.id); assert.equal(context.vm.isDirty.value, false)
    assert.equal(context.vm.savedSnapshot.value.content, saved.content)
  } finally { context.dispose(); if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor); else delete globalThis.crypto }
})

test('预览实际SHA等待中的自然改回不让旧候选检查重新可应用或标记原稿', async () => {
  await heldSHA(async ({ entered, release }) => {
    const saved = chapter('novel-a', { content: '😀原选段。尾文', directive: '原指令' })
    const context = await writing({ props: { initialChapterId: saved.id }, api: { listChapterMetadata: async () => [saved], getChapter: async () => saved },
      streamSSE: async (_path, payload, h) => {
        const points = Array.from(saved.content), replacement = '新候选'
        const candidate = points.slice(0, payload.selection_start).join('') + replacement + points.slice(payload.selection_end).join('')
        h.onToken(replacement); await h.onDone({ type: 'done', preview: true, chapter: saved, content: candidate,
          expected_author_fields: { title: saved.title, directive: saved.directive, content: saved.content }, selection_start: payload.selection_start, selection_end: payload.selection_end,
          consistency: metadata({ ...saved, content: candidate }, { subject: 'preview_candidate' }) })
      } })
    try {
      const start = saved.content.indexOf('原选段'); context.vm.contentRef.value = { selectionStart: start, selectionEnd: start + 3 }
      const pending = context.vm.doRewrite(); await entered
      context.vm.content.value = '自然作者新稿'; context.vm.content.value = saved.content
      release(); await pending
      assert.equal(context.vm.previewRuleCheck.value, null); assert.equal(context.vm.previewGuarded.value, false)
      assert.equal(context.vm.content.value, saved.content); assert.deepEqual(context.vm.conflicts.value, [])
      await context.vm.applyPreview(); assert.equal(context.requests.some(row => row.name === 'updateChapter'), false)
    } finally { context.dispose() }
  })
})

test('SHA前冻结metadata与精确全文，等待中外部对象修改不能偷偷改变检查提示', async () => {
  await heldSHA(async ({ entered, release, inputs }) => {
    const saved = chapter('novel-a', { content: '😀伏笔精确正文\0' }), raw = metadata(saved, { conflicts: [mark], warnings: ['原告警'] })
    const context = await writing({ api: { listChapterMetadata: async () => [] }, streamSSE: async (_path, _payload, h) => {
      h.onToken(saved.content); await h.onDone({ chapter: saved, consistency: raw })
    } })
    try {
      const pending = context.vm.doWrite(); await entered
      raw.conflicts = [{ ...mark, message: '修改后的假提示' }]; raw.warnings[0] = '后来对象告警'; raw.content_sha256 = sha('另外文本')
      release(); await pending
      assert.deepEqual(inputs, [saved.content]); assert.equal(context.vm.generationRuleCheck.value.status, 'completed')
      assert.equal(context.vm.generationRuleCheck.value.conflicts[0].message, mark.message)
      assert.deepEqual(context.vm.generationRuleCheck.value.warnings, ['原告警'])
    } finally { context.dispose() }
  })
})

test('正常目录返回同字段的新metadata对象保留生成检查，真实outline变更和作者改回仍清旧检查', async () => {
  const saved = chapter('novel-a', { content: '😀伏笔正文', outline_node_id: 'outline-a' }); let directory = []
  const context = await writing({ api: { listChapterMetadata: async () => directory.map(row => ({ ...row })) },
    streamSSE: async (_path, _payload, h) => {
      directory = [saved]; h.onToken(saved.content)
      await h.onDone({ chapter: saved, consistency: metadata(saved, { conflicts: [mark] }) })
    } })
  try {
    await context.vm.doWrite()
    assert.equal(context.vm.content.value, saved.content); assert.equal(context.vm.isDirty.value, false)
    assert.equal(context.vm.generationRuleCheck.value?.status, 'completed')
    assert.deepEqual(context.vm.segments.value.filter(row => row.kind === 'conflict').map(row => row.text), ['伏笔'])
    await context.vm.loadChapters()
    assert.equal(context.vm.generationRuleCheck.value?.status, 'completed')
    directory = [{ ...saved, outline_node_id: 'outline-b' }]; await context.vm.loadChapters()
    assert.equal(context.vm.generationRuleCheck.value, null); assert.deepEqual(context.vm.conflicts.value, [])
    await context.vm.doWrite()
    assert.equal(context.vm.generationRuleCheck.value?.status, 'completed')
    context.vm.content.value = '自然临时作者稿'; context.vm.content.value = saved.content
    assert.equal(context.vm.generationRuleCheck.value, null)
  } finally { context.dispose() }
})
