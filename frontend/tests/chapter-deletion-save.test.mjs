import test from 'node:test'
import assert from 'node:assert/strict'
import { runtime, deferred, settle, author, chapter, vnodes } from './chapter-deletion-fixtures.mjs'

const openDelete = r => r.button('WritingPanel', '删除章节').props.onClick()
const copy = value => JSON.parse(JSON.stringify(value))

test('自身DELETE在真实App卸载重挂后仍按原章单飞，晚204同步missing而不清后来三raw', async () => {
  const entered = deferred(), gate = deferred()
  const r = await runtime({ beforeRequest: async request => { if (request.method === 'DELETE') { entered.resolve(); await gate.promise } } })
  let deleting
  try {
    await r.enter(); r.input('章节正文', '作者明确删除的原稿'); await settle(); openDelete(r); deleting = r.vm().deleteChapter(); await entered.promise; await settle()
    const old = r.find('WritingPanel'); await r.leave(); await r.enter(); assert.notEqual(r.find('WritingPanel'), old)
    r.input('章节标题', '重挂后来标题'); r.input('章前指令', '重挂后来指令'); r.input('章节正文', '重挂后来正文😀\0\r\n ')
    const later = author(r.vm()); await r.vm().saveContent()
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 0)
    const notices = r.messages.length; gate.resolve(); await deleting; await settle()
    assert.equal(r.vm().deletionState.exists, false); assert.equal(r.vm().deletionState.submission.outcome, 'confirmed')
    assert.deepEqual(author(r.vm()), later); assert.equal(r.messages.length, notices); assert.equal(r.rows.has('chapter-a'), false)
    await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 0)
    assert.equal(r.beforeUnload().prevented, true)
  } finally { gate.resolve(); if (deleting) await deleting; r.dispose() }
})

test('真实204丢回应保持unknown，明确GET404只是当前缺失且原删除稿和后来稿可复制', async () => {
  const r = await runtime({ afterResponse: async request => request.method === 'DELETE' ? { status: 503, body: { detail: '原删除回应丢失' } } : undefined })
  try {
    await r.enter(); const original = author(r.vm()); openDelete(r); await r.vm().deleteChapter()
    assert.equal(r.vm().deletionState.submission.outcome, 'unknown'); r.input('章节正文', '删除未知后的后来稿😀\0\r\n ')
    await r.vm().checkChapterDeletion(); assert.equal(r.vm().deletionState.exists, false)
    assert.equal(r.vm().deletionState.submission.outcome, 'unknown'); r.vm().releaseChapterDeletion(); await r.vm().saveContent()
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 0); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 1)
    await r.button('WritingPanel', '复制删除原稿').props.onClick(); assert.deepEqual(JSON.parse(r.clipboard.at(-1)), original)
    assert.equal(r.beforeUnload().prevented, true)
  } finally { r.dispose() }
})

test('作者明确known204处置删除时dirty的manual稿，复制仍保留且无后来稿不永久unload提醒', async () => {
  const r = await runtime()
  try {
    await r.enter(); r.input('章节正文', '曾明确手工保存'); await r.vm().saveContent()
    r.input('章节正文', '永久删除时明确处置的未存稿😀\0\r\n '); const disposed = author(r.vm())
    const state = r.vm().manualState, originalSubmission = copy(state.submission)
    openDelete(r); await r.vm().deleteChapter(); await settle()
    assert.equal(state.exists, false); assert.deepEqual(copy(state.submission), originalSubmission); assert.deepEqual(copy(state.draft), disposed)
    assert.equal(r.beforeUnload().prevented, false)
    await r.button('WritingPanel', '复制其他章节删除原稿').props.onClick(); assert.deepEqual(JSON.parse(r.clipboard.at(-1)), disposed)
  } finally { r.dispose() }
})

test('目录与detail只读不分配删除或手工意图，打开确认后取消零DELETE且无unload假提醒', async () => {
  const r = await runtime()
  try {
    await r.enter(); await r.vm().loadChapters(); await r.vm().selectChapter({ id: 'chapter-a', novel_id: 'novel-a' }, true)
    const deletion = await r.helper('chapterDeletionDraft'), manual = await r.helper('manualChapterDraft')
    assert.equal(deletion.chapterDeletionDrafts().length, 0); assert.equal(manual.manualChapterDrafts().length, 0)
    openDelete(r); await settle(); assert.equal(r.vm().showDelete, true); assert.equal(deletion.chapterDeletionDrafts().length, 0)
    r.button('WritingPanel', '取消').props.onClick(); await settle(); await r.vm().deleteChapter()
    assert.equal(r.trace.filter(row => row.method !== 'GET').length, 0); assert.equal(r.beforeUnload().prevented, false)
  } finally { r.dispose() }
})

test('最终删除确认保护作者自然editback及真实目标ABA，旧确认不发DELETE', async () => {
  for (const scenario of ['editback', 'chapter', 'novel']) {
    const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-a', { seq: 2, title: '第二章' })] })
    try {
      await r.enter(); const original = r.vm().content; openDelete(r)
      if (scenario === 'editback') { r.input('章节正文', '确认中暂改'); r.input('章节正文', original) }
      if (scenario === 'chapter') { await r.vm().selectChapter({ id: 'chapter-b', novel_id: 'novel-a' }, true); await r.vm().selectChapter({ id: 'chapter-a', novel_id: 'novel-a' }, true) }
      if (scenario === 'novel') { await r.switchNovel('novel-b'); await r.switchNovel('novel-a'); await r.vm().selectChapter({ id: 'chapter-a', novel_id: 'novel-a' }, true) }
      await r.vm().deleteChapter(); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 0)
      openDelete(r); await r.vm().deleteChapter(); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 1)
    } finally { r.dispose() }
  }
})

test('确认期间manual核对只推进事实版本而raw不变，也拒旧删除确认', async () => {
  const r = await runtime()
  try {
    await r.enter(); r.input('章节正文', '已知手工稿'); await r.vm().saveContent(); openDelete(r)
    const before = author(r.vm()), version = r.vm().manualState.version
    await r.vm().checkManualChapter(); assert(r.vm().manualState.version > version); assert.deepEqual(author(r.vm()), before)
    await r.vm().deleteChapter(); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 0)
    openDelete(r); await r.vm().deleteChapter(); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 1)
  } finally { r.dispose() }
})

test('等待删除的新三raw和自然editback保修订，known后转新章稿；回Chat仍保护后来稿', async () => {
  const entered = deferred(), gate = deferred(), r = await runtime({ beforeRequest: async request => { if (request.method === 'DELETE') { entered.resolve(); await gate.promise } } })
  let deleting
  try {
    await r.enter(); const original = author(r.vm()); openDelete(r); deleting = r.vm().deleteChapter(); await entered.promise
    r.input('章节标题', '暂改标题'); r.input('章前指令', '暂改指令'); r.input('章节正文', '暂改正文')
    r.input('章节标题', original.title); r.input('章前指令', original.directive); r.input('章节正文', original.content)
    const record = r.vm().deletionState; assert(record.revision > record.submission.revision)
    gate.resolve(); await deleting; assert.equal(r.vm().selectedId, null); assert.deepEqual(author(r.vm()), original)
    assert.equal(record.submission.outcome, 'confirmed'); assert.equal(r.trace.filter(row => row.method === 'POST').length, 0)
    await r.leave(); assert.equal(r.beforeUnload().prevented, true)
  } finally { gate.resolve(); if (deleting) await deleting; r.dispose() }
})

test('正常clean章known204保复制副本，Chat及Bclean不因明确处置原稿持续warn', async () => {
  const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-b')] })
  try {
    await r.enter(); const original = author(r.vm()); openDelete(r); await r.vm().deleteChapter()
    assert.equal(r.beforeUnload().prevented, false); await r.leave(false); assert.equal(r.beforeUnload().prevented, false)
    await r.enter('novel-b', 'chapter-b'); assert.equal(r.vm().isDirty, false); assert.equal(r.beforeUnload().prevented, false)
    await r.button('WritingPanel', '复制其他章节删除原稿').props.onClick(); assert.deepEqual(JSON.parse(r.clipboard.at(-1)), original)
  } finally { r.dispose() }
})

test('外部GET404不赋own disposition，已有manual原稿missing仍需完整页关闭提醒', async () => {
  const r = await runtime()
  try {
    await r.enter(); r.input('章节正文', '已有manual原提交'); await r.vm().saveContent(); const state = r.vm().manualState
    r.rows.delete('chapter-a'); await r.vm().checkManualChapter()
    assert.equal(state.exists, false); assert.equal(state.deletionDisposition, undefined); assert.equal(r.beforeUnload().prevented, true)
    assert.equal((await r.helper('chapterDeletionDraft')).chapterDeletionDrafts().length, 0)
  } finally { r.dispose() }
})

test('pending不同章合法手工存储独立，旧A204不清B三raw/选择或吐旧toast', async () => {
  const entered = deferred(), gate = deferred(), r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-a', { seq: 2 })], beforeRequest: async request => { if (request.method === 'DELETE') { entered.resolve(); await gate.promise } } })
  let deleting
  try {
    await r.enter(); openDelete(r); deleting = r.vm().deleteChapter(); await entered.promise
    await r.vm().selectChapter({ id: 'chapter-b', novel_id: 'novel-a' }, true); r.input('章节正文', 'B合法新存稿😀'); await r.vm().saveContent()
    const before = author(r.vm()), notices = r.messages.length; gate.resolve(); await deleting; await settle()
    assert.equal(r.vm().selectedId, 'chapter-b'); assert.deepEqual(author(r.vm()), before); assert.equal(r.vm().isDirty, false); assert.equal(r.messages.length, notices)
    assert.equal(r.rows.get('chapter-b').content, before.content); assert.equal(r.rows.has('chapter-a'), false); assert.equal(r.beforeUnload().prevented, false)
  } finally { gate.resolve(); if (deleting) await deleting; r.dispose() }
})

test('旧A删除finally不清同组件B新章create loading，B原UUID请求仍单飞', async () => {
  const oldEntered = deferred(), oldGate = deferred(), newEntered = deferred(), newGate = deferred()
  const r = await runtime({ beforeRequest: async request => {
    if (request.method === 'DELETE') { oldEntered.resolve(); await oldGate.promise }
    if (request.method === 'POST') { newEntered.resolve(); await newGate.promise; return { status: 201, body: { ...chapter('created-b', 'novel-b', request.payload), creation_replayed: false, author_changed_since_creation: false } } }
  } })
  let deleting, creating
  try {
    await r.enter(); openDelete(r); deleting = r.vm().deleteChapter(); await oldEntered.promise
    await r.switchNovel('novel-b'); r.input('章节标题', 'B明确新章'); r.input('章节正文', 'B新章原文'); creating = r.vm().saveContent(); await newEntered.promise
    oldGate.resolve(); await deleting; assert.equal(r.vm().saving, true); await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'POST').length, 1)
    newGate.resolve(); await creating; assert.equal(r.vm().selectedId, 'created-b'); assert.equal(r.vm().saving, false)
  } finally { oldGate.resolve(); newGate.resolve(); if (deleting) await deleting; if (creating) await creating; r.dispose() }
})

test('未知DELETE读取200不是回执，风险采用只基线0写；显式后续manual ACK同步完整事实且旧unknown保留', async () => {
  let rejectDelete = true
  const r = await runtime({ beforeRequest: async request => rejectDelete && request.method === 'DELETE' ? { status: 503, body: { detail: '删除未得到回应' } } : undefined })
  try {
    await r.enter(); const original = author(r.vm()); openDelete(r); await r.vm().deleteChapter()
    r.input('章节正文', '作者后来正文😀'); const later = author(r.vm())
    r.vm().releaseChapterDeletion(); await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 0)
    r.rows.set('chapter-a', { ...r.rows.get('chapter-a'), title: '另一合法标题', updated_at: 'external-fact' })
    await r.vm().checkChapterDeletion(); assert.equal(r.vm().deletionState.submission.outcome, 'unknown'); assert.equal(r.vm().deletionState.phase, 'unknown')
    const beforeWrites = r.trace.filter(row => row.method !== 'GET').length; r.vm().releaseChapterDeletion(); await settle()
    assert.equal(r.trace.filter(row => row.method !== 'GET').length, beforeWrites); assert.deepEqual(author(r.vm()), later); assert.equal(r.vm().savedSnapshot.title, '另一合法标题')
    await r.vm().saveContent(); assert.equal(r.vm().isDirty, false); assert.deepEqual(copy(r.vm().manualState.saved), r.rows.get('chapter-a'))
    assert.equal(r.vm().deletionState.fact.content, later.content); assert.equal(r.vm().deletionState.baseline.content, later.content)
    assert.deepEqual(copy(r.vm().deletionState.submission.author), original); assert.equal(r.beforeUnload().prevented, true)
  } finally { r.dispose() }
})

test('未知删除heldGET与自然editback使采用资格失效，失败的新核对不得沿旧资料解除门禁', async () => {
  const entered = deferred(), gate = deferred(); let holding = false, failRead = false
  const r = await runtime({ beforeRequest: async request => {
    if (request.method === 'DELETE') return { status: 503, body: { detail: '未知删除' } }
    if (holding && request.method === 'GET' && request.path.endsWith('/chapter-a')) { entered.resolve(); await gate.promise }
    if (failRead && request.method === 'GET' && request.path.endsWith('/chapter-a')) return { status: 503, body: { detail: '核对失败' } }
  } })
  let checking
  try {
    await r.enter(); openDelete(r); await r.vm().deleteChapter(); const raw = r.vm().content
    holding = true; checking = r.vm().checkChapterDeletion(); await entered.promise; r.input('章节正文', '暂改'); r.input('章节正文', raw)
    gate.resolve(); await checking; assert.equal(r.vm().deletionState.observed, null); r.vm().releaseChapterDeletion(); assert.equal(r.vm().manualBlocked, true)
    holding = false; await r.vm().checkChapterDeletion(); assert(r.vm().deletionState.observed)
    failRead = true; await r.vm().checkChapterDeletion(); assert.equal(r.vm().deletionState.observed, null); r.vm().releaseChapterDeletion(); assert.equal(r.vm().manualBlocked, true)
  } finally { gate.resolve(); if (checking) await checking; r.dispose() }
})

test('pending时旧完整GET晚于真实204不得恢复exists/observed或取得采用资格', async () => {
  const deleteEntered = deferred(), deleteGate = deferred(), getEntered = deferred(), getGate = deferred(); let holdingRead = false
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (request.method === 'DELETE') { deleteEntered.resolve(); await deleteGate.promise }
    if (holdingRead && request.method === 'GET' && request.path.endsWith('/chapter-a')) { const body = copy(rows.get('chapter-a')); getEntered.resolve(); await getGate.promise; return { body } }
  } })
  let deleting, checking
  try {
    await r.enter(); openDelete(r); deleting = r.vm().deleteChapter(); await deleteEntered.promise; const state = r.vm().deletionState
    holdingRead = true; checking = r.vm().checkChapterDeletion(); await getEntered.promise
    deleteGate.resolve(); await deleting; getGate.resolve(); await checking
    assert.equal(state.exists, false); assert.equal(state.submission.outcome, 'confirmed'); assert.equal(state.observed, null)
    assert.equal(r.beforeUnload().prevented, false)
  } finally { deleteGate.resolve(); getGate.resolve(); if (deleting) await deleting; if (checking) await checking; r.dispose() }
})

test('未知风险采用之后重新明确删除为新意图，旧unknown只读保留且不因known204失去关闭提醒', async () => {
  let first = true
  const r = await runtime({ beforeRequest: async request => request.method === 'DELETE' && first ? (first = false, { status: 503, body: { detail: '第一次删除未知' } }) : undefined })
  try {
    await r.enter(); openDelete(r); await r.vm().deleteChapter(); const state = r.vm().deletionState, firstOperation = state.submission.operation
    await r.vm().checkChapterDeletion(); r.vm().releaseChapterDeletion(); openDelete(r); await r.vm().deleteChapter()
    assert.equal(state.submission.outcome, 'confirmed'); assert.notEqual(state.submission.operation, firstOperation); assert.equal(state.retained.length, 1)
    assert.equal(state.retained[0].outcome, 'unknown'); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 2); assert.equal(r.beforeUnload().prevented, true)
  } finally { r.dispose() }
})

test('删除原/后来/其他源精确复制及native readonly拒绝fallback保CRLF/emoji/NUL/空字段', async () => {
  const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-b')], beforeRequest: async request => request.method === 'DELETE' ? { status: 503, body: { detail: '未知' } } : undefined })
  try {
    await r.enter(); r.input('章节标题', ''); r.input('章前指令', ' 原😀\0\r\n '); r.input('章节正文', ' 原稿😀\0\r\n '); const original = author(r.vm())
    openDelete(r); await r.vm().deleteChapter(); r.input('章节正文', ' 后来😀\0\r\n '); const later = author(r.vm())
    r.clipboardReject(); await r.vm().copyChapterDeletion(r.vm().deletionState, true); await settle()
    let inputs = vnodes(r.find('WritingPanel').subTree, node => node.type?.name === 'NInput' && node.props?.['input-props']?.['aria-label'] === '手动复制删除原稿')
    assert.equal(inputs.length, 1); assert.equal('readonly' in inputs[0].props, true); assert.deepEqual(JSON.parse(inputs[0].props.value), original)
    r.vm().deletionCopyFallback = ''; await r.switchNovel('novel-b'); await r.vm().selectChapter({ id: 'chapter-b', novel_id: 'novel-b' }, true)
    await r.button('WritingPanel', '复制其他章节删除期间当前稿').props.onClick(); await settle()
    inputs = vnodes(r.find('WritingPanel').subTree, node => node.type?.name === 'NInput' && node.props?.['input-props']?.['aria-label'] === '手动复制删除期间当前稿')
    assert.equal(inputs.length, 1); assert.deepEqual(JSON.parse(inputs[0].props.value), later); assert.equal(r.vm().isDirty, false); assert.equal(r.beforeUnload().prevented, true)
  } finally { r.dispose() }
})

test('删除pending/unknown同章所有可调用写handlers门禁，预览读取/不同目标合同不扩', async () => {
  const entered = deferred(), gate = deferred(), r = await runtime({ beforeRequest: async request => { if (request.method === 'DELETE') { entered.resolve(); await gate.promise; return { status: 503, body: { detail: '未知' } } } } })
  let deleting
  try {
    await r.enter(); openDelete(r); deleting = r.vm().deleteChapter(); await entered.promise
    for (const method of ['saveContent', 'deleteChapter', 'doContinue', 'saveSummary', 'applyRepair', 'doSettle', 'confirmSettle', 'openRevisionHistory']) await r.vm()[method]()
    assert.equal(r.trace.filter(row => row.method !== 'GET').length, 1); assert.equal(r.vm().showRevisions, false)
    gate.resolve(); await deleting
    for (const method of ['saveContent', 'deleteChapter', 'doContinue', 'saveSummary', 'applyRepair', 'doSettle', 'confirmSettle', 'openRevisionHistory']) await r.vm()[method]()
    assert.equal(r.trace.filter(row => row.method !== 'GET').length, 1); assert.equal(r.vm().showRevisions, false)
  } finally { gate.resolve(); if (deleting) await deleting; r.dispose() }
})

test('损坏当前ChapterRead/错误源身份核对不产生风险采用资格或误解锁', async () => {
  for (const damage of [row => ({ ...row, id: 'wrong' }), row => ({ ...row, novel_id: 'wrong' }), row => ({ ...row, seq: 1.5 }), row => ({ ...row, summary: null }), row => ({ ...row, updated_at: null })]) {
    let damaged = false
    const r = await runtime({ beforeRequest: async request => request.method === 'DELETE' ? { status: 503, body: { detail: '未知' } } : undefined,
      afterResponse: async (request, body) => damaged && request.method === 'GET' && request.path.endsWith('/chapter-a') ? { body: damage(body) } : undefined })
    try {
      await r.enter(); openDelete(r); await r.vm().deleteChapter(); damaged = true; await r.vm().checkChapterDeletion(); r.vm().releaseChapterDeletion()
      assert.equal(r.vm().deletionState.observed, null); assert.equal(r.vm().manualBlocked, true); assert.equal(r.vm().deletionState.submission.outcome, 'unknown')
    } finally { r.dispose() }
  }
})

test('known204先记永久删除事实，后置目录失败或旧目录返回不能否认/复活，正常无后来稿quiet', async () => {
  let failList = false
  const r = await runtime({ beforeRequest: async request => failList && request.path.endsWith('/metadata') ? { status: 503, body: { detail: '目录读取失败' } } : undefined,
    afterResponse: async request => { if (request.method === 'DELETE') failList = true } })
  try {
    await r.enter(); openDelete(r); await r.vm().deleteChapter(); const helper = await r.helper('chapterDeletionDraft'), state = helper.findChapterDeletionDraft('novel-a', 'chapter-a')
    assert.equal(state.submission.outcome, 'confirmed'); assert.equal(state.exists, false); assert.equal(r.beforeUnload().prevented, false)
    assert(r.messages.some(row => /已删除.*列表刷新失败/.test(row.text))); assert.equal(r.trace.filter(row => row.method === 'DELETE').length, 1)
  } finally { r.dispose() }
})

test('manual原unknown明确采用后再knownDELETE仍保unknown警告，不把删除当保存回执', async () => {
  const r = await runtime({ afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '保存未知' } } : undefined })
  try {
    await r.enter(); r.input('章节正文', '原manual未知稿'); await r.vm().saveContent(); const manual = r.vm().manualState
    await r.vm().checkManualChapter(); r.vm().adoptCurrentManualChapter(); assert.equal(manual.submission.outcome, 'unknown')
    openDelete(r); await r.vm().deleteChapter(); assert.equal(manual.exists, false); assert.equal(manual.submission.outcome, 'unknown')
    assert.equal(r.beforeUnload().prevented, true); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
  } finally { r.dispose() }
})

test('风险采用后的manual真实ACK仅推进事实也使held旧GET资格失效，共享基线不回退', async () => {
  const putEntered = deferred(), putGate = deferred(), getEntered = deferred(), getGate = deferred(); let holdingRead = false
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (request.method === 'DELETE') return { status: 503, body: { detail: '原删除未知' } }
    if (request.method === 'PUT') { putEntered.resolve(); await putGate.promise }
    if (holdingRead && request.method === 'GET' && request.path.endsWith('/chapter-a')) { const body = copy(rows.get('chapter-a')); getEntered.resolve(); await getGate.promise; return { body } }
  } })
  let saving, checking
  try {
    await r.enter(); openDelete(r); await r.vm().deleteChapter(); await r.vm().checkChapterDeletion(); r.vm().releaseChapterDeletion()
    r.input('章节正文', '新明确手工提交原文😀'); saving = r.vm().saveContent(); await putEntered.promise
    holdingRead = true; checking = r.vm().checkChapterDeletion(); await getEntered.promise
    putGate.resolve(); await saving; getGate.resolve(); await checking
    assert.equal(r.vm().deletionState.observed, null); assert.equal(r.vm().deletionState.baseline.content, '新明确手工提交原文😀')
    assert.equal(r.vm().deletionState.fact.content, '新明确手工提交原文😀'); assert.equal(r.vm().isDirty, false)
    await r.leave(); await r.enter(); assert.equal(r.vm().isDirty, false); assert.equal(r.vm().savedSnapshot.content, '新明确手工提交原文😀')
  } finally { putGate.resolve(); getGate.resolve(); if (saving) await saving; if (checking) await checking; r.dispose() }
})
