import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createRenderer, defineComponent, h, nextTick, onBeforeUnmount, onMounted } from 'vue'
import { build } from 'vite'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const dependencyUrls = Object.fromEntries(await Promise.all([
  ['../types', '../src/types.ts'], ['../utils/chapter', '../src/utils/chapter.ts'], ['../utils/chapterCreation', '../src/utils/chapterCreation.ts'],
  ['../utils/asyncScope', '../src/utils/asyncScope.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile(await readFile(new URL(path, import.meta.url), 'utf8')))])))
const settle = async () => { for (let index = 0; index < 50; index += 1) { await Promise.resolve(); await nextTick() } }
const wait = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
let sequence = 0

// Vue真实renderer驱动async组件加载、子挂载钩子、v-show和卸载，不需浏览器依赖。
function renderer() {
  const remove = (node) => {
    if (!node.parent) return
    const index = node.parent.children.indexOf(node)
    if (index >= 0) node.parent.children.splice(index, 1)
    node.parent = null
  }
  return createRenderer({
    createElement: (type) => ({ type, children: [], props: {}, style: {}, parent: null }),
    createText: (text) => ({ type: 'text', text, parent: null }),
    createComment: (text) => ({ type: 'comment', text, parent: null }),
    insert: (node, parent, anchor = null) => {
      remove(node); node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node)
    },
    remove,
    setElementText: (node, text) => { node.text = text; node.children = [] },
    setText: (node, text) => { node.text = text },
    parentNode: (node) => node.parent,
    nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: (node, key, _previous, value) => { node.props[key] = value },
  })
}

function findInstance(vnode, name) {
  if (!vnode) return null
  if (vnode.component) {
    if ((vnode.type.__name ?? vnode.type.name) === name) return vnode.component
    const nested = findInstance(vnode.component.subTree, name)
    if (nested) return nested
  }
  if (Array.isArray(vnode.children)) for (const child of vnode.children) {
    const nested = findInstance(child, name)
    if (nested) return nested
  }
  return null
}

async function mounted(options = {}) {
  const loads = [], mounts = [], unmounts = [], requests = [], messages = []
  globalThis.window = { confirm: () => true, addEventListener() {}, removeEventListener() {} }
  const components = {}
  for (const name of ['ChatView', 'WritingPanel', 'OutlinePanel', 'KnowledgeGraph', 'AutopilotPanel', 'ForeshadowingTracking', 'BookDeliveryPanel']) {
    components[name] = defineComponent({ name, props: ['novelId', 'initialChapterId'], setup(props) {
      onMounted(() => mounts.push(name)); onBeforeUnmount(() => unmounts.push(name))
      return () => h('section', { 'data-panel': name }, props.novelId ?? '')
    } })
  }
  const passthrough = defineComponent({ setup(_props, { slots }) { return () => h('div', slots.default?.()) } })
  const modal = defineComponent({ props: ['show'], setup(props, { slots }) { return () => props.show ? h('aside', slots.default?.()) : null } })
  const defaults = {
    listNovels: async () => ['novel-a', 'novel-b'].map((id) => ({ id, title: id, style_anchor: '' })),
    listEntities: async (id, path) => path === 'characters' ? [{ id: `${id}-character`, novel_id: id, name: `${id}人物`, extras: {} }] : [],
    listChapterMetadata: async () => [],
  }
  const harness = {
    components,
    api: Object.fromEntries(Object.entries({ ...defaults, ...options.api }).map(([name, fn]) => [name, (...args) => {
      requests.push({ name, args: structuredClone(args) }); return fn(...args)
    }])),
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    loadComponent: async (name) => {
      loads.push(name)
      await options.delay?.[name]?.promise
      return components[name]
    },
  }
  for (const name of ['NButton', 'NEmpty', 'NForm', 'NFormItem', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NMenu', 'NSelect', 'NTabPane', 'NTabs']) harness[name] = passthrough
  harness.NModal = modal
  const key = `__lazyWorkbench${++sequence}`
  globalThis[key] = harness
  async function compileComponent(name) {
    const path = name === 'App' ? '../src/App.vue' : `../src/components/${name}.vue`
    const descriptor = parse(await readFile(new URL(path, import.meta.url), 'utf8')).descriptor
    const script = compileScript(descriptor, { id: `lazy-${sequence}-${name}` })
    const template = compileTemplate({ source: descriptor.template.content, id: `lazy-${sequence}-${name}`, filename: `${name}.vue`, compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(template.errors, [])
    let source = `const __harness = globalThis.${key}\n` + script.content.replace('export default', 'const __component =')
      + '\n' + template.code.replace('export function render', 'function render')
      + `\n__component.name = '${name}'\n__component.render = render\nexport default __component\n`
    source = source
      .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
      .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
      .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
      .replace(/import (\w+) from ['"]\.\/(?:components\/)?[^'"]+\.vue['"]/g, 'const $1 = __harness.components.$1')
      .replace(/import\(['"]\.\/(?:components\/)?([^'"]+)\.vue['"]\)/g, '__harness.loadComponent("$1")')
    for (const [name, url] of Object.entries(dependencyUrls)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
    return (await import(moduleUrl(compile(source) + `\n// ${sequence}-${name}`))).default
  }
  components.SettingsPanel = await compileComponent('SettingsPanel')
  components.WorkbenchView = await compileComponent('WorkbenchView')
  components.App = await compileComponent('App')
  const root = { children: [] }
  const app = renderer().createApp(options.app ? components.App : components.WorkbenchView,
    options.props ?? (options.app ? {} : { initialNovelId: 'novel-a', initialTab: 'writing' }))
  app.mount(root); await settle()
  const instance = (name) => findInstance(app._instance.vnode, name)
  return { loads, mounts, unmounts, requests, messages, instance,
    vm: options.app ? app._instance.setupState : instance('WorkbenchView').setupState,
    dispose: () => { app.unmount(); delete globalThis[key] },
  }
}

test('真实App初始聊天不下载/挂载工作台，直达写作只加载所选面板', async () => {
  const context = await mounted({ app: true })
  assert.deepEqual(context.loads, []); assert.equal(context.instance('WorkbenchView'), null)
  context.vm.onOpenWriting('novel-a', 'chapter-a'); await settle()
  assert.deepEqual(context.loads, ['WorkbenchView', 'WritingPanel'])
  assert.equal(context.instance('WritingPanel').props.initialChapterId, 'chapter-a')
  assert.equal(context.requests.some((entry) => entry.name === 'listEntities'), false)
  context.vm.onBack(); await settle()
  assert.equal(context.instance('WorkbenchView'), null)
  assert.equal(context.unmounts.includes('WritingPanel'), true)
  context.dispose()
})

test('真实App聊天直达大纲不加载隐藏设定或图表组件', async () => {
  const context = await mounted({ app: true })
  context.vm.onOpenOutline('novel-a'); await settle()
  assert.deepEqual(context.loads, ['WorkbenchView', 'OutlinePanel'])
  assert.equal(context.instance('SettingsPanel'), null)
  assert.equal(context.requests.some((entry) => entry.name === 'listEntities'), false)
  context.dispose()
})

test('首次访问设定才懒挂载，切页后v-show保留真实编辑身份、输入及dirty导航', async () => {
  const context = await mounted()
  assert.deepEqual(context.loads, ['WritingPanel']); assert.equal(context.instance('SettingsPanel'), null)
  context.vm.changeTab('settings'); await settle()
  const settings = context.instance('SettingsPanel')
  assert.equal(context.requests.filter((entry) => entry.name === 'listEntities').length, 6)
  settings.setupState.startEdit({ id: 'novel-a-character', name: '原人物', extras: {} })
  settings.setupState.form.name = '尚未保存的作者设定'; await settle()
  context.vm.changeTab('outline'); await settle()
  assert.equal(context.vm.activeTab, 'settings'); assert.equal(context.vm.showNavigationConfirm, true)
  context.vm.cancelNavigation(); context.vm.changeTab('outline'); context.vm.confirmNavigation(); await settle()
  assert.equal(context.vm.activeTab, 'outline')
  assert.equal(context.instance('SettingsPanel'), settings)
  assert.equal(settings.subTree.el.style.display, 'none')
  assert.equal(settings.setupState.form.name, '尚未保存的作者设定')
  context.vm.changeTab('settings'); context.vm.confirmNavigation(); await settle()
  assert.equal(context.instance('SettingsPanel'), settings)
  assert.equal(settings.setupState.editingId, 'novel-a-character')
  assert.equal(settings.setupState.form.name, '尚未保存的作者设定')
  assert.equal(context.loads.filter((name) => name === 'SettingsPanel').length, 1)
  assert.equal(context.requests.filter((entry) => entry.name === 'listEntities').length, 6)
  context.dispose()
})

test('图谱首次转设定在真实异步挂载后发送焦点，延迟读取也能选中对应实体', async () => {
  const pending = wait()
  const context = await mounted({ props: { initialNovelId: 'novel-a', initialTab: 'outline' }, api: {
    listEntities: async (id, path) => path === 'characters' ? pending.promise : [],
  } })
  context.vm.changeTab('graph'); await settle()
  assert.equal(context.instance('SettingsPanel'), null)
  context.vm.onGraphEdit({ type: 'character', id: 'novel-a-character' }); await settle()
  assert.equal(context.vm.settingsMounted, true)
  const settings = context.instance('SettingsPanel')
  assert.equal(settings.setupState.editingId, null)
  pending.resolve([{ id: 'novel-a-character', name: '图谱直达人物', extras: {} }]); await settle()
  assert.equal(settings.setupState.editingId, 'novel-a-character')
  assert.equal(settings.setupState.form.name, '图谱直达人物')
  context.dispose()
})

test('图谱首次焦点等待懒组件加载期间切作品，不把旧实体带入新作品', async () => {
  const pending = wait()
  const context = await mounted({ delay: { SettingsPanel: pending } })
  context.vm.onGraphEdit({ type: 'character', id: 'novel-a-character' }); await settle()
  context.vm.changeNovel('novel-b'); await settle()
  pending.resolve(); await settle()
  const settings = context.instance('SettingsPanel')
  assert.equal(settings.props.novelId, 'novel-b')
  assert.equal(settings.setupState.editingId, null)
  assert.equal(context.vm.focusTarget, null)
  assert.equal(context.requests.filter((entry) => entry.name === 'listEntities').every((entry) => entry.args[0] === 'novel-b'), true)
  context.dispose()
})

test('其他懒面板维持按页卸载，自动驾驶只在访问时挂载并保留离开确认语义', async () => {
  const context = await mounted()
  context.vm.changeTab('autopilot'); await settle()
  assert.equal(context.instance('WritingPanel'), null)
  assert.equal(context.mounts.filter((name) => name === 'AutopilotPanel').length, 1)
  context.vm.autopilotDirty = true
  context.vm.changeTab('graph'); await settle()
  assert.equal(context.vm.activeTab, 'autopilot'); assert.equal(context.instance('KnowledgeGraph'), null)
  context.vm.confirmNavigation(); await settle()
  assert.equal(context.instance('AutopilotPanel'), null)
  assert.equal(context.unmounts.includes('AutopilotPanel'), true)
  context.vm.changeTab('autopilot'); await settle()
  assert.equal(context.mounts.filter((name) => name === 'AutopilotPanel').length, 2)
  assert.equal(context.instance('SettingsPanel'), null)
  context.dispose()
})

test('真实生产构建初始依赖树不含工作台与ECharts，七个面板均保留独立动态入口', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url))
  const built = await build({ root, configFile: `${root}/vite.config.ts`, logLevel: 'error', build: { write: false, reportCompressedSize: false } })
  const chunks = (Array.isArray(built) ? built : [built]).flatMap((item) => item.output).filter((item) => item.type === 'chunk')
  const byFile = new Map(chunks.map((item) => [item.fileName, item]))
  const initial = new Set()
  const visit = (name) => { if (initial.has(name)) return; initial.add(name); for (const child of byFile.get(name)?.imports ?? []) visit(child) }
  for (const chunk of chunks.filter((item) => item.isEntry)) visit(chunk.fileName)
  const initialChunks = chunks.filter((chunk) => initial.has(chunk.fileName))
  const initialModules = initialChunks.flatMap((chunk) => Object.keys(chunk.modules))
  assert.equal(initialModules.some((name) => /\/components\/WorkbenchView\.vue/.test(name)), false)
  assert.equal(initialModules.some((name) => /\/node_modules\/(?:echarts|zrender)\//.test(name)), false)
  assert.ok(initialChunks.reduce((bytes, chunk) => bytes + Buffer.byteLength(chunk.code), 0) < 800_000)
  for (const name of ['SettingsPanel', 'KnowledgeGraph', 'WritingPanel', 'OutlinePanel', 'AutopilotPanel', 'ForeshadowingTracking', 'BookDeliveryPanel']) {
    assert.ok(chunks.some((chunk) => chunk.isDynamicEntry && chunk.facadeModuleId?.endsWith(`/${name}.vue`)), `${name} 应有独立动态入口`)
  }
})
