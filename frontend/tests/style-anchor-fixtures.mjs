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
  ['../utils/projectDraft', '../src/utils/projectDraft.ts'],
].map(async ([name, path]) => [name, moduleUrl(compile((await readFile(new URL(path, import.meta.url), 'utf8'))
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)))])))
export const settle = async () => { for (let index = 0; index < 50; index += 1) { await Promise.resolve(); await nextTick() } }
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

export async function styleAnchorRuntime(options = {}) {
  const loads = [], mounts = [], unmounts = [], requests = [], messages = []
  const listeners = new Map()
  globalThis.window = { confirm: () => options.confirm?.() ?? true,
    addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn) },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn) },
  }; Object.defineProperty(globalThis,'navigator',{ configurable:true,value:{clipboard:{writeText:async(text)=>{if(options.clipboardError)throw new Error('clipboard denied');options.copied?.push(text)}}}})
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
  const dialogs = []; const harness = {
    components,
    api: Object.fromEntries(Object.entries({ ...defaults, ...options.api }).map(([name, fn]) => [name, (...args) => {
      requests.push({ name, args: structuredClone(args) }); return fn(...args)
    }])),
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map((type) => [type, (text) => messages.push({ type, text })])),
    useDialog: () => ({ warning: (config) => dialogs.push(config) }),
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
  let styleDependency = {}; try { styleDependency['../utils/styleAnchorDraft'] = moduleUrl(compile((await readFile(new URL('../src/utils/styleAnchorDraft.ts', import.meta.url),'utf8')).replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// style-page-${sequence}`) } catch (error) { if(error.code !== 'ENOENT') throw error }
  const dependencies = { ...dependencyUrls, ...styleDependency, '../utils/projectDraft': moduleUrl(compile(
    (await readFile(new URL('../src/utils/projectDraft.ts', import.meta.url), 'utf8'))
      .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  ) + `\n// fixture-page-${sequence}`) }
  try { dependencies['../utils/entityDraft'] = moduleUrl(compile((await readFile(new URL('../src/utils/entityDraft.ts', import.meta.url), 'utf8'))
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace("from '../types'", `from '${dependencies['../types']}'`)) + `\n// entity-page-${sequence}`) }
  catch (error) { if (error.code !== 'ENOENT') throw error }
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
    for (const [name, url] of Object.entries(dependencies)) source = source.replaceAll(`from '${name}'`, `from '${url}'`)
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
  return { loads, mounts, unmounts, requests, messages, dialogs, listeners, instance,
    vm: options.app ? app._instance.setupState : instance('WorkbenchView').setupState,
    dispose: () => { app.unmount(); delete globalThis[key] },
  }
}
