import assert from 'node:assert/strict'
import test from 'node:test'
import { runtime, chapter, deferred, author, settle, vnodes, label } from './manual-chapter-fixtures.mjs'

test('actualApp同章manual PUT等待跨真实离页重挂仍单飞，原提交及后来稿可见', async () => {
  const gate = deferred(), entered = deferred(); let first = true
  const r = await runtime({ beforeRequest: async request => { if (request.method === 'PUT' && first) { first = false; entered.resolve(); await gate.promise } } })
  let saving
  try {
    await r.enter(); r.input('章节正文', '原提交😀\0\r\n '); saving = r.vm().saveContent(); await entered.promise
    r.input('章节正文', '等待期间作者后来原文😀\0 '); const later = author(r.vm())
    await r.leave(); await r.enter()
    r.input('章节正文', '重挂后作者另一明确新稿😀\0 '); later.content = '重挂后作者另一明确新稿😀\0 '
    await r.vm().saveContent()
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
    assert.deepEqual(author(r.vm()), later)
    assert.ok(r.region('已有章节保存核对'))
    gate.resolve(); await saving; await settle()
    assert.equal(r.vm().manualState.phase, 'known'); assert.equal(r.vm().savedSnapshot.content, '原提交😀\0\r\n ')
    assert.deepEqual(author(r.vm()), later); assert.equal(r.vm().isDirty, true)
  } finally { gate.resolve(); if (saving) await saving; r.dispose() }
})

test('真实完整PUT已存而前端503为unknown，原提交与后来三稿独立且不盲第二PUT', async () => {
  const gate = deferred(), entered = deferred()
  const r = await runtime({ beforeRequest: async request => { if (request.method === 'PUT') { entered.resolve(); await gate.promise } }, afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '真实保存后受控连接故障' } } : undefined })
  let saving
  try {
    await r.enter(); r.input('章节标题', ' 原标题提交 '); r.input('章节正文', '原正文提交😀\0'); r.input('章前指令', ' 原指令提交 ')
    const submitted = author(r.vm()); saving = r.vm().saveContent(); await entered.promise
    r.input('章节标题', '后来标题'); r.input('章节正文', '后来正文'); r.input('章前指令', '后来指令'); const later = author(r.vm())
    gate.resolve(); await saving; await settle()
    assert.equal(r.vm().manualState?.phase, 'unknown'); assert.deepEqual({ ...r.vm().manualState.submission.author }, submitted)
    assert.deepEqual(author(r.vm()), later); assert.ok(r.region('已有章节保存核对'))
    await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
  } finally { gate.resolve(); if (saving) await saving; r.dispose() }
})

test('普通已有章缺13列ACK不能假clean；完整历史seq零负数与status字符串按现Read合同', async () => {
  const r = await runtime({ chapters: [chapter('chapter-a', 'novel-a', { seq: -1, status: 'legacy' })], afterResponse: async (request, body) => { if (request.method === 'PUT') { const damaged = { ...body }; delete damaged.created_at; return { body: damaged } } } })
  try {
    await r.enter(); assert.equal(r.vm().selectedChapter.seq, -1); assert.equal(r.vm().selectedChapter.status, 'legacy')
    r.input('章节正文', '明确新原稿😀'); await r.vm().saveContent(); await settle()
    assert.equal(r.vm().manualState?.phase, 'unknown'); assert.equal(r.vm().isDirty, true)
    assert.equal(r.rows.get('chapter-a').content, '明确新原稿😀')
  } finally { r.dispose() }
})

test('unknown必须明确成功GET后才能采用；当前资料不是回执，采用保raw且零自动PUT', async () => {
  let lost = true
  const r = await runtime({ afterResponse: async request => request.method === 'PUT' && lost ? (lost = false, { status: 503, body: { detail: '已保存但受控丢回应' } }) : undefined })
  try {
    await r.enter(); r.input('章节正文', '原提交'); await r.vm().saveContent()
    const original = { ...r.vm().manualState.submission.author }
    assert.equal(r.vm().manualState.observed, null); r.vm().adoptCurrentManualChapter(); assert.equal(r.vm().manualState.phase, 'unknown')
    r.input('章节标题', '后来标题'); r.input('章节正文', '后来正文'); r.input('章前指令', '后来指令'); const later = author(r.vm())
    Object.assign(r.rows.get('chapter-a'), { title: '另一处当前标题', directive: '另一处当前指令', content: '另一处当前正文' })
    await r.vm().checkManualChapter(); assert.equal(r.vm().manualState.phase, 'unknown')
    assert.deepEqual({ ...r.vm().manualState.submission.author }, original)
    r.vm().adoptCurrentManualChapter(); assert.deepEqual(author(r.vm()), later)
    assert.deepEqual({ ...r.vm().savedSnapshot }, { title: '另一处当前标题', directive: '另一处当前指令', content: '另一处当前正文' })
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
    assert.match(label(r.region('已有章节保存核对')), /旧请求仍可能迟到|旧未知请求仍可能迟到/)
    await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 2)
    assert.deepEqual(r.trace.filter(row => row.method === 'PUT')[1].payload, { ...later, revision_source: 'manual_save' })
    assert.deepEqual({ ...r.vm().manualState.retained[0].author }, original)
  } finally { r.dispose() }
})

test('明确核对GET等待期间自然editback拒绝旧当前资料，失败再读不能沿用先前采用资格', async () => {
  const entered = deferred(), gate = deferred(); let checking = false, failing = false
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (checking && request.method === 'GET' && request.path.endsWith('/chapters/chapter-a')) {
      const body = structuredClone(rows.get('chapter-a')); entered.resolve(); await gate.promise
      return failing ? { status: 503, body: { detail: '读取故障' } } : { body }
    }
  }, afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '丢回应' } } : undefined })
  let read
  try {
    await r.enter(); r.input('章节正文', '明确原提交'); await r.vm().saveContent(); checking = true
    read = r.vm().checkManualChapter(); await entered.promise
    r.input('章节正文', '临时后来稿'); r.input('章节正文', '明确原提交'); gate.resolve(); await read
    assert.equal(r.vm().manualState.observed, null); r.vm().adoptCurrentManualChapter(); assert.equal(r.vm().manualState.phase, 'unknown')
    checking = false; await r.vm().checkManualChapter(); assert(r.vm().manualState.observed)
    checking = true; failing = true; await r.vm().checkManualChapter(); assert.equal(r.vm().manualState.observed, null)
    r.vm().adoptCurrentManualChapter(); assert.equal(r.vm().manualState.phase, 'unknown'); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
  } finally { gate.resolve(); if (read) await read; r.dispose() }
})

test('unknown采用等值当前资料后仍全页提醒，离开Settings之外聊天仍能保护原未知提交', async () => {
  const r = await runtime({ afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '丢回应' } } : undefined })
  try {
    await r.enter(); r.input('章节正文', '提交后当前等值'); await r.vm().saveContent(); await r.vm().checkManualChapter(); r.vm().adoptCurrentManualChapter()
    assert.equal(r.vm().isDirty, false); assert.equal(r.vm().manualState.phase, 'ready'); assert.equal(r.beforeUnload().prevented, true)
    await settle(); r.vm('WorkbenchView').returnToChat(); await settle(); assert.equal(Boolean(r.find('WritingPanel')), false); assert.equal(r.beforeUnload().prevented, true)
  } finally { r.dispose() }
})

test('quiet clean记录重挂采用合法外部fresh13事实；无manual记录首次选中保持完整读取', async () => {
  const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-b', { seq: 0, status: '' })] })
  try {
    await r.enter(); r.input('章节正文', '作者已存'); await r.vm().saveContent()
    r.vm('WorkbenchView').returnToChat(); await settle()
    Object.assign(r.rows.get('chapter-a'), { title: '外部当前标题', directive: '外部当前指令', content: '外部当前正文', seq: -7, status: 'legacy' })
    await r.enter(); assert.deepEqual(author(r.vm()), { title: '外部当前标题', directive: '外部当前指令', content: '外部当前正文' }); assert.equal(r.vm().isDirty, false)
    assert.equal(r.vm().selectedChapter.seq, -7); assert.equal(r.vm().selectedChapter.status, 'legacy')
    await r.switchNovel('novel-b'); await r.vm().selectChapter({ id: 'chapter-b', novel_id: 'novel-b' }, true); assert.equal(r.vm().manualState, null); assert.equal(r.vm().content, r.rows.get('chapter-b').content)
    r.input('章节正文', 'seq零空status合法保存'); await r.vm().saveContent(); assert.equal(r.vm().manualState.phase, 'known')
  } finally { r.dispose() }
})

test('正常fresh详情等待作者自然editback不替换raw或baseline', async () => {
  let holding = false; const entered = deferred(), gate = deferred()
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (holding && request.method === 'GET' && request.path.endsWith('/chapters/chapter-a')) { const body = structuredClone(rows.get('chapter-a')); entered.resolve(); await gate.promise; return { body } }
  } })
  let selecting
  try {
    await r.enter(); r.input('章节正文', '原已存'); await r.vm().saveContent(); const baseline = author(r.vm())
    Object.assign(r.rows.get('chapter-a'), { title: '外部新标题', content: '外部新正文' }); holding = true
    selecting = r.vm().selectChapter({ id: 'chapter-a', novel_id: 'novel-a' }, true); await entered.promise
    r.input('章节正文', '作者暂改'); r.input('章节正文', '原已存'); gate.resolve(); await selecting
    assert.deepEqual(author(r.vm()), baseline); assert.deepEqual({ ...r.vm().savedSnapshot }, baseline)
  } finally { gate.resolve(); if (selecting) await selecting; r.dispose() }
})

test('不同章节保存独立，旧A回应不清B当前稿或吐旧成功提示；其他记录可实际复制', async () => {
  const entered = deferred(), gate = deferred()
  const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-a', { title: 'B', content: 'B正文' })], beforeRequest: async request => {
    if (request.method === 'PUT' && request.path.endsWith('/chapter-a')) { entered.resolve(); await gate.promise }
  } })
  let saving
  try {
    await r.enter(); r.input('章节正文', 'A原提交'); saving = r.vm().saveContent(); await entered.promise
    r.input('章节正文', 'A后来稿'); await r.leave(); await r.enter('novel-a', 'chapter-b')
    r.input('章节正文', 'B明确已存'); await r.vm().saveContent(); r.input('章节正文', 'B后来稿'); const before = author(r.vm()), notices = r.messages.length
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 2); assert.equal(r.beforeUnload().prevented, true)
    r.button('WritingPanel', '复制其他章节原提交').props.onClick(); await settle(); assert.equal(JSON.parse(r.clipboard.at(-1)).content, 'A原提交')
    r.button('WritingPanel', '复制其他章节当前稿').props.onClick(); await settle(); assert.equal(JSON.parse(r.clipboard.at(-1)).content, 'A后来稿')
    const copiedNotices = r.messages.length; assert(copiedNotices >= notices)
    gate.resolve(); await saving; await settle(); assert.equal(r.vm().selectedId, 'chapter-b'); assert.deepEqual(author(r.vm()), before); assert.equal(r.messages.length, copiedNotices)
  } finally { gate.resolve(); if (saving) await saving; r.dispose() }
})

test('旧A回包不能清同组件B新章create loading，原UUID创建仍单飞', async () => {
  const oldEntered = deferred(), oldGate = deferred(), newEntered = deferred(), newGate = deferred()
  const r = await runtime({ beforeRequest: async request => {
    if (request.method === 'PUT') { oldEntered.resolve(); await oldGate.promise }
    if (request.method === 'POST' && request.path.endsWith('/chapters')) { newEntered.resolve(); await newGate.promise; return { status: 201, body: { ...chapter('created-b', 'novel-b', request.payload), creation_replayed: false, author_changed_since_creation: false } } }
  } })
  let oldSaving, newSaving
  try {
    await r.enter(); r.input('章节正文', 'A提交'); oldSaving = r.vm().saveContent(); await oldEntered.promise
    const oldInstance = r.find('WritingPanel'); await r.switchNovel('novel-b'); assert.equal(r.find('WritingPanel') === oldInstance, true)
    r.input('章节标题', 'B新章'); r.input('章节正文', 'B新章原文'); newSaving = r.vm().saveContent(); await newEntered.promise
    oldGate.resolve(); await oldSaving; await settle(); assert.equal(r.vm().saving, true)
    await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'POST').length, 1)
    newGate.resolve(); await newSaving; assert.equal(r.vm().selectedId, 'created-b'); assert.equal(r.vm().saving, false)
  } finally { oldGate.resolve(); newGate.resolve(); if (oldSaving) await oldSaving; if (newSaving) await newSaving; r.dispose() }
})

test('原章节真实404变copy-only；原提交和后来raw的JSON复制及native readonly fallback保精确字符', async () => {
  const r = await runtime({ afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '丢回应' } } : undefined })
  try {
    await r.enter(); r.input('章节标题', ' 原标题\0 '); r.input('章前指令', '\r\n 原指令😀 '); r.input('章节正文', ' 原提交😀\0\r\n '); const original = author(r.vm())
    await r.vm().saveContent(); r.input('章节正文', ' 后来原文😀\0\r\n '); const current = author(r.vm()); r.rows.delete('chapter-a')
    await r.vm().checkManualChapter(); assert.equal(r.vm().manualState.phase, 'missing'); assert.equal(r.vm().manualState.exists, false)
    await r.vm().saveContent(); r.vm().adoptCurrentManualChapter(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
    await r.button('WritingPanel', '复制原提交章节稿').props.onClick(); assert.deepEqual(JSON.parse(r.clipboard.at(-1)), original)
    r.clipboardReject(); await r.button('WritingPanel', '复制当前章节稿').props.onClick(); await settle()
    const inputs = vnodes(r.find('WritingPanel').subTree, node => node.type?.name === 'NInput' && node.props?.['input-props']?.['aria-label'] === '手动复制当前章节稿')
    assert.equal(inputs.length, 1); assert.equal('readonly' in inputs[0].props, true); assert.deepEqual(JSON.parse(inputs[0].props.value), current)
    assert.equal(r.beforeUnload().prevented, true); assert.equal(r.trace.filter(row => row.method === 'POST').length, 0)
  } finally { r.dispose() }
})

test('已知完整保存先记事实，后置目录故障不能否认保存；三raw全量PUT保空清与历史字段', async () => {
  let directoryFailed = false
  const r = await runtime({ chapters: [chapter('chapter-a', 'novel-a', { seq: 0, status: 'legacy' })], beforeRequest: async request => directoryFailed && request.path.endsWith('/metadata') ? { status: 503, body: { detail: '目录受控失败' } } : undefined })
  try {
    await r.enter(); r.input('章节标题', ''); r.input('章前指令', ''); r.input('章节正文', ''); directoryFailed = true
    await r.vm().saveContent(); assert.equal(r.vm().manualState.phase, 'known'); assert.equal(r.vm().isDirty, false)
    assert.deepEqual(r.trace.find(row => row.method === 'PUT').payload, { title: '', directive: '', content: '', revision_source: 'manual_save' })
    assert.equal(r.rows.get('chapter-a').seq, 0); assert(r.messages.some(row => /已保存，但章节列表刷新失败/.test(row.text)))
  } finally { r.dispose() }
})

test('pending与unknown同章写handlers受同记录门禁，已打开版本窗也阻止manual；只读无HTTP副作用', async () => {
  const entered = deferred(), gate = deferred()
  const r = await runtime({ beforeRequest: async request => { if (request.method === 'PUT') { entered.resolve(); await gate.promise } }, afterResponse: async request => request.method === 'PUT' ? { status: 503, body: { detail: '丢回应' } } : undefined })
  let saving
  try {
    await r.enter(); r.vm().showRevisions = true; r.input('章节正文', '明确手工稿'); await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 0)
    r.vm().showRevisions = false; saving = r.vm().saveContent(); await entered.promise
    const methods = ['saveContent', 'doContinue', 'deleteChapter', 'saveSummary', 'doSettle', 'confirmSettle', 'applyRepair', 'openRevisionHistory']
    for (const method of methods) await r.vm()[method](); assert.equal(r.vm().showRevisions, false); assert.equal(r.trace.filter(row => row.method !== 'GET').length, 1)
    gate.resolve(); await saving
    for (const method of methods) await r.vm()[method](); assert.equal(r.vm().showRevisions, false); assert.equal(r.trace.filter(row => row.method !== 'GET').length, 1)
  } finally { gate.resolve(); if (saving) await saving; r.dispose() }
})

test('known手工记录之后续写采用实际新13事实与三raw，旧页内记录不回退metadata', async () => {
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (request.path.endsWith('/continue')) {
      const updated = { ...rows.get('chapter-a'), content: '确认续写后新正文😀', directive: '原指令', summary: '', updated_at: 'new-continuation-fact' }
      rows.set(updated.id, updated); return new Response(`data: ${JSON.stringify({ type: 'done', chapter: updated })}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } })
    }
  } })
  try {
    await r.enter(); r.input('章节正文', '已明确手工存'); await r.vm().saveContent(); await r.vm().doContinue(); await settle()
    assert.equal(r.vm().content, '确认续写后新正文😀'); assert.equal(r.vm().isDirty, false)
    assert.deepEqual(JSON.parse(JSON.stringify(r.vm().manualState.saved)), r.rows.get('chapter-a'))
    assert.equal(r.vm().selectedChapter.updated_at, 'new-continuation-fact'); assert.deepEqual({ ...r.vm().manualState.draft }, author(r.vm()))
  } finally { r.dispose() }
})

test('known手工记录之后只读改写候选不写，明确apply后共享实际新13事实且候选门禁可调用', async () => {
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (request.path.endsWith('/rewrite')) {
      const source = rows.get('chapter-a'), replacement = '候选改写😀', content = replacement + Array.from(source.content).slice(request.payload.selection_end).join('')
      return new Response(`data: ${JSON.stringify({ type: 'token', content: replacement })}\n\ndata: ${JSON.stringify({ type: 'done', preview: true, chapter: source, expected_author_fields: request.payload.expected_author_fields, selection_start: request.payload.selection_start, selection_end: request.payload.selection_end, content })}\n\n`)
    }
  }, afterResponse: async (request, body, rows) => {
    if (request.payload?.revision_source === 'rewrite_apply') { const updated = { ...body, updated_at: 'new-apply-fact' }; rows.set(updated.id, updated); return { body: updated } }
  } })
  try {
    await r.enter(); r.input('章节正文', '手工原文😀后一段'); await r.vm().saveContent(); const before = r.trace.filter(row => row.method === 'PUT').length
    r.vm().contentRef.selectionStart = 0; r.vm().contentRef.selectionEnd = 4
    await r.vm().doRewrite(); assert.equal(r.vm().previewGuarded, true); assert.equal(r.trace.filter(row => row.method === 'PUT').length, before)
    const expected = r.vm().previewFullContent; r.vm().manualState.phase = 'unknown'; await r.vm().applyPreview(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, before)
    r.vm().manualState.phase = 'known'; await r.vm().applyPreview(); await settle()
    assert.equal(r.vm().content, expected); assert.equal(r.vm().isDirty, false)
    assert.deepEqual(JSON.parse(JSON.stringify(r.vm().manualState.saved)), r.rows.get('chapter-a')); assert.equal(r.vm().selectedChapter.updated_at, 'new-apply-fact')
  } finally { r.dispose() }
})

test('ACK必须完整13实际类型、源身份与精确三raw，损坏回应不把DB写入冒认已知', async () => {
  const damaged = [row => ({ ...row, novel_id: 'novel-b' }), row => ({ ...row, id: 'other-chapter' }),
    row => ({ ...row, directive: row.directive.trim() }), row => ({ ...row, content: row.content.replace('\r\n', '\n') }),
    row => ({ ...row, tension_score: '0' }), row => ({ ...row, seq: 0.5 }), row => ({ ...row, status: null })]
  for (const damage of damaged) {
    const r = await runtime({ afterResponse: async (request, body) => request.method === 'PUT' ? { body: damage(body) } : undefined })
    try {
      await r.enter(); r.input('章前指令', ' 原指令 '); r.input('章节正文', '实际原文😀\0\r\n '); const raw = author(r.vm()); await r.vm().saveContent()
      assert.equal(r.vm().manualState.phase, 'unknown'); assert.deepEqual({ ...r.vm().manualState.submission.author }, raw); assert.equal(r.vm().manualState.observed, null)
      r.vm().adoptCurrentManualChapter(); await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
    } finally { r.dispose() }
  }
})

test('pending时较旧核对GET晚404不能覆盖后来known保存事实或标deleted', async () => {
  const putEntered = deferred(), putGate = deferred(), getEntered = deferred(), getGate = deferred(); let checking = false
  const r = await runtime({ beforeRequest: async request => {
    if (request.method === 'PUT') { putEntered.resolve(); await putGate.promise }
    if (checking && request.method === 'GET' && request.path.endsWith('/chapter-a')) { getEntered.resolve(); await getGate.promise; return { status: 404, body: { detail: '旧读取404' } } }
  } })
  let saving, read
  try {
    await r.enter(); r.input('章节正文', '原提交已知事实'); saving = r.vm().saveContent(); await putEntered.promise
    checking = true; read = r.vm().checkManualChapter(); await getEntered.promise
    putGate.resolve(); await saving; getGate.resolve(); await read; await settle()
    assert.equal(r.vm().manualState.phase, 'known'); assert.equal(r.vm().manualState.exists, true); assert.equal(r.vm().savedSnapshot.content, '原提交已知事实')
  } finally { putGate.resolve(); getGate.resolve(); if (saving) await saving; if (read) await read; r.dispose() }
})

test('pending作者自然editback保修订；known只推进sent基线，不重置作者修订或原提交', async () => {
  const entered = deferred(), gate = deferred()
  const r = await runtime({ beforeRequest: async request => { if (request.method === 'PUT') { entered.resolve(); await gate.promise } } })
  let saving
  try {
    await r.enter(); r.input('章节正文', '原提交😀\0\r\n'); saving = r.vm().saveContent(); await entered.promise
    const originalRevision = r.vm().manualState.submission.revision
    r.input('章节正文', '暂改后来文'); r.input('章节正文', '原提交😀\0\r\n'); const laterRevision = r.vm().manualState.revision
    assert(laterRevision > originalRevision); gate.resolve(); await saving
    assert.equal(r.vm().manualState.revision, laterRevision); assert.equal(r.vm().isDirty, false)
    assert.equal(r.vm().content, '原提交😀\0\r\n'); assert.equal(r.vm().manualState.submission.author.content, '原提交😀\0\r\n')
  } finally { gate.resolve(); if (saving) await saving; r.dispose() }
})

test('A unknown及missing记录切B健康已有章，B不假dirty或全局禁写，A可从其他源入口复制', async () => {
  let lost = true
  const r = await runtime({ chapters: [chapter(), chapter('chapter-b', 'novel-b', { title: 'B原章' })], afterResponse: async request => request.method === 'PUT' && lost ? (lost = false, { status: 503, body: { detail: 'A丢回应' } }) : undefined })
  try {
    await r.enter(); r.input('章节正文', 'A精确原提交'); await r.vm().saveContent(); r.input('章节正文', 'A后来稿'); r.rows.delete('chapter-a'); await r.vm().checkManualChapter()
    await r.switchNovel('novel-b'); await r.vm().selectChapter({ id: 'chapter-b', novel_id: 'novel-b' }, true)
    assert.equal(r.vm().manualState, null); assert.equal(r.vm().isDirty, false); assert.equal(r.vm().manualBlocked, false); assert.equal(r.beforeUnload().prevented, true)
    assert.ok(r.region('其他章节页内稿')); await r.button('WritingPanel', '复制其他章节原提交').props.onClick(); assert.equal(JSON.parse(r.clipboard.at(-1)).content, 'A精确原提交')
    await r.button('WritingPanel', '复制其他章节当前稿').props.onClick(); assert.equal(JSON.parse(r.clipboard.at(-1)).content, 'A后来稿')
    r.input('章节正文', 'B合法新稿'); await r.vm().saveContent(); assert.equal(r.vm().manualState.phase, 'known'); assert.equal(r.vm().isDirty, false)
    assert.equal(r.trace.filter(row => row.method === 'PUT').length, 2); assert.equal(r.rows.get('chapter-b').content, 'B合法新稿'); assert.equal(r.rows.has('chapter-a'), false)
  } finally { r.dispose() }
})

test('known共享manual记录有效恢复ACK才关闭父history，目录held中新explicit保存优先保完整事实', async () => {
  let holdDirectory = false, used = false; const entered = deferred(), gate = deferred()
  const r = await runtime({ beforeRequest: async (request, rows) => {
    if (holdDirectory && !used && request.path.endsWith('/metadata')) {
      used = true; const body = [...rows.values()].map(row => { const { content, directive, summary, ...rest } = row; return { ...rest, word_count: Array.from(content).length } })
      entered.resolve(); await gate.promise; return { body }
    }
  } })
  let restoring
  try {
    await r.enter(); r.input('章节正文', 'manual已知原稿'); await r.vm().saveContent(); r.vm().openRevisionHistory(); assert.equal(r.vm().showRevisions, true)
    r.input('章节正文', '恢复前明确当前后来稿'); await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 1)
    r.input('章节正文', 'manual已知原稿'); const restored = { ...r.rows.get('chapter-a'), content: '恢复ACK真实原文', updated_at: 'restore-ack-fact' }; r.rows.set(restored.id, restored)
    holdDirectory = true; restoring = r.vm().onRestored(restored); await entered.promise; assert.equal(r.vm().showRevisions, false)
    assert.equal(r.vm().manualState.saved.updated_at, 'restore-ack-fact'); assert.equal(r.vm().savedSnapshot.content, restored.content)
    await r.vm().saveContent(); assert.equal(r.trace.filter(row => row.method === 'PUT').length, 2); gate.resolve(); await restoring; await settle()
    assert.equal(r.vm().content, 'manual已知原稿'); assert.equal(r.vm().isDirty, false); assert.equal(r.vm().savedSnapshot.content, 'manual已知原稿')
    assert.deepEqual(JSON.parse(JSON.stringify(r.vm().manualState.saved)), r.rows.get('chapter-a'))
  } finally { gate.resolve(); if (restoring) await restoring; r.dispose() }
})
