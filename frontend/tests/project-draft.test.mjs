import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { conversation, deferred, fields, novel, nodes, projectRuntime, settle } from './project-draft-fixtures.mjs'

test('真实创建只提交冻结原文且等待中新梗概保留，正常重开不清唯一新稿', async () => {
  const gate = deferred(), r = await projectRuntime({ createNovel: () => gate.promise }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(' 原提交\n😀 ')
  const pending = c.vm.submitCreate(); c.vm.createForm.value.summary = '等待中的唯一新梗概'
  assert.equal(r.calls.find(row => row.name === 'createNovel').args[0].summary, ' 原提交\n😀 ')
  gate.resolve(novel(fields(' 原提交\n😀 '))); await pending
  assert.equal(c.vm.showCreate.value, true); assert.equal(c.vm.createForm.value.summary, '等待中的唯一新梗概')
  assert.equal(c.vm.createState.value.savedNovel.summary, ' 原提交\n😀 ')
  c.vm.openCreate(); assert.equal(c.vm.createForm.value.summary, '等待中的唯一新梗概'); r.stop()
})

test('同模块跨重挂 pending 创建单飞，旧完成不 toast 或清新实例作者稿', { timeout: 1200 }, async () => {
  const gate = deferred(), r = await projectRuntime({ createNovel: () => gate.promise }), first = await r.mount()
  first.vm.openCreate(); first.vm.createForm.value = fields(); const pending = first.vm.submitCreate()
  await first.vm.submitCreate(); first.stop(); const second = await r.mount(); second.vm.openCreate()
  second.vm.createForm.value.summary = '重挂新输入'; await second.vm.submitCreate()
  assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1)
  gate.resolve(novel()); await pending
  assert.equal(second.vm.createForm.value.summary, '重挂新输入'); assert.equal(second.vm.showCreate.value, true)
  assert.equal(first.notices.length, 0); assert.equal(second.notices.length, 0); r.stop()
})

test('升级 A→B→A 原源锁和新稿保留，旧回包不关闭新epoch或覆盖B', { timeout: 1200 }, async () => {
  const gate = deferred(), r = await projectRuntime({ promoteConversation: () => gate.promise }), c = await r.mount()
  c.vm.conversations.value = [conversation('a'), conversation('b')]
  c.vm.openPromote(conversation('a')); c.vm.promoteForm.value = fields(); const pending = c.vm.submitPromote()
  c.vm.openPromote(conversation('b')); c.vm.promoteForm.value.summary = 'B构思'
  c.vm.openPromote(conversation('a')); c.vm.promoteForm.value.summary = 'A后来构思'; await c.vm.submitPromote()
  assert.equal(r.calls.filter(row => row.name === 'promoteConversation').length, 1)
  gate.resolve(novel()); await pending
  assert.equal(c.vm.promoteTarget.value.id, 'a'); assert.equal(c.vm.promoteForm.value.summary, 'A后来构思'); assert.equal(c.notices.length, 0)
  c.vm.openPromote(conversation('b')); assert.equal(c.vm.promoteForm.value.summary, 'B构思'); r.stop()
})

test('已存后仅改梗概才显式部分PUT，等待中改回原值仍dirty且再次只送梗概', async () => {
  const gate = deferred(), r = await projectRuntime({ updateNovel: () => gate.promise }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
  c.vm.createForm.value.summary = '新梗概'; const saving = c.vm.saveProjectChanges('create')
  await c.vm.saveProjectChanges('create'); c.vm.createForm.value.summary = '原梗概'
  gate.resolve(novel({ summary: '新梗概', genre: '另一窗口新题材', style_anchor: '另一窗口新文风' })); await saving
  assert.deepEqual(r.calls.filter(row => row.name === 'updateNovel').map(row => row.args), [['novel-saved', { summary: '新梗概' }]])
  assert.equal(c.vm.createForm.value.summary, '原梗概'); assert.equal(c.vm.createState.value.baseline.summary, '新梗概')
  assert.equal(c.vm.projectUnsaved.value, true); assert.deepEqual(r.helper.projectChanges(c.vm.createState.value), { summary: '原梗概' }); r.stop()
})

test('unknown保原提交与新稿，正常目录核对不猜同名回执或自动再次POST', async () => {
  const r = await projectRuntime({ createNovel: async () => { throw Object.assign(new Error('连接已断'), { status: 503 }) }, listNovels: async () => [novel()] }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate(); c.vm.createForm.value.summary = '后来新稿'
  await c.vm.submitCreate(); await c.vm.checkProjectDirectory('create')
  assert.equal(c.vm.createState.value.phase, 'unknown'); assert.equal(c.vm.createState.value.savedNovel, null)
  assert.equal(c.vm.createState.value.original.summary, '原梗概'); assert.equal(c.vm.createForm.value.summary, '后来新稿')
  assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1)
  assert.ok(nodes(c.render(), node => node.props?.['input-props']?.['aria-label'] === '原提交构思').length); r.stop()
})

test('真实201后目录失败仍绑定原已存ID，无新输入时无变化零PUT且不能再次创建', async () => {
  let reads = 0
  const r = await projectRuntime({ listNovels: async () => { if (++reads > 1) throw new Error('目录暂不可读'); return [] } }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
  assert.equal(c.vm.createState.value.phase, 'saved'); assert.equal(c.vm.createState.value.savedNovel.id, 'novel-saved')
  assert.deepEqual(c.vm.novels.value.map(item => item.id), ['novel-saved'])
  assert.ok(c.notices.some(row => /已保存.*刷新未完成/.test(row.text)))
  await c.vm.submitCreate(); await c.vm.saveProjectChanges('create')
  assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1); assert.equal(r.calls.filter(row => row.name === 'updateNovel').length, 0)
  assert.equal(c.vm.projectUnsaved.value, false)
  const buttons = nodes(c.render(), node => node.type?.name === 'NButton')
  assert.ok(buttons.some(node => node.props.disabled && JSON.stringify(node.children?.default?.()).includes('保存当前修改到此作品'))); r.stop()
})

test('缺字段或不匹配原三字段的创建回应不能known/PUT，保原稿和当前稿', async () => {
  const malformed = [null, {}, novel({ id: '' }), novel({ style_anchor: null }), novel({ summary: '不是本次原文' }), novel({ genre: '错误题材' }), novel({ title: '错误书名' })]
  for (const result of malformed) {
    const r = await projectRuntime({ createNovel: async () => result }), c = await r.mount()
    c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
    assert.equal(c.vm.createState.value.phase, 'unknown'); assert.equal(c.vm.createState.value.savedNovel, null)
    assert.equal(c.vm.createForm.value.summary, '原梗概'); assert.equal(c.vm.createState.value.original.summary, '原梗概')
    await c.vm.saveProjectChanges('create'); await c.vm.submitCreate()
    assert.equal(r.calls.filter(row => row.name === 'updateNovel').length, 0); assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1)
    assert.equal(c.notices.some(row => row.type === 'success'), false); r.stop()
  }
})

test('关闭pending只隐藏页内表单，确认期间editback阻止关闭，重开保持锁和新稿', async () => {
  const gate = deferred(), r = await projectRuntime({ createNovel: () => gate.promise }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(); const pending = c.vm.submitCreate()
  c.vm.closeProject('create'); const first = c.dialogs.at(-1)
  assert.match(first.content, /不会取消后台保存/)
  c.vm.createForm.value.summary = '期间改动'; c.vm.createForm.value.summary = '原梗概'
  assert.equal(first.onPositiveClick(), false); assert.equal(c.vm.showCreate.value, true)
  c.vm.closeProject('create'); c.dialogs.at(-1).onPositiveClick(); assert.equal(c.vm.showCreate.value, false)
  c.vm.openCreate(); c.vm.createForm.value.summary = '重开后新稿'; await c.vm.submitCreate()
  gate.resolve(novel()); await pending
  assert.equal(c.vm.showCreate.value, true); assert.equal(c.vm.createForm.value.summary, '重开后新稿')
  assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1); assert.equal(c.notices.filter(row => row.type === 'success').length, 0); r.stop()
})

test('升级绑定后关闭和组件重挂仍有真实模板恢复入口，删除源后只能复制', async () => {
  let threads = [conversation('a')], saved = []
  const r = await projectRuntime({ listConversations: async () => threads, listNovels: async () => saved,
    promoteConversation: async (_cid, payload) => { const result = novel(payload); saved = [result]; threads = [{ ...conversation('a'), novel_id: result.id }]; return result } }), first = await r.mount()
  first.vm.openPromote(conversation('a')); first.vm.promoteForm.value = fields(); await first.vm.submitPromote()
  first.vm.promoteForm.value.summary = '绑定后唯一新稿'; first.vm.closeProject('promote'); first.dialogs.at(-1).onPositiveClick(); first.stop()
  const second = await r.mount()
  const record = second.vm.projectRecords.value.find(row => row.kind === 'promote')
  const entry = nodes(second.render(), node => node.type?.name === 'NButton' && JSON.stringify(node.children?.default?.()).includes('升级作品构思'))[0]
  assert.ok(entry); entry.props.onClick()
  assert.equal(second.vm.promoteForm.value.summary, '绑定后唯一新稿'); assert.equal(second.vm.promoteTarget.value.novel_id, 'novel-saved')
  threads = []; await second.vm.checkProjectDirectory('promote')
  assert.equal(record.state.sourceExists, false); await second.vm.saveProjectChanges('promote'); await second.vm.submitPromote()
  await second.vm.copyProject('promote'); assert.match(r.copied.at(-1), /绑定后唯一新稿/)
  assert.equal(r.calls.filter(row => row.name === 'promoteConversation').length, 1); assert.equal(r.calls.filter(row => row.name === 'updateNovel').length, 0); r.stop()
})

test('显式PUT未知只按真实ID读取当前资料，不覆盖新编辑也不把未sent字段变成新意图', async () => {
  let saved = novel()
  const r = await projectRuntime({ listNovels: async () => [saved], updateNovel: async (_id,payload) => { saved = novel({ ...payload, genre: '外部新题材', style_anchor: '外部新文风' }); throw new Error('已写入但回应丢失') } }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
  c.vm.createForm.value.summary = '本次提交新梗概'; await c.vm.saveProjectChanges('create'); c.vm.createForm.value.title = '等待期间新书名'
  assert.equal(c.vm.createState.value.phase, 'unknown'); await c.vm.saveProjectChanges('create'); await c.vm.submitCreate()
  assert.equal(r.calls.filter(row => row.name === 'updateNovel').length, 1); await c.vm.checkProjectDirectory('create')
  assert.equal(c.vm.createState.value.phase, 'saved'); assert.equal(c.vm.createForm.value.title, '等待期间新书名')
  assert.equal(c.vm.createForm.value.genre, '故事'); assert.equal(c.vm.createState.value.savedNovel.genre, '外部新题材')
  assert.deepEqual(r.helper.projectChanges(c.vm.createState.value), { title: '等待期间新书名' })
  assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 1); r.stop()
})

test('PUT缺字段/错ID/错提交值都unknown，新稿保留且不能盲再次保存', async () => {
  for (const result of [novel({ id: 'another-novel' }), { id: 'novel-saved', summary: '新梗概' }, novel({ summary: '并非本次新梗概' })]) {
    const r = await projectRuntime({ updateNovel: async () => result }), c = await r.mount()
    c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate(); c.vm.createForm.value.summary = '新梗概'
    await c.vm.saveProjectChanges('create'); assert.equal(c.vm.createState.value.phase, 'unknown')
    assert.equal(c.vm.createState.value.savedNovel.id, 'novel-saved'); assert.equal(c.vm.createForm.value.summary, '新梗概')
    assert.equal(c.vm.createState.value.baseline.summary, '原梗概'); await c.vm.saveProjectChanges('create'); assert.equal(r.calls.filter(row => row.name === 'updateNovel').length, 1); r.stop()
  }
})

test('目录核对冻结升级A身份，切B和旧read不误记绑定；坏threads不冒已核对', async () => {
  const gate = deferred(); let reads = 0
  const r = await projectRuntime({ listConversations: async () => ++reads === 1 ? [conversation('a'),conversation('b')] : gate.promise }), c = await r.mount()
  c.vm.openPromote(conversation('a')); const checking = c.vm.checkProjectDirectory('promote'); const original = c.vm.promoteState.value
  c.vm.openPromote(conversation('b')); gate.resolve([{ ...conversation('a'), novel_id: 'actual-a' }, { ...conversation('b'), novel_id: 'actual-b' }]); await checking
  assert.equal(original.bindingId, 'actual-a'); assert.equal(c.vm.promoteState.value.bindingId, null); assert.equal(c.notices.length, 0)
  const invalid = await projectRuntime({ listConversations: async () => [{ id: 'a', novel_id: [] }] }), bad = await invalid.mount()
  bad.vm.openPromote(conversation('a')); await bad.vm.checkProjectDirectory('promote')
  assert.equal(bad.vm.promoteState.value.directory, null); assert.equal(bad.vm.promoteState.value.sourceExists, null); invalid.stop(); r.stop()
})

test('空白构思dirty和beforeunload保护，离页取消或显式保留均不发送', async () => {
  const r = await projectRuntime(), c = await r.mount(); c.vm.openCreate(); c.vm.createForm.value.summary = ' \n　'
  assert.equal(c.vm.projectUnsaved.value, true)
  let prevented = false; const event = { preventDefault: () => { prevented = true }, returnValue: undefined }
  c.listeners.get('beforeunload')(event); assert.equal(prevented, true); assert.equal(event.returnValue, '')
  c.vm.enterWorkbench('other'); assert.equal(c.events.length, 0); const confirm = c.dialogs.at(-1)
  assert.match(confirm.content, /作品构思/); assert.match(confirm.content, /完整刷新.*复制/); confirm.onPositiveClick()
  assert.deepEqual(c.events, [['enter-workbench','other']]); assert.equal(r.calls.some(row => ['createNovel','promoteConversation','updateNovel'].includes(row.name)), false)
  c.stop(); assert.equal(c.listeners.has('beforeunload'), false)
})

test('复制故障提供可手动选取的只读当前全文，关闭不清新稿', async () => {
  const r = await projectRuntime({}, { clipboardError: true }), c = await r.mount(); c.vm.openCreate(); c.vm.createForm.value = fields('保留\n😀\u0000全文')
  await c.vm.copyProject('create'); assert.match(c.vm.copyFallback.value, /保留\n😀\u0000全文/)
  const box = nodes(c.render(), node => node.props?.['input-props']?.['aria-label'] === '当前作品构思')[0]
  assert.equal(Object.hasOwn(box.props, 'readonly'), true); assert.equal(box.props.value, c.vm.copyFallback.value)
  c.vm.closeProject('create'); c.dialogs.at(-1).onPositiveClick(); c.vm.openCreate(); assert.equal(c.vm.createForm.value.summary, '保留\n😀\u0000全文'); r.stop()
})

test('本次作品表单标签经真实Naive NInput落在原生input/textarea，复制文本框可访问且只读', async () => {
  // Naive SSR 初始化需真实无DOM环境；此前SFC夹具的window仅用于导航监听。
  const originalWindow = globalThis.window
  delete globalThis.window
  const { NInput } = await import('naive-ui')
  globalThis.window = originalWindow
  const r = await projectRuntime({}, { clipboardError: true }), c = await r.mount()
  c.vm.openCreate(); c.vm.createForm.value = fields('精确全文😀'); await c.vm.submitCreate()
  c.vm.conversations.value = [conversation('a')]; c.vm.openPromote(conversation('a'))
  await c.vm.copyProject('create')
  const boxes = nodes(c.render(), node => node.type?.name === 'NInput' && node.props?.['input-props'])
  const labels = boxes.map(node => node.props['input-props']['aria-label'])
  assert.ok(['新建作品书名','新建作品题材','新建作品梗概','升级作品书名','升级作品题材','升级作品梗概','原提交构思','当前作品构思'].every(label => labels.includes(label)))
  for (const box of boxes) {
    const props = { ...box.props, inputProps: box.props['input-props'], readonly: Object.hasOwn(box.props, 'readonly'), 'onUpdate:value': undefined }
    delete props['input-props']
    const html = await renderToString(createSSRApp({ render: () => h(NInput, props) }))
    assert.ok(html.includes(`aria-label="${props.inputProps['aria-label']}"`))
    assert.match(html, props.type === 'textarea' ? /<textarea[^>]+aria-label=/ : /<input[^>]+aria-label=/)
    if (props.readonly) assert.match(html, /<textarea[^>]+readonly/)
  }
  r.stop()
})

test('另建是作者明确新意图，确认期间新编辑拒绝换记录，pending/unknown不能另建', async () => {
  const r = await projectRuntime(), c = await r.mount(); c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
  c.vm.startFreshCreate(); const dialog = c.dialogs.at(-1); assert.match(dialog.content, /已保存作品不会删除/)
  c.vm.createForm.value.summary = '确认期间新稿'; assert.equal(dialog.onPositiveClick(), false); assert.equal(c.vm.createState.value.savedNovel.id, 'novel-saved')
  c.vm.startFreshCreate(); c.dialogs.at(-1).onPositiveClick(); assert.deepEqual(c.vm.createForm.value, { title:'',genre:'',summary:'' })
  c.vm.createForm.value = fields(); await c.vm.submitCreate(); assert.equal(r.calls.filter(row => row.name === 'createNovel').length, 2)
  c.vm.createState.value.phase = 'unknown'; const count = c.dialogs.length; c.vm.startFreshCreate(); assert.equal(c.dialogs.length, count); r.stop()
})
