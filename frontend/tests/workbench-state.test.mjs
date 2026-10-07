import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencyUrls = Object.fromEntries(await Promise.all([
  ['../types', '../src/types.ts'],
  ['../utils/chapter', '../src/utils/chapter.ts'],
  ['../utils/wordLimits', '../src/utils/wordLimits.ts'],
  ['../utils/asyncScope', '../src/utils/asyncScope.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const settle = async () => { for (let index = 0; index < 20; index += 1) { await Promise.resolve(); await nextTick() } }
const wait = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const character = (novelId = 'novel-a') => ({ id: `${novelId}-character`, novel_id: novelId, name: `${novelId}人物`, extras: { 阵营: '原有设定' } })
const outlineNode = (novelId = 'novel-a', patch = {}) => ({ id: `${novelId}-volume`, novel_id: novelId, level: 'volume', parent_id: '', sort_order: 0, title: `${novelId}卷`, summary: '原有概要', ...patch })
const book = (novelId = 'novel-a') => ({ title: novelId, chapter_count: 1, word_count: 10, draft_count: 0, finalized_count: 1, volumes: [], chapters: [] })
const preflight = (patch = {}) => ({ chapter_count: 1, word_count: 10, ready: true, draft_chapters: [], unbound_chapters: [], unresolved_foreshadowings: [], rule_conflicts: [], ...patch })
const outlineDraft = (title = '第一卷') => ({ title, summary: '卷概要', chapters: [{ title: '第一章', summary: '章概要' }], acts: [] })
const settleResult = (patch = {}) => ({ summary: '定稿摘要', suggestions: [{ type: 'character', data: { name: '章节人物' } }], ...patch })
let sequence = 0

async function panel(name, options = {}) {
  const props = reactive({ novelId: 'novel-a', ...options.props })
  const messages = [], emitted = [], requests = [], hooks = {}, listeners = new Map()
  let confirmResult = true
  globalThis.window = {
    confirm: () => confirmResult,
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name) => listeners.delete(name),
  }
  const defaults = {
    listNovels: async () => ['novel-a', 'novel-b'].map((id) => ({ id, title: id, style_anchor: `${id}文风` })),
    listEntities: async (novelId, apiPath) => apiPath === 'characters' ? [character(novelId)] : [],
    listChapters: async (novelId) => [{ id: `${novelId}-chapter`, novel_id: novelId, seq: 1, title: novelId, content: `${novelId}章节正文`, directive: '', summary: '', status: 'draft', outline_node_id: '' }],
    createEntity: async (novelId, apiPath, payload) => ({ ...payload, id: `${novelId}-created` }),
    updateEntity: async (novelId, apiPath, id, payload) => ({ ...payload, id }),
    deleteEntity: async () => {}, updateNovel: async () => {},
    createNovel: async (payload) => ({ id: 'created-novel', ...payload }),
    listOutline: async (novelId) => [outlineNode(novelId)],
    createOutline: async (novelId, payload) => ({ ...payload, id: `${novelId}-created` }),
    updateOutline: async (novelId, id, payload) => ({ ...payload, id }),
    deleteOutline: async () => {}, reorderOutline: async () => {},
    previewOutline: async () => outlineDraft(), previewMessageOutline: async () => outlineDraft(),
    applyOutline: async () => ({ volume_id: 'saved-volume' }), applyMessageOutline: async () => ({ volume_id: 'saved-volume' }),
    settleChapter: async () => settleResult(), settleApply: async () => ({ created: [], failed: [] }),
    getBookSummary: async (novelId) => book(novelId),
    preflightExport: async () => preflight(), downloadBookFile: async () => new Blob(['章节正文']),
  }
  const implementations = { ...defaults, ...options.api }
  const api = Object.fromEntries(Object.entries(implementations).map(([method, implementation]) => [method, (...args) => {
    requests.push({ method, args: JSON.parse(JSON.stringify(args)) })
    return implementation(...args)
  }]))
  const harness = {
    api,
    streamSSE: async () => {},
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    onMounted: (fn) => { hooks.mount = fn }, onBeforeUnmount: (fn) => { hooks.unmount = fn },
  }
  for (const component of ['NButton', 'NCheckbox', 'NEmpty', 'NForm', 'NFormItem', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NMenu', 'NModal', 'NSelect', 'NSpin', 'NTag', 'NSwitch', 'NTabPane', 'NTabs']) harness[component] = component.toLowerCase()
  globalThis.__workbenchHarness = harness
  const original = await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')
  const descriptor = parse(original).descriptor
  const compiled = compileScript(descriptor, { id: `workbench-${sequence}` })
  let source = 'const __harness = globalThis.__workbenchHarness\n' + compiled.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api(?:, streamSSE)? \} from ['"]\.\.\/api['"]/g, 'const { api, streamSSE } = __harness')
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1 = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  for (const [name, url] of Object.entries(dependencyUrls)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
  const component = (await import(moduleUrl(compile(source) + `\n// ${++sequence}`))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  await hooks.mount?.(); await settle()
  const template = compileTemplate({ source: descriptor.template.content, id: `workbench-${sequence}`, filename: `${name}.vue`, compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.deepEqual(template.errors, [])
  const templateModule = await import(moduleUrl(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${sequence}`))
  const render = () => templateModule.render({}, [], props, proxyRefs(vm), {}, {})
  const dispose = () => { hooks.unmount?.(); scope.stop() }
  return { vm, props, messages, requests, emitted, listeners, hooks, dispose, render, confirm: (value) => { confirmResult = value } }
}

test('设定真实 SFC 切作品清编辑身份，未确认换类型/实体/新建均保留作者输入', async () => {
  const context = await panel('SettingsPanel')
  const { vm, props } = context
  vm.startEdit(character())
  vm.form.value.name = '尚未保存的人物'
  context.confirm(false)
  vm.changeType('location'); vm.startCreate(); vm.startEdit(character())
  assert.equal(vm.selectedType.value, 'character')
  assert.equal(vm.form.value.name, '尚未保存的人物')
  assert.equal(vm.editingId.value, 'novel-a-character')
  assert.equal(vm.dirty.value, true)
  props.novelId = 'novel-b'; await settle()
  assert.equal(vm.editingId.value, null)
  assert.equal(vm.form.value.name, '')
  vm.form.value.name = 'B 的新人物'; await vm.save()
  const created = context.requests.find((request) => request.method === 'createEntity')
  assert.equal(created.args[0], 'novel-b')
  assert.equal(context.requests.some((request) => request.method === 'updateEntity'), false)
  context.dispose()
})

test('设定真实 SFC A→B→A 舍弃旧六类实体、文风和章节结果，全部请求固定原作品', async () => {
  const entityPending = wait(), metaPending = wait(), chapterPending = wait()
  let charactersCalls = 0, novelsCalls = 0, chaptersCalls = 0
  const context = await panel('SettingsPanel', { api: {
    listEntities: async (id, path) => path === 'characters'
      ? (++charactersCalls === 1 ? entityPending.promise : [character(id)]) : [],
    listNovels: async () => ++novelsCalls === 1 ? metaPending.promise : [{ id: 'novel-a', style_anchor: '当前文风' }],
    listChapters: async (id) => ++chaptersCalls === 1 ? chapterPending.promise : [{ id: `${id}-current`, content: '当前正文' }],
  } })
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  entityPending.resolve([character('旧人物')]); metaPending.resolve([{ id: 'novel-a', style_anchor: '旧文风' }]); chapterPending.resolve([{ id: 'old-chapter', content: '旧正文' }])
  await settle()
  assert.equal(context.vm.allEntities.value.character[0].id, 'novel-a-character')
  assert.equal(context.vm.styleAnchor.value, '当前文风')
  assert.equal(context.vm.chapters.value[0].id, 'novel-a-current')
  assert.equal(context.requests.filter((request) => request.method === 'listEntities').slice(0, 6).every((request) => request.args[0] === 'novel-a'), true)
  context.dispose()
})

test('设定真实 SFC 保存后续编辑仍为脏数据，新建获得身份后第二次保存更新同一实体', async () => {
  const pending = wait()
  const context = await panel('SettingsPanel', { api: { createEntity: async () => pending.promise } })
  context.vm.form.value.name = '第一次名字'
  const saving = context.vm.save()
  context.vm.form.value.name = '保存时又改的名字'
  pending.resolve({ id: 'created-character', name: '第一次名字' }); await saving
  assert.equal(context.vm.form.value.name, '保存时又改的名字')
  assert.equal(context.vm.editingId.value, 'created-character')
  assert.equal(context.vm.entityDirty.value, true)
  await context.vm.save()
  const updates = context.requests.filter((request) => request.method === 'updateEntity')
  assert.equal(updates.length, 1)
  assert.equal(updates[0].args[2], 'created-character')
  assert.equal(updates[0].args[3].name, '保存时又改的名字')
  assert.equal(context.requests.filter((request) => request.method === 'createEntity').length, 1)
  assert.equal(context.vm.dirty.value, false)
  context.dispose()
})

test('设定真实 SFC 清空 JSON 会发送空结构，文风保存中新编辑不会被误标已保存', async () => {
  const pending = wait()
  const context = await panel('SettingsPanel', { api: { updateNovel: async () => pending.promise } })
  context.vm.startEdit(character())
  context.vm.form.value.extras = ''; await context.vm.save()
  const update = context.requests.find((request) => request.method === 'updateEntity')
  assert.deepEqual(update.args[3].extras, {})
  context.vm.styleAnchor.value = '提交的文风'
  const saving = context.vm.saveAnchor()
  context.vm.styleAnchor.value = '新修改的文风'
  pending.resolve({}); await saving
  assert.equal(context.vm.anchorBaseline.value, '提交的文风')
  assert.equal(context.vm.styleAnchor.value, '新修改的文风')
  assert.equal(context.vm.dirty.value, true)
  let prevented = false
  context.listeners.get('beforeunload')({ preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  context.dispose()
})

test('设定真实 SFC 旧保存成功/失败与卸载后读取均不影响当前身份、loading 和提示', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await panel('SettingsPanel', { api: { createEntity: async () => pending.promise } })
    context.vm.form.value.name = 'A 新人物'; const saving = context.vm.save()
    context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
    context.vm.form.value.name = '返回 A 后的新输入'
    if (outcome === 'success') pending.resolve({ id: 'old-created' })
    else pending.reject(new Error('旧错误'))
    await saving
    assert.equal(context.vm.editingId.value, null)
    assert.equal(context.vm.form.value.name, '返回 A 后的新输入')
    assert.equal(context.messages.length, 0)
    assert.equal(context.vm.saving.value, false)
    context.dispose()
  }
  const pending = wait()
  const context = await panel('SettingsPanel', { api: { listEntities: async () => pending.promise } })
  context.dispose(); pending.resolve([character('卸载后人物')]); await settle()
  assert.equal(context.vm.allEntities.value.character.length, 0)
  assert.equal(context.messages.length, 0)
})

test('设定真实 SFC 延迟读取文风只更新持久化基线，保留新编辑及已经确认的保存', async () => {
  const pending = wait()
  const context = await panel('SettingsPanel', { api: { listNovels: async () => pending.promise } })
  context.vm.styleAnchor.value = '先输入再清空'; context.vm.styleAnchor.value = ''
  pending.resolve([{ id: 'novel-a', style_anchor: '原本已存基准' }]); await settle()
  assert.equal(context.vm.styleAnchor.value, '')
  assert.equal(context.vm.anchorBaseline.value, '原本已存基准')
  assert.equal(context.vm.dirty.value, true)
  context.dispose()
  const oldMeta = wait()
  const saved = await panel('SettingsPanel', { api: { listNovels: async () => oldMeta.promise } })
  saved.vm.styleAnchor.value = '已确认的新基准'; await saved.vm.saveAnchor()
  oldMeta.resolve([{ id: 'novel-a', style_anchor: '过时的服务端快照' }]); await settle()
  assert.equal(saved.vm.anchorBaseline.value, '已确认的新基准')
  assert.equal(saved.vm.styleAnchor.value, '已确认的新基准')
  assert.equal(saved.vm.dirty.value, false)
  saved.dispose()
})

test('设定真实 SFC 删除成功后刷新失败仍移除已删记录，旧读取不能复活且保留其他设定', async () => {
  const pending = wait(), original = character(), remaining = { ...character(), id: 'remaining-character' }
  let phase = 'initial'
  const context = await panel('SettingsPanel', { api: { listEntities: async (id, path) => {
    if (path === 'characters') {
      if (phase === 'old') return pending.promise
      if (phase === 'refresh') throw new Error('读取失败')
      return [original, remaining]
    }
    return path === 'locations' ? [{ id: 'remaining-location', name: '保留地点' }] : []
  } } })
  context.vm.startEdit(original)
  phase = 'old'; const oldReading = context.vm.loadAll()
  phase = 'refresh'; await context.vm.remove()
  assert.deepEqual(context.vm.allEntities.value.character.map((item) => item.id), ['remaining-character'])
  assert.equal(context.vm.allEntities.value.location[0].id, 'remaining-location')
  assert.equal(context.vm.editingId.value, null)
  assert.equal(context.messages.length, 1)
  assert.equal(context.messages[0].type, 'warning')
  assert.match(context.messages[0].text, /已删除.*刷新失败/)
  pending.resolve([original, remaining]); await oldReading
  assert.deepEqual(context.vm.allEntities.value.character.map((item) => item.id), ['remaining-character'])
  context.vm.startEdit(original)
  assert.equal(context.vm.editingId.value, null)
  context.dispose()
})

test('设定真实 SFC 删除失败保留记录，跨作品迟到删除不剔除当前作品或提示', async () => {
  const failed = await panel('SettingsPanel', { api: { deleteEntity: async () => { throw new Error('删除失败') } } })
  failed.vm.startEdit(character()); await failed.vm.remove()
  assert.equal(failed.vm.allEntities.value.character[0].id, 'novel-a-character')
  assert.equal(failed.vm.editingId.value, 'novel-a-character')
  assert.equal(failed.messages[0].text, '删除失败')
  failed.dispose()
  const pending = wait()
  const switched = await panel('SettingsPanel', { api: { deleteEntity: async () => pending.promise } })
  switched.vm.startEdit(character()); const deleting = switched.vm.remove()
  switched.props.novelId = 'novel-b'; await settle()
  pending.resolve(); await deleting
  assert.equal(switched.vm.allEntities.value.character[0].id, 'novel-b-character')
  assert.equal(switched.messages.length, 0)
  switched.dispose()
})

test('大纲真实 SFC 关闭、换编辑和浏览器离开保护未保存概要与 AI 预览', async () => {
  const context = await panel('OutlinePanel')
  context.vm.openEdit(outlineNode()); context.vm.form.value.summary = '未保存概要'
  context.confirm(false)
  context.vm.handleModalVisibility(false); context.vm.openCreate('', 'volume')
  assert.equal(context.vm.showModal.value, true)
  assert.equal(context.vm.form.value.summary, '未保存概要')
  assert.equal(context.vm.editingId.value, 'novel-a-volume')
  context.confirm(true); context.vm.handleModalVisibility(false); context.vm.openPlanner()
  assert.equal(context.vm.dirty.value, true)
  context.confirm(false); context.vm.closePlanner()
  assert.equal(context.vm.showPlanner.value, true)
  let prevented = false
  context.listeners.get('beforeunload')({ preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  context.dispose()
})

test('大纲真实 SFC 保存中新编辑保留在弹窗并保留新建身份，不重复新建', async () => {
  const pending = wait()
  const context = await panel('OutlinePanel', { api: { createOutline: async () => pending.promise } })
  context.vm.openCreate('', 'volume'); context.vm.form.value.title = '第一卷'
  const saving = context.vm.save()
  context.vm.form.value.summary = '保存时新写的概要'
  pending.resolve(outlineNode('novel-a', { id: 'created-volume', title: '第一卷' })); await saving
  assert.equal(context.vm.showModal.value, true)
  assert.equal(context.vm.form.value.summary, '保存时新写的概要')
  assert.equal(context.vm.editingId.value, 'created-volume')
  assert.equal(context.vm.dirty.value, true)
  await context.vm.save()
  assert.equal(context.requests.filter((request) => request.method === 'createOutline').length, 1)
  assert.equal(context.requests.find((request) => request.method === 'updateOutline').args[1], 'created-volume')
  assert.equal(context.vm.showModal.value, false)
  context.dispose()
})

test('大纲真实 SFC A→B→A 旧读取、旧保存与旧 AI 应用回调不污染新会话', async () => {
  const loadPending = wait(), savePending = wait()
  let calls = 0
  const context = await panel('OutlinePanel', { api: {
    listOutline: async (id) => ++calls === 1 ? loadPending.promise : [outlineNode(id)],
    createOutline: async () => savePending.promise,
  } })
  context.vm.openPlanner(); const oldApplied = context.vm.plannerApplied.value
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  context.vm.openPlanner(); await oldApplied()
  assert.equal(context.vm.showPlanner.value, true)
  context.vm.closePlanner(); context.vm.openCreate('', 'volume'); context.vm.form.value.title = '原请求卷'
  const saving = context.vm.save()
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  context.vm.openCreate('', 'volume'); context.vm.form.value.title = '新输入卷'
  savePending.resolve(outlineNode('novel-a', { id: 'old-saved' })); loadPending.resolve([outlineNode('旧数据')]); await saving; await settle()
  assert.equal(context.vm.nodes.value[0].id, 'novel-a-volume')
  assert.equal(context.vm.editingId.value, null)
  assert.equal(context.vm.form.value.title, '新输入卷')
  assert.equal(context.vm.showModal.value, true)
  assert.equal(context.messages.length, 0)
  context.dispose()
})

test('大纲真实 SFC 重排/读取失败有提示，卸载后的旧读取错误保持安静', async () => {
  const context = await panel('OutlinePanel', { api: {
    listOutline: async (id) => [outlineNode(id), outlineNode(id, { id: 'second-volume', sort_order: 1 })],
    reorderOutline: async () => { throw new Error('重排失败') },
  } })
  await context.vm.move(outlineNode(), 1)
  assert.equal(context.messages[0].text, '重排失败')
  assert.equal(context.vm.moving.value, false)
  context.dispose()
  const pending = wait()
  const disposed = await panel('OutlinePanel', { api: { listOutline: async () => pending.promise } })
  disposed.dispose(); pending.reject(new Error('迟到读取失败')); await settle()
  assert.equal(disposed.messages.length, 0)
})

test('大纲真实 SFC 删除确认成功后刷新失败仍移除本地旧子树，旧读取不能复活或再编辑重排', async () => {
  const pending = wait(), volume = outlineNode(), other = outlineNode('novel-a', { id: 'remaining-volume' })
  const child = outlineNode('novel-a', { id: 'stale-act', parent_id: volume.id, level: 'act' })
  const grandchild = outlineNode('novel-a', { id: 'stale-chapter', parent_id: child.id, level: 'chapter' })
  const original = [volume, child, grandchild, other]
  let phase = 'initial'
  const context = await panel('OutlinePanel', { api: { listOutline: async () => {
    if (phase === 'old') return pending.promise
    if (phase === 'refresh') throw new Error('读取失败')
    return original
  } } })
  phase = 'old'; const oldReading = context.vm.load()
  phase = 'refresh'; await context.vm.remove(volume)
  assert.deepEqual(context.vm.nodes.value.map((item) => item.id), ['remaining-volume'])
  assert.equal(context.messages.length, 1)
  assert.equal(context.messages[0].type, 'warning')
  assert.match(context.messages[0].text, /已删除.*刷新失败/)
  pending.resolve(original); await oldReading
  assert.deepEqual(context.vm.nodes.value.map((item) => item.id), ['remaining-volume'])
  context.vm.openEdit(volume); await context.vm.move(volume, 1)
  assert.equal(context.vm.showModal.value, false)
  assert.equal(context.requests.some((request) => request.method === 'reorderOutline'), false)
  context.dispose()
})

test('大纲真实 SFC 后端拒绝删除有子节点时不丢本地子树，跨作品迟到成功不污染当前大纲', async () => {
  const volume = outlineNode(), child = outlineNode('novel-a', { id: 'child', parent_id: volume.id, level: 'chapter' })
  const rejected = await panel('OutlinePanel', { api: { listOutline: async () => [volume, child], deleteOutline: async () => { throw new Error('该节点下还有子节点，请先删除子节点') } } })
  await rejected.vm.remove(volume)
  assert.deepEqual(rejected.vm.nodes.value.map((item) => item.id), [volume.id, child.id])
  assert.match(rejected.messages[0].text, /还有子节点/)
  rejected.dispose()
  const pending = wait()
  const switched = await panel('OutlinePanel', { api: { deleteOutline: async () => pending.promise } })
  const deleting = switched.vm.remove(volume); switched.props.novelId = 'novel-b'; await settle()
  pending.resolve(); await deleting
  assert.equal(switched.vm.nodes.value[0].id, 'novel-b-volume')
  assert.equal(switched.messages.length, 0)
  switched.dispose()
})

test('大纲真实模板在创建保存中禁改层级，标题概要仍能编辑并继续保存', async () => {
  const pending = wait()
  const context = await panel('OutlinePanel', { api: { createOutline: async () => pending.promise } })
  context.vm.openCreate('novel-a-volume', 'act'); context.vm.form.value.title = '新幕'
  const saving = context.vm.save()
  const descendants = []
  const visit = (node) => {
    if (Array.isArray(node)) { node.forEach(visit); return }
    if (!node || typeof node !== 'object') return
    descendants.push(node)
    if (typeof node.children?.default === 'function') visit(node.children.default())
    else visit(node.children)
  }
  visit(context.render())
  const select = descendants.find((node) => node.type === 'nselect' && node.props?.value === 'act')
  assert.equal(select.props.disabled, true)
  select.props['onUpdate:value']('chapter')
  assert.equal(context.vm.form.value.level, 'act')
  context.vm.form.value.summary = '保存期间补充的概要'
  pending.resolve(outlineNode('novel-a', { id: 'created-act', level: 'act', parent_id: 'novel-a-volume' })); await saving
  assert.equal(context.vm.form.value.level, 'act')
  assert.equal(context.vm.form.value.summary, '保存期间补充的概要')
  assert.equal(context.vm.dirty.value, true)
  await context.vm.save()
  const update = context.requests.find((request) => request.method === 'updateOutline')
  assert.deepEqual(update.args[2], { title: '新幕', summary: '保存期间补充的概要' })
  assert.equal(context.vm.showModal.value, false)
  context.dispose()
})

test('大纲真实 SFC 拖拽只重排同父同层，箭头共用持久化顺序，保存期间和旧拖拽禁止操作', async () => {
  const volume = outlineNode(), act = outlineNode('novel-a', { id: 'act', level: 'act', parent_id: volume.id, sort_order: 0 })
  const first = outlineNode('novel-a', { id: 'first', level: 'chapter', parent_id: volume.id, sort_order: 1 })
  const second = outlineNode('novel-a', { id: 'second', level: 'chapter', parent_id: volume.id, sort_order: 2 })
  const inner = outlineNode('novel-a', { id: 'inner', level: 'chapter', parent_id: act.id, sort_order: 0 })
  const nodes = [volume, act, first, second, inner]
  const context = await panel('OutlinePanel', { api: { listOutline: async () => nodes.map((node) => ({ ...node })), reorderOutline: async (id, order) => { order.forEach((nodeId, index) => { nodes.find((node) => node.id === nodeId).sort_order = index }) } } })
  let prevented = 0
  const event = { preventDefault() { prevented += 1 }, dataTransfer: { setData() {} } }
  context.vm.startDrag(first, event)
  assert.equal(context.vm.canDrop(second), true)
  assert.equal(context.vm.canDrop(act), false)
  assert.equal(context.vm.canDrop(inner), false)
  await context.vm.dropOn(inner, event)
  assert.equal(context.requests.some((request) => request.method === 'reorderOutline'), false)
  context.vm.startDrag(first, event); context.vm.dragOver(second, event); await context.vm.dropOn(second, event)
  assert.equal(prevented, 2)
  assert.deepEqual(context.requests.find((request) => request.method === 'reorderOutline').args, ['novel-a', ['act', 'second', 'first']])
  assert.deepEqual(context.vm.childrenOf(volume.id).map((node) => node.id), ['act', 'second', 'first'])
  await context.vm.move(first, -1)
  assert.deepEqual(context.vm.childrenOf(volume.id).map((node) => node.id), ['act', 'first', 'second'])
  context.vm.saving.value = true; context.vm.startDrag(first, event)
  assert.equal(context.vm.draggedNodeId.value, null)
  context.vm.saving.value = false; context.vm.startDrag(first, event)
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  assert.equal(context.vm.canDrop(second), false)
  const before = context.requests.filter((request) => request.method === 'reorderOutline').length
  await context.vm.dropOn(second, event)
  assert.equal(context.requests.filter((request) => request.method === 'reorderOutline').length, before)
  context.dispose()
})

test('大纲真实 SFC 迟到拖拽重排不重新读取新作品或影响其操作状态', async () => {
  const pending = wait()
  const first = outlineNode(), second = outlineNode('novel-a', { id: 'second', sort_order: 1 })
  const context = await panel('OutlinePanel', { api: { listOutline: async (id) => id === 'novel-a' ? [first, second] : [outlineNode(id)], reorderOutline: async () => pending.promise } })
  const event = { preventDefault() {}, dataTransfer: { setData() {} } }
  context.vm.startDrag(first, event); const dropping = context.vm.dropOn(second, event)
  context.props.novelId = 'novel-b'; await settle()
  const reads = context.requests.filter((request) => request.method === 'listOutline').length
  pending.reject(new Error('A 的重排失败')); await dropping
  assert.equal(context.requests.filter((request) => request.method === 'listOutline').length, reads)
  assert.equal(context.vm.nodes.value[0].id, 'novel-b-volume')
  assert.equal(context.vm.moving.value, false)
  assert.equal(context.messages.length, 0)
  context.dispose()
})

test('大纲真实编译模板中卷直属章显示章样式且没有新增子章，幕保留新增章', async () => {
  const volume = outlineNode(), act = outlineNode('novel-a', { id: 'act', level: 'act', parent_id: volume.id, title: '幕节点' })
  const chapter = outlineNode('novel-a', { id: 'direct', level: 'chapter', parent_id: volume.id, title: '直属章节点' })
  const context = await panel('OutlinePanel', { api: { listOutline: async () => [volume, act, chapter] } })
  const flattened = []
  const visit = (node) => {
    if (!node) return
    if (Array.isArray(node)) { node.forEach(visit); return }
    if (typeof node !== 'object') return
    flattened.push(node)
    if (typeof node.children?.default === 'function') visit(node.children.default())
    else visit(node.children)
  }
  const text = (node) => {
    if (node == null) return ''
    if (typeof node !== 'object') return String(node)
    if (Array.isArray(node)) return node.map(text).join('')
    return text(typeof node.children?.default === 'function' ? node.children.default() : node.children)
  }
  visit(context.render())
  const direct = flattened.find((node) => node.props?.class === 'node chapter' && text(node).includes('直属章节点'))
  const actNode = flattened.find((node) => node.props?.class === 'node act' && text(node).includes('幕节点'))
  assert.ok(direct); assert.ok(actNode)
  assert.equal(text(direct).includes('+章'), false)
  assert.equal(text(actNode).includes('+章'), true)
  const row = flattened.find((node) => node.props?.class?.includes('row') && text(node).includes('直属章节点') && !text(node).includes('幕节点'))
  assert.equal(row.props.draggable, true)
  assert.equal(typeof row.props.onDragstart, 'function')
  assert.equal(typeof row.props.onDrop, 'function')
  context.dispose()
})

test('AI大纲卡真实 SFC 切来源 A→B→A 使旧预览成功/失败失效且不影响新请求 loading', async () => {
  for (const outcome of ['success', 'failure']) {
    const old = wait(), current = wait(); let calls = 0
    const context = await panel('OutlinePlanCard', { api: { previewOutline: async () => ++calls === 1 ? old.promise : current.promise } })
    context.vm.sourceText.value = '原有构思'; const first = context.vm.preview()
    context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'
    context.vm.sourceText.value = '新构思'; const second = context.vm.preview()
    if (outcome === 'success') old.resolve(outlineDraft('旧预览'))
    else old.reject(new Error('旧预览失败'))
    await first
    assert.equal(context.vm.loading.value, true)
    assert.equal(context.vm.plan.value, null)
    assert.equal(context.messages.length, 0)
    current.resolve(outlineDraft('新预览')); await second
    assert.equal(context.vm.plan.value.title, '新预览')
    assert.equal(context.vm.loading.value, false)
    context.dispose()
  }
})

test('AI大纲卡真实 SFC 卸载或更换消息来源后迟到预览失败不提示，成功不更新', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await panel('OutlinePlanCard', { api: { previewOutline: async () => pending.promise } })
    context.vm.sourceText.value = '旧构思'; const previewing = context.vm.preview(); context.dispose()
    if (outcome === 'success') pending.resolve(outlineDraft('卸载后的预览'))
    else pending.reject(new Error('卸载后的失败'))
    await previewing
    assert.equal(context.vm.plan.value, null)
    assert.equal(context.messages.length, 0)
    assert.equal(context.emitted.length, 0)
  }
  const pending = wait()
  const changed = await panel('OutlinePlanCard', { props: { conversationId: 'conversation-a', messageId: 'message-a' }, api: { previewMessageOutline: async () => pending.promise } })
  const oldPreview = changed.vm.preview(); changed.props.messageId = 'message-b'
  pending.reject(new Error('旧消息的失败')); await oldPreview
  assert.equal(changed.messages.length, 0)
  assert.equal(changed.vm.loading.value, false)
  changed.dispose()
})

test('AI大纲卡真实 SFC 重新生成期间的作者编辑不被迟到预览覆盖', async () => {
  const pending = wait()
  const context = await panel('OutlinePlanCard', { api: { previewOutline: async () => pending.promise } })
  context.vm.sourceText.value = '构思'; context.vm.plan.value = outlineDraft('当前草稿')
  const previewing = context.vm.preview(); context.vm.plan.value.chapters[0].summary = '等待期间的新编辑'
  pending.resolve(outlineDraft('重新生成的草稿')); await previewing
  assert.equal(context.vm.plan.value.title, '当前草稿')
  assert.equal(context.vm.plan.value.chapters[0].summary, '等待期间的新编辑')
  assert.equal(context.vm.loading.value, false)
  context.dispose()
})

test('AI大纲卡真实 SFC 确认应用发送冻结草稿，期间的新编辑保留且不触发父级关闭', async () => {
  const pending = wait()
  const context = await panel('OutlinePlanCard', { api: { applyOutline: async () => pending.promise } })
  context.vm.sourceText.value = '构思'; context.vm.plan.value = outlineDraft('提交的卷')
  const applying = context.vm.apply()
  context.vm.plan.value.title = '应用期间编辑的卷'
  context.vm.plan.value.chapters[0].summary = '新章节概要'
  pending.resolve({ volume_id: 'saved-volume' }); await applying
  const submitted = context.requests.find((request) => request.method === 'applyOutline')
  assert.equal(submitted.args[0], 'novel-a')
  assert.equal(submitted.args[1].title, '提交的卷')
  assert.equal(submitted.args[1].chapters[0].summary, '章概要')
  assert.equal(context.vm.plan.value.title, '应用期间编辑的卷')
  assert.equal(context.vm.plan.value.chapters[0].summary, '新章节概要')
  assert.equal(context.emitted.some(([event]) => event === 'applied'), false)
  assert.equal(context.vm.applying.value, false)
  assert.match(context.messages[0].text, /新增编辑已保留/)
  context.dispose()
})

test('AI大纲卡真实 SFC 应用卸载后不提示/回显；当前未编辑的确认正常回显落地', async () => {
  for (const outcome of ['success', 'failure']) {
    const pending = wait()
    const context = await panel('OutlinePlanCard', { api: { applyOutline: async () => pending.promise } })
    context.vm.plan.value = outlineDraft(); const applying = context.vm.apply(); context.dispose()
    if (outcome === 'success') pending.resolve({ volume_id: 'late-volume' })
    else pending.reject(new Error('迟到应用失败'))
    await applying
    assert.equal(context.messages.length, 0)
    assert.equal(context.emitted.length, 0)
  }
  const current = await panel('OutlinePlanCard')
  current.vm.plan.value = outlineDraft(); await current.vm.apply()
  assert.deepEqual(current.emitted, [['applied', { volume_id: 'saved-volume' }]])
  current.dispose()
})

test('定稿真实 SFC A→B 迟到结果不打开 B 建议弹窗且不能确认写入 B', async () => {
  const pending = wait()
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleChapter: async () => pending.promise } })
  const settling = context.vm.doSettle()
  context.props.novelId = 'novel-b'; await settle()
  await context.vm.selectChapter(context.vm.chapters.value[0], true)
  pending.resolve(settleResult({ summary: 'A 的迟到摘要', suggestions: [{ type: 'character', data: { name: 'A 人物' } }] })); await settling
  assert.equal(context.vm.showSettle.value, false)
  assert.equal(context.vm.suggestions.value.length, 0)
  await context.vm.confirmSettle()
  assert.equal(context.requests.some((request) => request.method === 'settleApply'), false)
  assert.equal(context.vm.title.value, 'novel-b')
  context.dispose()
})

test('定稿真实 SFC 生成及目录刷新期间作者新正文不会被旧响应覆盖', async () => {
  const pending = wait(), refreshing = wait(); let reads = 0
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: {
    settleChapter: async () => pending.promise,
    listChapters: async () => ++reads === 1 ? [{ id: 'novel-a-chapter', novel_id: 'novel-a', seq: 1, title: '原标题', content: '原正文', directive: '', summary: '', status: 'draft', outline_node_id: '' }] : refreshing.promise,
  } })
  const settling = context.vm.doSettle()
  pending.resolve(settleResult()); await settle()
  context.vm.content.value = '等待刷新时作者的新正文'
  refreshing.resolve([{ id: 'novel-a-chapter', novel_id: 'novel-a', seq: 1, title: '原标题', content: '原正文', directive: '', summary: '已定稿摘要', status: 'finalized', outline_node_id: '' }]); await settling
  assert.equal(context.vm.content.value, '等待刷新时作者的新正文')
  assert.equal(context.vm.isDirty.value, true)
  assert.equal(context.vm.showSettle.value, false)
  context.dispose()
})

test('定稿真实 SFC 作品/章节/作者字段 A→B→A 使旧提炼成功失败失效，旧完成不清新 loading', async () => {
  for (const outcome of ['success', 'failure']) {
    const old = wait(), current = wait(); let calls = 0
    const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleChapter: async () => ++calls === 1 ? old.promise : current.promise } })
    const oldRequest = context.vm.doSettle()
    context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'
    const currentRequest = context.vm.doSettle()
    if (outcome === 'success') old.resolve(settleResult({ summary: '旧摘要' }))
    else old.reject(new Error('旧提炼失败'))
    await oldRequest
    assert.equal(context.vm.settleLoading.value, true)
    assert.equal(context.vm.showSettle.value, false)
    assert.equal(context.messages.length, 0)
    current.resolve(settleResult({ summary: '当前摘要' })); await currentRequest
    assert.equal(context.vm.settleSummary.value, '当前摘要')
    assert.equal(context.vm.showSettle.value, true)
    context.dispose()
  }
  for (const field of ['title', 'directive', 'content']) {
    const pending = wait()
    const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleChapter: async () => pending.promise } })
    const requesting = context.vm.doSettle(), original = context.vm[field].value
    context.vm[field].value = '短暂的新编辑'; context.vm[field].value = original
    pending.resolve(settleResult()); await requesting
    assert.equal(context.vm.showSettle.value, false, field)
    await context.vm.confirmSettle()
    assert.equal(context.requests.some((request) => request.method === 'settleApply'), false)
    context.dispose()
  }
  const pending = wait()
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleChapter: async () => pending.promise } })
  const original = { ...context.vm.chapters.value[0] }, next = { ...original, id: 'next-chapter', title: '第二章', content: '第二章正文', seq: 2 }
  context.vm.chapters.value.push(next)
  const requesting = context.vm.doSettle()
  await context.vm.selectChapter(next, true); await context.vm.selectChapter(original, true)
  pending.resolve(settleResult()); await requesting
  assert.equal(context.vm.showSettle.value, false)
  context.dispose()
})

test('定稿真实 SFC 已提炼预览绑定原正文，编辑或关闭后不能确认旧建议', async () => {
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' } })
  await context.vm.doSettle()
  context.vm.content.value = '提炼后作者修改正文'
  await context.vm.confirmSettle()
  assert.equal(context.requests.some((request) => request.method === 'settleApply'), false)
  assert.equal(context.vm.showSettle.value, false)
  assert.equal(context.vm.content.value, '提炼后作者修改正文')
  context.dispose()
  const closed = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' } })
  await closed.vm.doSettle(); closed.vm.handleSettleVisibility(false); await closed.vm.confirmSettle()
  assert.equal(closed.requests.some((request) => request.method === 'settleApply'), false)
  closed.dispose()
})

test('定稿真实 SFC 确认请求冻结作品章节；切作品后的 A 响应不影响 B 的新确认', async () => {
  const old = wait(), current = wait(); let calls = 0
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleApply: async () => ++calls === 1 ? old.promise : current.promise } })
  await context.vm.doSettle(); const oldApplying = context.vm.confirmSettle()
  context.props.novelId = 'novel-b'; await settle()
  await context.vm.selectChapter(context.vm.chapters.value[0], true)
  await context.vm.doSettle(); const currentApplying = context.vm.confirmSettle()
  old.resolve({ created: [{ index: 0, type: 'character', id: 'A-created' }], failed: [] }); await oldApplying
  assert.equal(context.vm.settleApplying.value, true)
  assert.equal(context.vm.settledCount.value, 0)
  assert.equal(context.vm.showSettle.value, true)
  assert.equal(context.messages.length, 0)
  current.resolve({ created: [], failed: [{ index: 0, type: 'character', error: 'B 的失败' }] }); await currentApplying
  const writes = context.requests.filter((request) => request.method === 'settleApply')
  assert.deepEqual(writes.map((request) => request.args.slice(0, 2)), [['novel-a', 'novel-a-chapter'], ['novel-b', 'novel-b-chapter']])
  assert.equal(context.vm.settleErrors.value[0], 'B 的失败')
  assert.equal(context.vm.settleApplying.value, false)
  assert.equal(context.vm.showSettle.value, true)
  context.dispose()
})

test('定稿真实 SFC 确认期间新增 JSON 编辑保留且成功项锁定，后续确认不重复落库', async () => {
  const pending = wait()
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: { settleApply: async () => pending.promise } })
  await context.vm.doSettle(); const applying = context.vm.confirmSettle()
  context.vm.settleDataTexts.value[0] = JSON.stringify({ name: '等待时的新名字' })
  pending.resolve({ created: [{ index: 0, type: 'character', id: 'created-person' }], failed: [] }); await applying
  assert.equal(context.vm.settleDataTexts.value[0], JSON.stringify({ name: '等待时的新名字' }))
  assert.equal(context.vm.settleCompleted.value[0], true)
  assert.equal(context.vm.showSettle.value, true)
  const request = context.requests.find((request) => request.method === 'settleApply')
  assert.equal(request.args[2][0].data.name, '章节人物')
  await context.vm.confirmSettle()
  assert.equal(context.requests.filter((request) => request.method === 'settleApply').length, 1)
  assert.equal(context.vm.settledCount.value, 1)
  context.dispose()
})

test('定稿真实 SFC 卸载后提炼或应用迟到成功/失败均保持安静', async () => {
  for (const stage of ['preview', 'apply']) {
    for (const outcome of ['success', 'failure']) {
      const pending = wait()
      const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: stage === 'preview'
        ? { settleChapter: async () => pending.promise }
        : { settleApply: async () => pending.promise } })
      if (stage === 'apply') await context.vm.doSettle()
      const requesting = stage === 'preview' ? context.vm.doSettle() : context.vm.confirmSettle()
      context.dispose()
      if (outcome === 'failure') pending.reject(new Error('卸载后的错误'))
      else pending.resolve(stage === 'preview' ? settleResult() : { created: [{ index: 0, type: 'character', id: 'late' }] })
      await requesting
      assert.equal(context.vm.showSettle.value, false)
      assert.equal(context.messages.length, 0)
    }
  }
})

test('定稿真实编译模板持续显示记忆告警；列表刷新失败不妨碍已确认来源的建议落库', async () => {
  let reads = 0
  const context = await panel('WritingPanel', { props: { initialChapterId: 'novel-a-chapter' }, api: {
    listChapters: async (id) => {
      if (++reads > 1) throw new Error('临时无法读取目录')
      return [{ id: 'novel-a-chapter', novel_id: id, seq: 1, title: '第一章', content: '正文', directive: '', summary: '', status: 'draft', outline_node_id: '' }]
    },
    settleChapter: async () => settleResult({ warnings: ['章节已定稿，但记忆嵌入失败，尚未完整写入检索层'] }),
    settleApply: async () => ({ created: [{ index: 0, type: 'character', id: 'person' }], failed: [] }),
  } })
  await context.vm.doSettle()
  assert.equal(context.vm.showSettle.value, true)
  assert.equal(context.vm.settleWarnings.value.length, 2)
  const all = []
  const visit = (node) => {
    if (Array.isArray(node)) { node.forEach(visit); return }
    if (!node || typeof node !== 'object') return
    all.push(node)
    if (typeof node.children?.default === 'function') visit(node.children.default())
    else visit(node.children)
  }
  visit(context.render())
  assert.ok(all.some((node) => node.type === 'li' && typeof node.children === 'string' && node.children.includes('记忆嵌入失败')))
  await context.vm.confirmSettle()
  assert.equal(context.vm.settledCount.value, 1)
  assert.equal(context.vm.showSettle.value, false)
  context.dispose()
})

test('成品交付真实 SFC 改范围 A→B→A 使旧预检查失效，未经当前检查不能下载', async () => {
  const pending = wait()
  const context = await panel('BookDeliveryPanel', { api: { preflightExport: async () => pending.promise } })
  const checking = context.vm.runPreflight()
  context.vm.includeDrafts.value = true; context.vm.includeDrafts.value = false
  pending.resolve(preflight()); await checking; await context.vm.confirmDownload()
  assert.equal(context.vm.report.value, null)
  assert.equal(context.requests.some((request) => request.method === 'downloadBookFile'), false)
  assert.equal(context.vm.checking.value, false)
  context.dispose()
})

test('成品交付真实 SFC 切作品 A→B→A 拒绝旧目录/预检查/下载结果', async () => {
  const summaryPending = wait(), checkPending = wait()
  let calls = 0
  const context = await panel('BookDeliveryPanel', { api: {
    getBookSummary: async (id) => ++calls === 1 ? summaryPending.promise : book(id),
    preflightExport: async () => checkPending.promise,
  } })
  const checking = context.vm.runPreflight()
  context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a'; await settle()
  summaryPending.resolve({ ...book(), title: '旧目录' }); checkPending.resolve(preflight()); await checking; await settle()
  assert.equal(context.vm.summary.value.title, 'novel-a')
  assert.equal(context.vm.report.value, null)
  assert.equal(context.vm.showConfirm.value, false)
  context.dispose()
})

test('成品交付真实 SFC 下载使用已检查的冻结请求；改变格式/卸载取消迟到文件触发', async () => {
  const originalDocument = globalThis.document, originalUrl = globalThis.URL.createObjectURL
  let downloads = 0
  globalThis.document = { createElement: () => ({ click: () => { downloads += 1 }, remove() {} }), body: { appendChild() {} } }
  globalThis.URL.createObjectURL = () => 'blob:test'
  try {
    for (const change of ['format', 'novel', 'unmount']) {
      const pending = wait()
      const context = await panel('BookDeliveryPanel', { api: { downloadBookFile: async () => pending.promise } })
      await context.vm.runPreflight(); const downloading = context.vm.confirmDownload()
      const request = context.requests.find((request) => request.method === 'downloadBookFile')
      assert.equal(request.args[0], 'novel-a')
      assert.equal(request.args[1].format, 'docx')
      if (change === 'format') context.vm.format.value = 'txt'
      else if (change === 'novel') { context.props.novelId = 'novel-b'; context.props.novelId = 'novel-a' }
      else context.dispose()
      pending.resolve(new Blob(['旧文件'])); await downloading
      assert.equal(downloads, 0)
      if (change !== 'unmount') context.dispose()
    }
  } finally { globalThis.document = originalDocument; globalThis.URL.createObjectURL = originalUrl }
})

test('成品交付真实 SFC 当前检查确认后正常下载三种格式，文件名与请求一致', async () => {
  const originalDocument = globalThis.document, originalCreateUrl = globalThis.URL.createObjectURL
  const originalRevokeUrl = globalThis.URL.revokeObjectURL, originalTimeout = globalThis.setTimeout
  const downloaded = [], revoked = []
  globalThis.document = { createElement: () => ({ download: '', click() { downloaded.push(this.download) }, remove() {} }), body: { appendChild() {} } }
  globalThis.URL.createObjectURL = () => 'blob:test'
  globalThis.URL.revokeObjectURL = (url) => revoked.push(url)
  globalThis.setTimeout = (fn) => { fn(); return 0 }
  try {
    for (const format of ['docx', 'markdown', 'txt']) {
      const context = await panel('BookDeliveryPanel')
      context.vm.format.value = format; await context.vm.runPreflight(); await context.vm.confirmDownload()
      const check = context.requests.find((request) => request.method === 'preflightExport')
      const file = context.requests.find((request) => request.method === 'downloadBookFile')
      assert.deepEqual(file.args, check.args)
      assert.equal(context.vm.downloading.value, false)
      assert.equal(context.messages.at(-1).text, '文件已开始下载')
      context.dispose()
    }
    assert.deepEqual(downloaded, ['novel-a.docx', 'novel-a.md', 'novel-a.txt'])
    assert.equal(revoked.length, 3)
  } finally {
    globalThis.document = originalDocument; globalThis.URL.createObjectURL = originalCreateUrl
    globalThis.URL.revokeObjectURL = originalRevokeUrl; globalThis.setTimeout = originalTimeout
  }
})

test('工作台真实 SFC 设定、大纲脏数据保护换作品、换页和返回对话，取消保留目标', async () => {
  const context = await panel('WorkbenchView', { props: { initialNovelId: 'novel-a' } })
  context.vm.settingsDirty.value = true
  context.vm.changeNovel('novel-b')
  assert.equal(context.vm.novelId.value, 'novel-a')
  assert.equal(context.vm.showNavigationConfirm.value, true)
  assert.match(context.vm.navigationWarning.value, /设定/)
  context.vm.cancelNavigation(); assert.equal(context.vm.novelId.value, 'novel-a')
  context.vm.confirmNavigation(); assert.equal(context.vm.novelId.value, 'novel-a')
  context.vm.changeNovel('novel-b'); context.vm.confirmNavigation()
  assert.equal(context.vm.novelId.value, 'novel-b')
  context.vm.settingsDirty.value = false; context.vm.outlineDirty.value = true
  context.vm.changeTab('writing'); assert.equal(context.vm.activeTab.value, 'settings')
  context.vm.cancelNavigation(); context.vm.returnToChat()
  assert.equal(context.emitted.some(([event]) => event === 'back'), false)
  context.vm.confirmNavigation(); assert.equal(context.emitted.some(([event]) => event === 'back'), true)
  context.dispose()
})

test('工作台真实 SFC 作品创建中新表单不被旧成功关闭，卸载后旧成功不跳转或提示', async () => {
  const pending = wait()
  const context = await panel('WorkbenchView', { props: { initialNovelId: 'novel-a' }, api: { createNovel: async () => pending.promise } })
  context.vm.openCreate(); context.vm.createForm.value.title = '提交的作品'
  const creating = context.vm.submitCreate()
  context.vm.createForm.value.title = '创建中新编辑的作品'
  pending.resolve({ id: 'created-novel', title: '提交的作品' }); await creating
  assert.equal(context.vm.showCreate.value, true)
  assert.equal(context.vm.createForm.value.title, '创建中新编辑的作品')
  assert.equal(context.vm.novelId.value, 'novel-a')
  context.dispose()
  const late = wait()
  const disposed = await panel('WorkbenchView', { props: { initialNovelId: 'novel-a' }, api: { createNovel: async () => late.promise } })
  disposed.vm.openCreate(); disposed.vm.createForm.value.title = '旧作品'
  const oldCreate = disposed.vm.submitCreate(); disposed.dispose()
  late.resolve({ id: 'late-novel', title: '旧作品' }); await oldCreate
  assert.equal(disposed.vm.novelId.value, 'novel-a')
  assert.equal(disposed.messages.length, 0)
})
