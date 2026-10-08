import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencies = Object.fromEntries(await Promise.all([
  ['../types', '../src/types.ts'], ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/asyncScope', '../src/utils/asyncScope.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const { ENTITY_TYPES } = await import(dependencies['../types'])
const flush = async () => { for (let index = 0; index < 20; index++) { await Promise.resolve(); await nextTick() } }
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
const entity = (type = 'character', novelId = 'novel-a', patch = {}) => {
  const config = ENTITY_TYPES.find((item) => item.type === type)
  return { id: `${novelId}-${type}`, novel_id: novelId, ...Object.fromEntries(config.fields.map((field) => [field.key,
    field.type === 'json' ? field.key === 'extras' ? { 阵营: '原有', nested: { b: 2, a: 1 } } : ['first', 'second']
      : field.type === 'select' ? 'planted' : field.required ? `${type}原名` : `${field.key}原值`,
  ])), ...patch }
}
let sequence = 0
async function panel(options = {}) {
  const props = reactive({ novelId: 'novel-a', ...options.props }), requests = [], messages = [], hooks = {}, listeners = new Map(), emitted = []
  let confirmed = true
  globalThis.window = { confirm: () => confirmed, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) }
  const database = options.database ?? new Map(['novel-a', 'novel-b'].flatMap((id) => ENTITY_TYPES.map((type) => [`${id}/${type.apiPath}`, [entity(type.type, id)]])))
  const implementations = {
    listEntities: async (id, path) => structuredClone(database.get(`${id}/${path}`) ?? []),
    listNovels: async () => [{ id: 'novel-a', style_anchor: '' }, { id: 'novel-b', style_anchor: '' }],
    listChapterMetadata: async () => [],
    updateEntity: async (novelId, path, id, payload) => {
      const found = database.get(`${novelId}/${path}`)?.find((item) => item.id === id)
      if (!found) throw Object.assign(new Error('不存在'), { status: 404 })
      Object.assign(found, payload); return structuredClone(found)
    },
    createEntity: async (novelId, path, payload) => {
      const saved = { id: `${novelId}-created-${sequence}`, novel_id: novelId, ...payload }
      database.get(`${novelId}/${path}`).push(saved); return structuredClone(saved)
    },
    ...options.api,
  }
  const key = `__settingsFieldSave${++sequence}`
  globalThis[key] = {
    api: Object.fromEntries(Object.entries(implementations).map(([name, fn]) => [name, (...args) => { requests.push({ name, args: structuredClone(args) }); return fn(...args) }])),
    useMessage: () => Object.fromEntries(['success', 'error', 'warning', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const name of ['NButton', 'NEmpty', 'NForm', 'NFormItem', 'NInput', 'NList', 'NListItem', 'NMenu', 'NModal', 'NSelect']) globalThis[key][name] = name.toLowerCase()
  const descriptor = parse(await readFile(new URL('../src/components/SettingsPanel.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: `settings-field-${sequence}` })
  let source = `const __harness = globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const pageDependencies = { ...dependencies, '../utils/styleAnchorDraft': moduleUrl(compile(
    (await readFile(new URL('../src/utils/styleAnchorDraft.ts', import.meta.url), 'utf8'))
      .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  ) + `\n// style-page-${sequence}`) }
  for (const [name, url] of Object.entries(pageDependencies)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source)))).default
  const scope = effectScope(), vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  await hooks.mount?.(); await flush()
  const template = compileTemplate({ source: descriptor.template.content, id: `settings-field-${sequence}`, filename: 'SettingsPanel.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendered = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
  return { vm, props, requests, messages, database, listeners, emitted,
    edit(type = 'character') { vm.changeType(type); vm.startEdit(vm.allEntities.value[type][0]) },
    render: () => rendered.render({}, [], props, proxyRefs(vm), {}, {}), confirm(value) { confirmed = value },
    dispose() { hooks.unmount?.(); scope.stop(); delete globalThis[key] },
  }
}
const writes = (context) => context.requests.filter((request) => request.name === 'updateEntity' || request.name === 'createEntity')
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

test('只补旧伏笔内容不覆Tracking已消费状态关联，响应真实完整字段安全同步且下次仍只补内容', async () => {
  const context = await panel(); context.edit('foreshadowing')
  const actual = context.database.get('novel-a/foreshadowings')[0]
  Object.assign(actual, { status: 'consumed', consumed_chapter_id: 'real-chapter-2', related_characters: ['real-character'], planted_chapter_id: 'real-chapter-1' })
  context.vm.form.value.content = '作者仅补描述'; await context.vm.save()
  assert.deepEqual(writes(context)[0].args[3], { content: '作者仅补描述' })
  assert.equal(actual.status, 'consumed'); assert.deepEqual(actual.related_characters, ['real-character']); assert.equal(actual.consumed_chapter_id, 'real-chapter-2')
  assert.equal(context.vm.form.value.status, 'consumed'); assert.equal(context.vm.form.value.consumed_chapter_id, 'real-chapter-2'); assert.equal(context.vm.entityDirty.value, false)
  context.vm.form.value.content = '第二次仅补描述'; await context.vm.save()
  assert.deepEqual(writes(context)[1].args[3], { content: '第二次仅补描述' }); context.dispose()
})

test('人物仅改目标保留新已知信息性格及extras，完整返回采用后仍不会夹带旧字段', async () => {
  const context = await panel(); context.edit()
  const actual = context.database.get('novel-a/characters')[0]
  Object.assign(actual, { personality: '已变得果断', known_info: '已获知真相', extras: { 阵营: '已更新', nested: { a: 3 } } })
  context.vm.form.value.goal = '作者的新目标'; await context.vm.save()
  assert.deepEqual(writes(context)[0].args[3], { goal: '作者的新目标' })
  assert.equal(actual.personality, '已变得果断'); assert.equal(actual.known_info, '已获知真相'); assert.deepEqual(actual.extras, { 阵营: '已更新', nested: { a: 3 } })
  assert.equal(context.vm.form.value.known_info, '已获知真相'); assert.deepEqual(JSON.parse(context.vm.form.value.extras), actual.extras)
  context.vm.form.value.goal = '再补目标'; await context.vm.save()
  assert.deepEqual(writes(context)[1].args[3], { goal: '再补目标' }); context.dispose()
})

test('保存等待的新输入保留，sent字段推进冻结基线，未sent旧值不成为下次伪修改', async () => {
  const response = deferred(); let count = 0
  const database = new Map([['novel-a/foreshadowings', [entity('foreshadowing')]]])
  const context = await panel({ database, api: { updateEntity: async (novelId, path, id, payload) => {
    const row = database.get(`${novelId}/${path}`).find((item) => item.id === id)
    Object.assign(row, payload, { status: 'consumed', related_characters: ['fresh-character'], consumed_chapter_id: 'fresh-chapter' })
    const saved = structuredClone(row); if (++count === 1) await response.promise; return saved
  } } }); context.edit('foreshadowing')
  const oldStatus = context.vm.form.value.status
  context.vm.form.value.content = '第一次提交'; const saving = context.vm.save()
  context.vm.form.value.content = '等待期间又补内容'
  response.resolve(); await saving
  assert.equal(context.vm.form.value.content, '等待期间又补内容'); assert.equal(context.vm.form.value.status, oldStatus)
  assert.equal(context.vm.entityDirty.value, true)
  assert.equal(JSON.parse(context.vm.formBaseline.value).content, '第一次提交')
  assert.equal(JSON.parse(context.vm.formBaseline.value).status, oldStatus)
  await context.vm.save()
  assert.deepEqual(writes(context).map((request) => request.args[3]), [{ content: '第一次提交' }, { content: '等待期间又补内容' }])
  assert.equal(context.vm.form.value.status, 'consumed'); assert.equal(context.vm.entityDirty.value, false); context.dispose()
})

test('在途把sent字段改回旧值仍是未保存修改，下次显式保存确实只提交改回字段', async () => {
  const response = deferred(); let count = 0
  const database = new Map([['novel-a/characters', [entity()]]])
  const context = await panel({ database, api: { updateEntity: async (novelId, path, id, payload) => {
    const row = database.get(`${novelId}/${path}`).find((item) => item.id === id)
    Object.assign(row, payload, { known_info: '另一窗口已更新', extras: { 新事实: true } })
    const saved = structuredClone(row); if (++count === 1) await response.promise; return saved
  } } }); context.edit()
  const oldGoal = context.vm.form.value.goal, oldInfo = context.vm.form.value.known_info
  context.vm.form.value.goal = '本次提交目标'; const saving = context.vm.save()
  context.vm.form.value.goal = oldGoal
  response.resolve(); await saving
  assert.equal(context.vm.form.value.goal, oldGoal); assert.equal(context.vm.form.value.known_info, oldInfo); assert.equal(context.vm.entityDirty.value, true)
  assert.equal(JSON.parse(context.vm.formBaseline.value).goal, '本次提交目标')
  let prevented = false; context.listeners.get('beforeunload')({ preventDefault() { prevented = true } }); assert.equal(prevented, true)
  await context.vm.save()
  assert.deepEqual(writes(context).map((request) => request.args[3]), [{ goal: '本次提交目标' }, { goal: oldGoal }]); context.dispose()
})

test('保存期间作者改变未sent字段会保留为明确意图，未改的其它新事实不会混入下一次提交', async () => {
  const response = deferred(); let count = 0
  const database = new Map([['novel-a/characters', [entity()]]])
  const context = await panel({ database, api: { updateEntity: async (novelId, path, id, payload) => {
    const row = database.get(`${novelId}/${path}`).find((item) => item.id === id)
    Object.assign(row, payload, { extras: { 新事实: '应保留' } })
    const saved = structuredClone(row); if (++count === 1) await response.promise; return saved
  } } }); context.edit()
  context.vm.form.value.goal = '提交目标'; const saving = context.vm.save()
  context.vm.form.value.known_info = '作者等待期间改了已知信息'
  response.resolve(); await saving
  await context.vm.save()
  assert.deepEqual(writes(context).map((request) => request.args[3]), [{ goal: '提交目标' }, { known_info: '作者等待期间改了已知信息' }])
  assert.deepEqual(database.get('novel-a/characters')[0].extras, { 新事实: '应保留' }); context.dispose()
})

test('JSON缩进对象键重排及数字表示同义不脏不发PUT，实际数组顺序和嵌套值变化会提交', async () => {
  const context = await panel(); context.edit()
  context.vm.form.value.extras = ' { "nested": { "a": 1.0, "b": 2e0 }, "阵营": "原有" } '
  assert.equal(context.vm.entityDirty.value, false)
  const saveButton = nodes(context.render(), (node) => node.type === 'nbutton' && textOf(node) === '保存')[0]
  assert.equal(saveButton.props.disabled, true)
  await context.vm.save(); assert.equal(writes(context).length, 0); assert.equal(context.messages.some((row) => row.type === 'success'), false)
  assert.match(context.messages[0].text, /没有新的修改.*未提交/)
  context.vm.form.value.extras = '{"nested":{"a":7,"b":2},"阵营":"原有"}'; await context.vm.save()
  assert.deepEqual(writes(context)[0].args[3], { extras: { nested: { a: 7, b: 2 }, 阵营: '原有' } })
  context.edit('world_setting'); context.vm.form.value.tags = '[ "second", "first" ]'; await context.vm.save()
  assert.deepEqual(writes(context)[1].args[3], { tags: ['second', 'first'] }); context.dispose()
})

test('空JSON输入与空容器同义，明确清空对象数组字符串和状态只发送这些字段', async () => {
  const context = await panel(); context.edit()
  context.vm.form.value.extras = ' '; context.vm.form.value.known_info = ''; await context.vm.save()
  assert.deepEqual(writes(context)[0].args[3], { known_info: '', extras: {} })
  context.vm.form.value.extras = '{}'; assert.equal(context.vm.entityDirty.value, false); await context.vm.save(); assert.equal(writes(context).length, 1)
  context.edit('foreshadowing'); context.vm.form.value.related_characters = ''; context.vm.form.value.consumed_chapter_id = ''; context.vm.form.value.status = 'suspended'; await context.vm.save()
  assert.deepEqual(writes(context)[1].args[3], { status: 'suspended', related_characters: [], consumed_chapter_id: '' })
  context.vm.form.value.related_characters = '[]'; assert.equal(context.vm.entityDirty.value, false); await context.vm.save(); assert.equal(writes(context).length, 2)
  context.dispose()
})

test('六类编辑只发送指定字段，新建仍发送完整字段，nullable ref清空而required端点拒绝null', async () => {
  for (const type of ENTITY_TYPES) {
    const context = await panel(); context.edit(type.type)
    const field = type.fields.find((field) => field.type === 'text' && !field.required) ?? type.fields.find((field) => field.type === 'textarea' && !field.required)
    context.vm.form.value[field.key] = `只补${field.key}`; await context.vm.save()
    assert.deepEqual(writes(context)[0].args[3], { [field.key]: `只补${field.key}` })
    context.vm.startCreate()
    for (const field of type.fields.filter((field) => field.required)) context.vm.form.value[field.key] = `新${field.key}`
    await context.vm.save()
    assert.deepEqual(Object.keys(writes(context)[1].args[2]).sort(), type.fields.map((field) => field.key).sort())
    context.dispose()
  }
  const context = await panel(); context.edit('location'); context.vm.form.value.parent_id = null; await context.vm.save()
  assert.deepEqual(writes(context)[0].args[3], { parent_id: '' }); assert.equal(context.database.get('novel-a/locations')[0].parent_id, '')
  context.edit('timeline_event'); context.vm.form.value.cause_id = ''; await context.vm.save(); assert.deepEqual(writes(context)[1].args[3], { cause_id: '' })
  context.edit('relationship'); context.vm.form.value.subject_id = null; await context.vm.save()
  assert.equal(writes(context).length, 2); assert.match(context.messages.at(-1).text, /主体实体.*必填/); context.dispose()
})

test('JSON null或非匹配容器被明确拒绝，解析错误与API拒绝都保留原输入和有效基线', async () => {
  for (const [type, field, raw] of [['character', 'extras', 'null'], ['character', 'extras', '[]'], ['character', 'extras', '123'], ['world_setting', 'tags', '{}'], ['timeline_event', 'participants', 'null'], ['foreshadowing', 'related_characters', '"string"'], ['character', 'extras', '{invalid']]) {
    const context = await panel(); context.edit(type)
    const baseline = context.vm.formBaseline.value; context.vm.form.value[field] = raw
    await context.vm.save(); assert.equal(writes(context).length, 0); assert.equal(context.vm.form.value[field], raw)
    assert.equal(context.vm.formBaseline.value, baseline); assert.equal(context.vm.entityDirty.value, true); assert.equal(context.messages.at(-1).type, 'error'); context.dispose()
  }
  const context = await panel({ api: { updateEntity: async () => { throw Object.assign(new Error('保存被拒绝'), { status: 400 }) } } }); context.edit()
  const baseline = context.vm.formBaseline.value; context.vm.form.value.goal = '失败仍保留'; await context.vm.save()
  assert.equal(context.vm.formBaseline.value, baseline); assert.equal(context.vm.form.value.goal, '失败仍保留'); assert.equal(context.vm.entityDirty.value, true)
  assert.deepEqual(writes(context)[0].args[3], { goal: '失败仍保留' }); assert.equal(context.vm.saving.value, false); context.dispose()
})

test('新建挂起时作者新输入不会丢，创建成功绑定原ID后下一次只更新新改字段', async () => {
  const response = deferred()
  const context = await panel({ api: { createEntity: async (novelId, _path, payload) => { await response.promise; return { id: 'created', novel_id: novelId, ...payload } },
    updateEntity: async (novelId, _path, id, payload) => ({ ...entity(), id, novel_id: novelId, ...payload }) } })
  context.vm.form.value.name = '首次创建姓名'
  const creating = context.vm.save(); context.vm.form.value.goal = '创建等待的新目标'
  response.resolve(); await creating
  assert.equal(context.vm.editingId.value, 'created'); assert.equal(context.vm.form.value.goal, '创建等待的新目标'); assert.equal(context.vm.entityDirty.value, true)
  await context.vm.save(); assert.equal(writes(context).filter((request) => request.name === 'createEntity').length, 1)
  assert.deepEqual(writes(context)[1].args[3], { goal: '创建等待的新目标' }); context.dispose()
})

test('保存200后目录失败保留实际完整实体，保存前旧list不能复活旧事实或覆盖作者输入', async () => {
  const staleRead = deferred(); let reads = 0
  const context = await panel({ api: { listEntities: async (id, path) => {
    if (path !== 'characters') return []
    reads += 1; if (reads === 2) return staleRead.promise; if (reads > 2) throw new Error('列表读取失败'); return [entity('character', id)]
  }, updateEntity: async (novelId, _path, id, payload) => ({ ...entity(), id, novel_id: novelId, ...payload, known_info: '实际保存时的新事实' }) } }); context.edit()
  const reading = context.vm.loadAll(); context.vm.form.value.goal = '本次目标'; await context.vm.save()
  staleRead.resolve([entity()]); await reading
  assert.equal(context.vm.allEntities.value.character[0].goal, '本次目标'); assert.equal(context.vm.allEntities.value.character[0].known_info, '实际保存时的新事实')
  assert.equal(context.vm.form.value.known_info, '实际保存时的新事实'); assert.equal(context.vm.entityDirty.value, false)
  assert.ok(context.messages.some((row) => /设定已保存.*列表刷新失败/.test(row.text))); assert.equal(context.messages.some((row) => row.type === 'error'), false)
  context.dispose()
})

test('作品或实体身份ABA及卸载后的旧保存成功失败静默，不推进当前基线和新loading', async () => {
  for (const transition of ['novel', 'entity', 'unmount']) for (const outcome of ['success', 'failure']) {
    const response = deferred()
    const context = await panel({ api: { updateEntity: async () => response.promise } }); context.edit()
    context.vm.form.value.goal = '旧请求目标'; const saving = context.vm.save()
    if (transition === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await flush(); context.edit() }
    else if (transition === 'entity') { context.edit('location'); context.edit() }
    else context.dispose()
    context.vm.form.value.goal = '新的作者输入'
    const baseline = context.vm.formBaseline.value, calls = context.requests.length
    if (outcome === 'success') response.resolve(entity('character', 'novel-a', { goal: '旧请求目标' })); else response.reject(new Error('旧保存错误'))
    await saving
    assert.equal(context.vm.form.value.goal, '新的作者输入'); assert.equal(context.vm.formBaseline.value, baseline)
    assert.equal(context.messages.length, 0); assert.equal(context.requests.length, calls)
    if (transition !== 'unmount') context.dispose()
  }
})
