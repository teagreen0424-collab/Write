import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { NInput } from 'naive-ui'
import { styleAnchorRuntime, settle } from './style-anchor-fixtures.mjs'

const novel = (id = 'a') => ({ id, title: id, genre: '', summary: '', style_anchor: '', created_at: '', updated_at: '' })
const character = (id = 'a-character', novelId = 'a', patch = {}) => ({ id, novel_id: novelId, name: '人物原名', role: '', personality: '', goal: '原目标', appearance: '', catchphrase: '', pov: '', frequency: '', known_info: '', extras: {}, created_at: '', updated_at: '', ...patch })
const wait = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const vm = c => c.instance('SettingsPanel').setupState
const wb = c => c.instance('WorkbenchView').setupState
async function mounted(api = {}, options = {}) {
  const c = await styleAnchorRuntime({ app: true, ...options, api: { listNovels: async () => [novel(), novel('b')], listChapterMetadata: async () => [], listEntities: async (id, path) => path === 'characters' ? [character(`${id}-character`, id)] : [], ...api } })
  c.vm.onEnterWorkbench('a'); await settle(); return c
}
async function leave(c) {
  wb(c).returnToChat(); if (wb(c).showNavigationConfirm) wb(c).confirmNavigation(); await settle()
  assert.equal(c.instance('SettingsPanel'), null)
}
async function edit(c, id = 'a-character') {
  vm(c).startEdit(vm(c).allEntities[vm(c).selectedType].find(row => row.id === id)); await settle()
}
async function enter(c, id) {
  wb(c).changeNovel(id); if (wb(c).showNavigationConfirm) wb(c).confirmNavigation(); await settle()
}
function findVNode(node, predicate) {
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  if (node.component) { const found = findVNode(node.component.subTree, predicate); if (found) return found }
  if (Array.isArray(node.children)) for (const child of node.children) { const found = findVNode(child, predicate); if (found) return found }
  return null
}
const inputProps = (c, label) => {
  const found = findVNode(c.instance('SettingsPanel').subTree, node => (node.props?.['input-props'] ?? node.props?.inputProps)?.['aria-label'] === label)
  assert.ok(found, `actual compiled input ${label}`)
  return { ...found.props, inputProps: found.props['input-props'] ?? found.props.inputProps }
}
const record = (type, id = `a-${type}`, novelId = 'a', patch = {}) => ({ id, novel_id: novelId, created_at: '', updated_at: '', ...{
  character: character(id, novelId), location: { name: '原地点', level: '', parent_id: '', description: '原描述' },
  world_setting: { name: '原世界观', category: '', description: '原描述', tags: ['first', 'second'] },
  timeline_event: { title: '原事件', time_point: '', participants: ['first'], cause_id: '', description: '原描述' },
  foreshadowing: { content: '原伏笔', status: 'planted', related_characters: ['first'], planted_chapter_id: '', consumed_chapter_id: '' },
  relationship: { subject_id: 'character-a', object_id: 'location-a', relation_type: '原关系', note: '原备注' },
}[type], ...patch })
const typePaths = { character: 'characters', location: 'locations', world_setting: 'world-settings', timeline_event: 'timeline-events', foreshadowing: 'foreshadowings', relationship: 'relationships' }

test('设定同目标preHTTP等待跨真实App重挂禁第二PUT，旧完成只推进原提交基线并保后来稿', async () => {
  const gate = wait(); let calls = 0
  const rows = [character()]
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], updateEntity: async (_id, _path, id, payload) => {
    calls++; if (calls === 1) await gate.promise
    Object.assign(rows.find(row => row.id === id), payload); return structuredClone(rows.find(row => row.id === id))
  } })
  let pending
  try {
    await edit(c); const old = vm(c); old.form.goal = '原提交目标'; pending = old.save()
    await leave(c); c.vm.onEnterWorkbench('a'); await settle(); await edit(c)
    const current = vm(c); current.form.goal = '重挂作者后来稿'; await current.save(); assert.equal(calls, 1)
    const messages = c.messages.length; gate.resolve(); await pending
    assert.equal(current.form.goal, '重挂作者后来稿'); assert.equal(JSON.parse(current.formBaseline).goal, '原提交目标')
    assert.equal(current.entityDirty, true); assert.equal(c.messages.length, messages)
  } finally { gate.resolve(); await pending; c.dispose(); await settle() }
})

test('设定真实创建事实后的transport503不盲第二POST，同表单原提交和后来稿跨重挂保留', async () => {
  let calls = 0; const rows = []
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], createEntity: async (id, _path, payload) => {
    calls++; rows.push(character(`created-${calls}`, id, payload)); throw Object.assign(new Error('201 后回应传输丢失'), { status: 503 })
  } })
  try {
    const original = vm(c); original.form.name = '原创建姓名'; await original.save(); original.form.name = '后来创建稿'; await original.save()
    assert.equal(calls, 1); await leave(c); c.vm.onEnterWorkbench('a'); await settle()
    assert.equal(vm(c).form.name, '后来创建稿'); await vm(c).save(); assert.equal(calls, 1)
    assert.equal(vm(c).entityState.phase, 'unknown'); assert.equal(vm(c).entityState.original.name, '原创建姓名')
  } finally { c.dispose(); await settle() }
})

test('设定正常切换类型再返回原记录保留等待和精确原稿，其他记录可以独立保存', async () => {
  const gate = wait(); const calls = [], rows = [character(), character('other')]
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], updateEntity: async (_novel, _path, id, payload) => {
    calls.push({ id, payload }); if (id === 'a-character') await gate.promise
    Object.assign(rows.find(row => row.id === id), payload); return structuredClone(rows.find(row => row.id === id))
  } })
  let pending
  try {
    await edit(c); vm(c).form.goal = '原目标精确提交'; pending = vm(c).save(); vm(c).form.goal = '离页前后来稿'
    vm(c).changeType('location'); vm(c).changeType('character'); await edit(c, 'other'); vm(c).form.goal = '另一记录独立提交'; await vm(c).save()
    await edit(c); assert.equal(vm(c).form.goal, '离页前后来稿'); await vm(c).save(); assert.equal(calls.length, 2)
    gate.resolve(); await pending; assert.equal(vm(c).entityState.original.goal, '原目标精确提交'); assert.equal(vm(c).form.goal, '离页前后来稿')
  } finally { gate.resolve(); await pending; c.dispose(); await settle() }
})

test('六类完整回应确认真实部分PUT和新建身份，原始表单JSON与payload分别保留', async () => {
  for (const type of Object.keys(typePaths)) {
    const rows = [record(type)], writes = []
    const c = await mounted({ listEntities: async (_id, path) => path === typePaths[type] ? structuredClone(rows) : [],
      updateEntity: async (novelId, path, id, payload) => { writes.push({ method: 'PUT', payload }); Object.assign(rows.find(row => row.id === id), payload); return structuredClone(rows.find(row => row.id === id)) },
      createEntity: async (novelId, path, payload) => { writes.push({ method: 'POST', payload }); const saved = record(type, `${type}-created`, novelId, payload); rows.push(saved); return structuredClone(saved) },
    })
    try {
      vm(c).changeType(type); await edit(c, `a-${type}`)
      const field = vm(c).config.fields.find(field => !field.required && ['text', 'textarea'].includes(field.type))
      vm(c).form[field.key] = ` \n作者仅改${field.key}😀\0\r\n `; await vm(c).save()
      assert.deepEqual(writes[0], { method: 'PUT', payload: { [field.key]: ` \n作者仅改${field.key}😀\0\r\n ` } })
      assert.equal(vm(c).entityState.phase, 'saved'); assert.equal(vm(c).entityState.original[field.key], writes[0].payload[field.key])
      vm(c).startCreate(); const intent = vm(c).entityState.key
      for (const field of vm(c).config.fields.filter(field => field.required)) vm(c).form[field.key] = `新${field.key}`
      await vm(c).save(); assert.equal(vm(c).editingId, `${type}-created`); assert.equal(vm(c).entityState.key, intent)
      assert.deepEqual(Object.keys(writes[1].payload).sort(), vm(c).config.fields.map(field => field.key).sort())
      assert.equal(vm(c).dirty, false)
    } finally { c.dispose() }
  }
})

test('缺完整字段错误来源错误sent或JSON容器的200均未知，保原稿基线并禁第二写', async () => {
  const bad = [{}, character('other'), character('a-character', 'b'), character('a-character', 'a', { goal: '错提交' }),
    character('a-character', 'a', { goal: '原提交', extras: null }), character('a-character', 'a', { goal: '原提交', extras: [] }),
    character('a-character', 'a', { goal: '原提交', known_info: undefined }), character('a-character', 'a', { goal: '原提交', created_at: undefined })]
  for (const response of bad) {
    let writes = 0; const c = await mounted({ updateEntity: async () => { writes++; return response } })
    try {
      await edit(c); const baseline = vm(c).formBaseline; vm(c).form.goal = '原提交'; await vm(c).save(); vm(c).form.goal = '后来精确稿'; await vm(c).save()
      assert.equal(writes, 1); assert.equal(vm(c).entityState.phase, 'unknown'); assert.equal(vm(c).entityState.saved, null)
      assert.equal(vm(c).formBaseline, baseline); assert.equal(vm(c).entityState.original.goal, '原提交'); assert.equal(vm(c).form.goal, '后来精确稿')
    } finally { c.dispose() }
  }
})

test('known创建先保原ID与完整事实，后置列表失败不否认保存或重复POST', async () => {
  let writes = 0, saved = false; const c = await mounted({ listEntities: async () => { if (saved) throw new Error('后置列表故障'); return [] },
    createEntity: async (id, _path, payload) => { writes++; saved = true; return character('known-created', id, payload) },
    updateEntity: async (id, _path, itemId, payload) => { writes++; return character(itemId, id, { name: '原名', goal: '后续目标', ...payload }) },
  })
  try {
    vm(c).form.name = '原名'; await vm(c).save(); assert.equal(vm(c).entityState.phase, 'saved'); assert.equal(vm(c).editingId, 'known-created')
    assert.equal(vm(c).entityState.saved.name, '原名'); assert.equal(vm(c).form.name, '原名'); await vm(c).save(); assert.equal(writes, 1)
    vm(c).form.goal = '后续目标'; await vm(c).save(); assert.equal(writes, 2)
    assert.equal(c.requests.filter(row => row.name === 'createEntity').length, 1); assert.equal(c.requests.filter(row => row.name === 'updateEntity').length, 1)
    assert.ok(c.messages.some(row => /已保存.*列表刷新失败/.test(row.text)))
  } finally { c.dispose() }
})

test('known目标unknown只读当前事实不充当回执，明确采用保后来稿且零自动PUT', async () => {
  let calls = 0; const rows = [character()]
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], updateEntity: async (_id, _path, id, payload) => {
    calls++; Object.assign(rows[0], payload); if (calls === 1) throw Object.assign(new Error('原200传输丢失'), { status: 503 }); return structuredClone(rows[0])
  } })
  try {
    await edit(c); const p = vm(c); p.form.goal = '原提交'; await p.save(); p.form.goal = '作者后来稿'; await p.checkEntity()
    assert.equal(p.entityState.phase, 'unknown'); assert.equal(p.entityState.saved, null); assert.equal(p.entityState.observed.goal, '原提交')
    assert.equal(JSON.parse(p.formBaseline).goal, '原目标'); assert.equal(calls, 1)
    p.adoptCurrentEntity(); assert.equal(calls, 1); assert.equal(p.form.goal, '作者后来稿'); assert.equal(JSON.parse(p.formBaseline).goal, '原提交')
    assert.equal(p.entityState.original.goal, '原提交'); assert.match(p.entityState.error, /原请求结果仍未知/)
    await p.save(); assert.equal(calls, 2); assert.deepEqual(c.requests.filter(row => row.name === 'updateEntity').at(-1).args[3], { goal: '作者后来稿' })
  } finally { c.dispose() }
})

test('未知创建同名目录不改绑，明确另建产生独立意图且原创建与后来稿仍可复制', async () => {
  let writes = 0; const rows = []
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], createEntity: async (id, _path, payload) => {
    writes++; const row = character(`created-${writes}`, id, payload); rows.push(row); if (writes === 1) throw Object.assign(new Error('201 后503'), { status: 503 }); return structuredClone(row)
  } })
  try {
    const p = vm(c); p.form.name = '同名原提交'; await p.save(); const original = p.entityState; p.form.name = '未知后来稿'; await p.checkEntity(); p.adoptCurrentEntity(); await p.save()
    assert.equal(writes, 1); assert.equal(p.editingId, null); assert.equal(original.saved, null); assert.equal(original.observed, null); assert.match(original.error, /同名.*不能证明/)
    p.startCreate(); assert.notEqual(p.entityState.key, original.key); assert.equal(writes, 1)
    p.form.name = '同名原提交'; await p.save(); assert.equal(writes, 2); assert.equal(p.editingId, 'created-2')
    assert.equal(p.otherEntityDrafts[0].original.name, '同名原提交'); assert.equal(p.otherEntityDrafts[0].draft.name, '未知后来稿'); assert.equal(p.otherEntityDrafts[0].phase, 'unknown')
  } finally { c.dispose() }
})

test('原输入改回与未sent外部新事实分清，等待后原基线只推进sent字段', async () => {
  const gate = wait(), rows = [character()], calls = []
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [], updateEntity: async (_id, _path, id, payload) => {
    calls.push(payload); Object.assign(rows[0], payload, { known_info: '外部最新事实', extras: { external: true } }); const saved = structuredClone(rows[0]); if (calls.length === 1) await gate.promise; return saved
  } })
  let pending
  try {
    await edit(c); const p = vm(c); p.form.goal = '原提交目标'; pending = p.save(); p.form.goal = '其他'; p.form.goal = '原目标'; gate.resolve(); await pending
    assert.equal(p.form.goal, '原目标'); assert.equal(p.form.known_info, ''); assert.equal(JSON.parse(p.formBaseline).goal, '原提交目标'); assert.equal(p.entityDirty, true)
    await p.save(); assert.deepEqual(calls, [{ goal: '原提交目标' }, { goal: '原目标' }]); assert.deepEqual(rows[0].extras, { external: true })
  } finally { gate.resolve(); await pending; c.dispose() }
})

test('不同作品各自保存，旧A只更新A记录不选B不清B稿不toast，返回A仍单飞', async () => {
  const gate = wait(); const rows = { a: [character()], b: [character('b-character', 'b')] }, calls = []
  const c = await mounted({ listEntities: async (id, path) => path === 'characters' ? structuredClone(rows[id]) : [], updateEntity: async (id, _path, itemId, payload) => {
    calls.push(id); if (id === 'a') await gate.promise; Object.assign(rows[id][0], payload); return structuredClone(rows[id][0])
  } })
  let pending
  try {
    await edit(c); vm(c).form.goal = 'A原提交'; pending = vm(c).save(); vm(c).form.goal = 'A后来稿'; await enter(c, 'b'); await edit(c, 'b-character')
    vm(c).form.goal = 'B明确提交'; await vm(c).save(); vm(c).form.goal = 'B当前未存稿'; const messages = c.messages.length
    gate.resolve(); await pending; assert.equal(vm(c).editingId, 'b-character'); assert.equal(vm(c).form.goal, 'B当前未存稿'); assert.equal(c.messages.length, messages)
    await enter(c, 'a'); await edit(c); assert.equal(vm(c).form.goal, 'A后来稿'); assert.equal(JSON.parse(vm(c).formBaseline).goal, 'A原提交'); assert.deepEqual(calls, ['a', 'b'])
  } finally { gate.resolve(); await pending; c.dispose() }
})

test('旧list不能推翻后来check所得删除事实，已删来源只读复制不复活或改为POST', async () => {
  const old = wait(); let phase = 'initial', writes = 0
  const c = await mounted({ listEntities: async (_id, path) => { if (path !== 'characters') return []; return phase === 'old' ? old.promise : phase === 'deleted' ? [] : [character()] }, updateEntity: async () => { writes++; throw new Error('未知原提交') } })
  try {
    await edit(c); const p = vm(c); p.form.goal = '原提交'; await p.save(); p.form.goal = '删除后仍需保留的后来稿'
    phase = 'old'; const reading = p.loadAll(); phase = 'deleted'; await p.checkEntity(); assert.equal(p.entityState.exists, false); old.resolve([character()]); await reading
    assert.equal(p.entityState.exists, false); await p.save(); assert.equal(writes, 1); assert.equal(p.form.goal, '删除后仍需保留的后来稿')
    assert.equal(p.entityState.original.goal, '原提交'); assert.equal(p.editingId, null)
    await enter(c, 'b'); assert.equal(vm(c).otherEntityDrafts.length, 1); const copy = inputProps(c, `其他源当前设定 · ${vm(c).otherEntityDrafts[0].key}`)
    assert.equal(JSON.parse(copy.value).goal, '删除后仍需保留的后来稿')
  } finally { old.resolve([character()]); c.dispose() }
})

test('干净原记录跨真实App重挂采用外部新事实，未保存作者稿不被列表刷新替换', async () => {
  let rows = [character()]; const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [] })
  try {
    await edit(c); await leave(c); rows = [character('a-character', 'a', { goal: '外部保存目标' })]; c.vm.onEnterWorkbench('a'); await settle(); await edit(c)
    assert.equal(vm(c).form.goal, '外部保存目标'); assert.equal(vm(c).entityDirty, false)
    vm(c).form.goal = '作者独立未存稿'; rows = [character('a-character', 'a', { goal: '外部再变' })]; await vm(c).loadAll()
    assert.equal(vm(c).form.goal, '作者独立未存稿'); assert.equal(JSON.parse(vm(c).formBaseline).goal, '外部保存目标'); assert.equal(vm(c).entityDirty, true)
  } finally { c.dispose() }
})

test('实体待保存和未知在Bclean及真实Settings卸载后仍提醒整页离开，UI导航不写', async () => {
  for (const outcome of ['pending', 'unknown']) {
    const gate = wait(); const c = await mounted({ updateEntity: async () => outcome === 'pending' ? gate.promise : Promise.reject(new Error('未知')) }); let pending
    try {
      await edit(c); vm(c).form.goal = '原提交'; pending = vm(c).save(); if (outcome === 'unknown') await pending
      await enter(c, 'b'); assert.equal(vm(c).dirty, false)
      let prevented = false; for (const listener of c.listeners.get('beforeunload') ?? []) listener({ preventDefault() { prevented = true } }); assert.equal(prevented, true)
      await leave(c); prevented = false; for (const listener of c.listeners.get('beforeunload') ?? []) listener({ preventDefault() { prevented = true } }); assert.equal(prevented, true)
      assert.equal(c.requests.filter(row => row.name === 'updateEntity').length, 1)
    } finally { gate.resolve(character('a-character', 'a', { goal: '原提交' })); await pending; c.dispose() }
  }
})

test('设定clipboard拒绝时原生Naive只读回退精确保字段原文，原提交及其他源复制入口可达', async () => {
  const gate = wait(), raw = ' \n原表单😀\0\r\n ', later = '后来稿😀\0'
  const c = await mounted({ updateEntity: async () => gate.promise }, { clipboardError: true }); let pending
  try {
    await edit(c); const p = vm(c); p.form.goal = raw; p.form.extras = ' { "b": 2, "a": 1 } '; pending = p.save(); p.form.goal = later; await settle()
    assert.equal(inputProps(c, '当前设定表单 · goal').value, later); assert.equal(JSON.parse(inputProps(c, '原提交设定表单').value).goal, raw)
    await p.copyEntity(true); await settle(); const original = inputProps(c, '手动复制原提交设定'); assert.equal(JSON.parse(original.value).goal, raw); assert.equal(JSON.parse(original.value).extras, ' { "b": 2, "a": 1 } ')
    const html = await renderToString(createSSRApp({ render: () => h(NInput, original) }))
    assert.match(html, /aria-label="手动复制原提交设定"/); assert.match(html, /readonly/)
    await p.copyEntity(); await settle(); assert.equal(JSON.parse(inputProps(c, '手动复制当前设定').value).goal, later)
    await enter(c, 'b'); const other = vm(c).otherEntityDrafts[0]; await vm(c).copyEntity(true, other); await settle(); assert.equal(JSON.parse(inputProps(c, '手动复制原提交设定').value).goal, raw)
    assert.equal(vm(c).dirty, false)
  } finally { gate.resolve(character('a-character', 'a', { goal: raw, extras: { b: 2, a: 1 } })); await pending; c.dispose() }
})

test('目录GET等待期间作者字段editback及同义JSON原文选择不被fresh事实误清', async () => {
  const gate = wait(); let held = false
  const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? held ? gate.promise : [character()] : [] })
  try {
    await edit(c); const p = vm(c); held = true; const reading = p.loadAll()
    p.form.goal = '作者另一目标'; p.form.goal = '原目标'; p.form.extras = '  { }  '
    assert.equal(p.entityDirty, false)
    gate.resolve([character('a-character', 'a', { goal: '外部fresh目标', extras: { external: true } })]); await reading
    assert.equal(p.form.goal, '原目标'); assert.equal(p.form.extras, '  { }  ')
    assert.equal(JSON.parse(p.formBaseline).goal, '原目标'); assert.equal(p.entityDirty, false)
  } finally { gate.resolve([character()]); c.dispose() }
})

test('unknown明确采用当前资料不把未sent外部新字段变成作者修改，后续仍只写明确后来字段', async () => {
  const rows = [character()], calls = []; const c = await mounted({ listEntities: async (_id, path) => path === 'characters' ? structuredClone(rows) : [],
    updateEntity: async (_id, _path, id, payload) => { calls.push(payload); Object.assign(rows[0], payload); if (calls.length === 1) throw Object.assign(new Error('原200回应丢失'), { status: 503 }); return structuredClone(rows[0]) },
  })
  try {
    await edit(c); const p = vm(c); p.form.goal = '原提交目标'; await p.save(); p.form.goal = '后来明确目标'
    rows[0].role = '其他合法作者新身份'; rows[0].known_info = '当前已保存的新知识'; rows[0].extras = { external: true }
    await p.checkEntity(); p.adoptCurrentEntity(); assert.equal(calls.length, 1); assert.equal(p.form.role, ''); assert.equal(p.form.known_info, '')
    await p.save(); assert.deepEqual(calls, [{ goal: '原提交目标' }, { goal: '后来明确目标' }])
    assert.equal(rows[0].role, '其他合法作者新身份'); assert.equal(rows[0].known_info, '当前已保存的新知识'); assert.deepEqual(rows[0].extras, { external: true })
    p.form.extras = ' { "external" : true } '; assert.equal(p.entityDirty, false); await p.save(); assert.equal(calls.length, 2)
    p.form.role = '作者明确新身份'; await p.save(); assert.deepEqual(calls[2], { role: '作者明确新身份' })
    assert.equal(rows[0].role, '作者明确新身份'); assert.equal(rows[0].known_info, '当前已保存的新知识'); assert.deepEqual(rows[0].extras, { external: true })
  } finally { c.dispose() }
})
