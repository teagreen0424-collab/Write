import assert from 'node:assert/strict'
import test from 'node:test'
import { outlineAuthor, deferred, settle } from './outline-author-fixture.mjs'

test('大纲仅改标题保留另一处已存概要，提交保持作者原文', async () => {
  const c = await outlineAuthor(); try {
    c.edit(); c.vm.form.value.title = '  作者标题  '
    c.rows.get('novel-a').summary = '另一处的新概要'
    await c.vm.save()
    assert.deepEqual(c.writes[0].payload, { title: '  作者标题  ' })
    assert.equal(c.rows.get('novel-a').summary, '另一处的新概要')
    assert.equal(c.vm.showModal.value, false)
  } finally { c.dispose() }
})
test('大纲仅清空概要保留另一处标题，空串是明确修改', async () => {
  const c = await outlineAuthor(); try {
    c.edit(); c.vm.form.value.summary = ''; c.rows.get('novel-a').title = '另一处标题'
    await c.vm.save()
    assert.deepEqual(c.writes[0].payload, { summary: '' })
    assert.equal(c.rows.get('novel-a').title, '另一处标题')
    assert.equal(c.rows.get('novel-a').summary, '')
  } finally { c.dispose() }
})
test('大纲明确改两字段仍按作者本次原文覆盖同字段，不增加并发拒绝', async () => {
  const c = await outlineAuthor(); try {
    c.edit(); Object.assign(c.vm.form.value, { title: '作者标题', summary: '作者概要' })
    Object.assign(c.rows.get('novel-a'), { title: '其他标题', summary: '其他概要' })
    await c.vm.save()
    assert.deepEqual(c.writes[0].payload, { title: '作者标题', summary: '作者概要' })
    assert.equal(c.rows.get('novel-a').title, '作者标题')
    assert.equal(c.rows.get('novel-a').summary, '作者概要')
  } finally { c.dispose() }
})
test('大纲未修改或改后还原不发写入，也不覆盖另一处更新', async () => {
  const c = await outlineAuthor(); try {
    c.edit(); c.vm.form.value.title = '临时'; c.vm.form.value.title = '原标题'
    c.rows.get('novel-a').summary = '另一处概要'
    await c.vm.save()
    assert.deepEqual(c.writes, [])
    assert.equal(c.rows.get('novel-a').summary, '另一处概要')
  } finally { c.dispose() }
})
test('大纲已存标题回应不清保存期间的概要，下一次仅存概要保留新标题', async () => {
  const pending = deferred(), c = await outlineAuthor({ afterUpdate: async () => pending.promise })
  try {
    c.edit(); c.vm.form.value.title = '首次作者标题'; const saving = c.vm.save()
    await settle(); c.vm.form.value.summary = '后来概要'; pending.resolve(); await saving
    assert.equal(c.vm.dirty.value, true); assert.equal(c.vm.form.value.summary, '后来概要')
    assert.equal(c.writes.length, 1)
    c.rows.get('novel-a').title = '另一处后来标题'; await c.vm.save()
    assert.deepEqual(c.writes.map(row => row.payload), [{ title: '首次作者标题' }, { summary: '后来概要' }])
    assert.equal(c.rows.get('novel-a').title, '另一处后来标题')
  } finally { pending.resolve(); c.dispose() }
})
test('大纲失败保留原稿与基线，明确重试仍只发修改字段', async () => {
  let fail = true
  const c = await outlineAuthor({ beforeUpdate: async () => { if (fail) throw new Error('受控失败') } })
  try {
    c.edit(); c.vm.form.value.title = '待重试标题'; await c.vm.save()
    assert.equal(c.vm.dirty.value, true); assert.equal(c.vm.showModal.value, true)
    assert.equal(c.vm.form.value.title, '待重试标题'); assert.equal(c.writes.length, 1)
    assert.equal(c.messages.at(-1).type, 'error')
    fail = false; c.rows.get('novel-a').summary = '失败期间另一处概要'; await c.vm.save()
    assert.deepEqual(c.writes.map(row => row.payload), [{ title: '待重试标题' }, { title: '待重试标题' }])
    assert.equal(c.rows.get('novel-a').summary, '失败期间另一处概要')
  } finally { c.dispose() }
})
test('大纲新建仍提交完整四作者字段，空白标题仍被拒绝', async () => {
  const c = await outlineAuthor(); try {
    c.vm.openCreate('novel-a-volume', 'act'); c.vm.form.value.title = '  '; await c.vm.save()
    assert.equal(c.writes.length, 0)
    Object.assign(c.vm.form.value, { title: '  新幕  ', summary: '新幕概要' }); await c.vm.save()
    assert.deepEqual(c.writes[0], { method: 'POST', novelId: 'novel-a', payload: { title: '  新幕  ', level: 'act', summary: '新幕概要', parent_id: 'novel-a-volume' } })
  } finally { c.dispose() }
})
test('大纲新建回应保留后来概要，第二次保存不覆盖另一处已改的新建标题', async () => {
  const pending = deferred(), c = await outlineAuthor({ afterCreate: async () => pending.promise })
  try {
    c.vm.openCreate('novel-a-volume', 'act'); c.vm.form.value.title = '新幕'
    const saving = c.vm.save(); await settle(); c.vm.form.value.summary = '创建期间后来概要'
    pending.resolve(); await saving
    assert.equal(c.vm.editingId.value, 'novel-a-created'); assert.equal(c.vm.dirty.value, true)
    c.rows.get('novel-a-created').title = '另一处已改新幕'; await c.vm.save()
    assert.deepEqual(c.writes[1].payload, { summary: '创建期间后来概要' })
    assert.equal(c.rows.get('novel-a-created').title, '另一处已改新幕')
    assert.equal(c.writes.filter(row => row.method === 'POST').length, 1)
  } finally { pending.resolve(); c.dispose() }
})
test('大纲旧作品迟到回应不清新作品编辑，也不发新作品写入', async () => {
  const pending = deferred(), c = await outlineAuthor({ afterUpdate: async () => pending.promise })
  try {
    c.edit(); c.vm.form.value.title = 'A作者标题'; const saving = c.vm.save(); await settle()
    c.props.novelId = 'novel-b'; await settle(); c.edit(); c.vm.form.value.summary = 'B作者原稿'
    pending.resolve(); await saving
    assert.equal(c.vm.showModal.value, true); assert.equal(c.vm.dirty.value, true)
    assert.equal(c.vm.form.value.summary, 'B作者原稿'); assert.equal(c.messages.length, 0)
    assert.equal(c.writes.length, 1); assert.equal(c.writes[0].novelId, 'novel-a')
  } finally { pending.resolve(); c.dispose() }
})
