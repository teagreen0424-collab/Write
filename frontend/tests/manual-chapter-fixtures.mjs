import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRenderer, defineComponent, h, nextTick } from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const vueURL = import.meta.resolve('vue')
let sequence = 0
export const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
export const settle = async () => { for (let i = 0; i < 30; i++) { await Promise.resolve(); await nextTick() } }
export const chapter = (id = 'chapter-a', novelId = 'novel-a', patch = {}) => ({ id, novel_id: novelId, outline_node_id: '', seq: 1, title: '原标题', content: '原正文😀\0\r\n ', status: 'draft', summary: '', tension_score: null, drift_score: null, directive: '原指令', created_at: '2026-10-08T00:00:00', updated_at: '2026-10-08T00:00:00', ...patch })
export const author = vm => ({ title: vm.title, directive: vm.directive, content: vm.content })
export function vnodes(vnode, predicate, result = []) {
  if (!vnode || typeof vnode !== 'object') return result
  if (predicate(vnode)) result.push(vnode)
  if (vnode.component) vnodes(vnode.component.subTree, predicate, result)
  if (Array.isArray(vnode.children)) for (const child of vnode.children) vnodes(child, predicate, result)
  return result
}
export function label(vnode) {
  const text = value => Array.isArray(value) ? value.map(text).join('') : value && typeof value === 'object' ? text(value.children) : typeof value === 'string' ? value : ''
  return text(vnode?.children?.default?.() ?? vnode?.children)
}
function instance(vnode, name) {
  if (!vnode) return null
  if (vnode.component) {
    if (vnode.type?.name === name) return vnode.component
    const found = instance(vnode.component.subTree, name); if (found) return found
  }
  if (Array.isArray(vnode.children)) for (const child of vnode.children) { const found = instance(child, name); if (found) return found }
  return null
}

export async function runtime(options = {}) {
  const key = `__manualChapterFixture${++sequence}`, dependencies = new Map(), components = {}, definitions = new Map()
  const trace = [], messages = [], dialogs = [], clipboard = [], listeners = new Map(), storage = new Map()
  const rows = new Map((options.chapters ?? [chapter()]).map(row => [row.id, structuredClone(row)]))
  const copied = text => clipboard.push(text)
  let clipboardRejected = false
  globalThis.Document = class {}; globalThis.ShadowRoot = class {}
  globalThis.document = Object.assign(new Document(), { activeElement: null })
  globalThis.window = { addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn) }, removeEventListener(name, fn) { listeners.get(name)?.delete(fn) } }
  globalThis.localStorage = { get length() { return storage.size }, key: i => [...storage.keys()][i] ?? null, getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) }
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { async writeText(text) { if (clipboardRejected) throw new Error('受控剪贴板拒绝'); copied(text) } } } })
  globalThis.fetch = async (url, config = {}) => {
    const path = String(url), method = config.method ?? 'GET', payload = config.body ? JSON.parse(config.body) : undefined
    const request = { path, method, payload }; trace.push(request)
    const controlled = await options.beforeRequest?.(request, rows)
    if (controlled instanceof Response) return controlled
    if (controlled) return new Response(JSON.stringify(controlled.body), { status: controlled.status ?? 200 })
    const target = path.match(/\/novels\/([^/]+)\/chapters\/([^/]+)$/)
    let body
    if (path === '/api/v1/novels') body = ['novel-a', 'novel-b'].map(id => ({ id, title: id, genre: '', summary: '', style_anchor: '', created_at: '', updated_at: '' }))
    else if (path.endsWith('/chapters/metadata')) body = [...rows.values()].filter(row => path.includes(`/novels/${row.novel_id}/`)).map(row => { const { content, directive, summary, ...metadata } = row; return { ...metadata, word_count: Array.from(content).length } })
    else if (target && method === 'GET') body = rows.get(target[2])
    else if (target && method === 'PUT') {
      const previous = rows.get(target[2]); if (!previous) return new Response('{"detail":"not found"}', { status: 404 })
      body = { ...previous, ...payload }; delete body.revision_source; delete body.expected_author_fields
      if (['title', 'content', 'directive'].some(field => field in payload && payload[field] !== previous[field])) Object.assign(body, { status: 'draft', summary: '', tension_score: null, drift_score: null })
      rows.set(body.id, structuredClone(body))
    } else if (target && method === 'DELETE') { rows.delete(target[2]); return new Response(null, { status: 204 }) }
    else if (/\/outline$/.test(path)) body = []
    else throw new Error(`Unexpected actual api.ts request ${method} ${path}`)
    if (!body) return new Response('{"detail":"not found"}', { status: 404 })
    const response = await options.afterResponse?.(request, body, rows)
    return new Response(JSON.stringify(response?.body ?? body), { status: response?.status ?? 200 })
  }
  const apiModule = await import(moduleURL(compile(await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')) + `\n// ${key}`))
  const harness = { ...apiModule, components, async loadComponent(name) { return components[name] }, useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map(type => [type, text => messages.push({ type, text })])), useDialog: () => ({ warning: value => dialogs.push(value) }) }
  globalThis[key] = harness
  const stub = defineComponent({ name: 'Stub', setup(_props, { slots }) { return () => h('div', [slots.default?.(), slots.footer?.()]) } })
  for (const name of ['ChatView', 'SettingsPanel', 'OutlinePanel', 'AutopilotPanel', 'ForeshadowingTracking', 'KnowledgeGraph', 'BookDeliveryPanel', 'ChapterRevisionPanel']) components[name] = defineComponent({ ...stub, name })
  for (const name of ['NButton', 'NCheckbox', 'NEmpty', 'NInput', 'NInputNumber', 'NList', 'NListItem', 'NSelect', 'NTag', 'NTabs', 'NTabPane', 'NForm', 'NFormItem']) harness[name] = defineComponent({ ...stub, name })
  harness.NModal = defineComponent({ name: 'NModal', props: ['show'], setup(props, { slots }) { return () => props.show ? h('aside', [slots.default?.(), slots.footer?.()]) : null } })
  async function dependency(name) {
    if (dependencies.has(name)) return dependencies.get(name)
    let source = await readFile(new URL(`../src/${name}.ts`, import.meta.url), 'utf8')
    source = source.replace(/from ['"]vue['"]/g, `from '${vueURL}'`)
    const result = moduleURL(compile(source) + `\n// helper ${key}/${name}`); dependencies.set(name, result); return result
  }
  async function component(name) {
    const file = name === 'App' ? '../src/App.vue' : `../src/components/${name}.vue`
    const descriptor = parse(await readFile(new URL(file, import.meta.url), 'utf8'), { filename: file }).descriptor
    const script = compileScript(descriptor, { id: key + name }), template = compileTemplate({ source: descriptor.template.content, id: key + name, filename: file, compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(template.errors, [])
    for (const match of script.content.matchAll(/import \{([^}]+)\} from ['"]naive-ui['"]/g)) for (const part of match[1].split(',')) { const n = part.trim(); if (n.startsWith('N') && !harness[n]) harness[n] = defineComponent({ ...stub, name: n }) }
    let source = `const __harness=globalThis.${key}\n` + script.content.replace('export default', 'const __component=') + '\n' + template.code.replace('export function render', 'function render') + `\n__component.name='${name}';__component.render=render;export default __component`
    source = source.replace(/from ['"]vue['"]/g, `from '${vueURL}'`).replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1}=__harness').replace(/import \{([^}]+)\} from ['"]\.\.\/api['"]/g, 'const {$1}=__harness')
      .replace(/import (\w+) from ['"]\.\/(?:components\/)?[^'"]+\.vue['"]/g, 'const $1=__harness.components.$1').replace(/import\(['"]\.\/(?:components\/)?([^'"]+)\.vue['"]\)/g, '__harness.loadComponent("$1")')
    for (const match of [...source.matchAll(/from ['"]\.\.\/(types|utils\/\w+)['"]/g)]) { const url = await dependency(match[1]); source = source.replaceAll(`from '../${match[1]}'`, `from '${url}'`).replaceAll(`from "../${match[1]}"`, `from '${url}'`) }
    components[name] = (await import(moduleURL(compile(source)))).default; definitions.set(name, components[name])
  }
  await component('WritingPanel'); await component('WorkbenchView'); await component('App')
  const remove = node => { if (node.parent) { node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null } }
  const renderer = createRenderer({
    createElement(type) { const events = new Map(), el = { type, tagName: type.toUpperCase(), props: {}, children: [], parent: null, style: {}, value: '', selectionStart: 0, selectionEnd: 0,
      addEventListener(name, fn) { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(fn) }, removeEventListener(name, fn) { events.get(name)?.delete(fn) },
      dispatchEvent(event) { event.target = el; for (const fn of events.get(event.type) ?? []) fn(event) }, getRootNode: () => document, focus() { document.activeElement = el }, select() { el.selectionStart = 0; el.selectionEnd = el.value.length } }; return el },
    createText: text => ({ type: 'text', text, parent: null }), createComment: text => ({ type: 'comment', text, parent: null }),
    insert(node, parent, anchor) { remove(node); node.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node) }, remove,
    setElementText(node, text) { node.text = text; node.children = [] }, setText(node, text) { node.text = text }, parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null, patchProp(node, key, _old, value) { node.props[key] = value },
  })
  const root = { children: [] }, app = renderer.createApp(components.App); app.mount(root); await settle()
  const find = name => instance(app._instance.vnode, name)
  return { rows, app, trace, messages, dialogs, clipboard, find, vm: (name = 'WritingPanel') => find(name)?.setupState,
    async enter(novelId = 'novel-a', chapterId = 'chapter-a') { app._instance.setupState.onOpenWriting(novelId, chapterId); await settle(); await find('WorkbenchView').setupState.loadNovels(); await find('WritingPanel').setupState.loadChapters(); await settle() },
    async switchNovel(novelId) { find('WorkbenchView').setupState.changeNovel(novelId); await settle(); if (find('WorkbenchView').setupState.showNavigationConfirm) this.button('WorkbenchView', '离开并继续').props.onClick(); await settle(); await find('WritingPanel').setupState.loadChapters(); await settle() },
    button(name, text) { const result = vnodes(find(name)?.subTree, v => v.type?.name === 'NButton' && label(v).trim() === text); assert.equal(result.length, 1, `${name} actual button ${text}`); return result[0] },
    region(text) { return vnodes(find('WritingPanel')?.subTree, v => v.props?.['aria-label'] === text)[0] },
    input(aria, value) { const node = vnodes(find('WritingPanel').subTree, v => v.type === 'textarea' && v.props?.['aria-label'] === aria)[0]; if (node) { node.el.value = value; node.el.dispatchEvent({ type: 'input' }) } else { const input = vnodes(find('WritingPanel').subTree, v => v.type?.name === 'NInput' && v.props?.['input-props']?.['aria-label'] === aria)[0]; assert(input); input.props['onUpdate:value'](value) } },
    async leave() { find('WorkbenchView').setupState.returnToChat(); await settle(); assert(find('WorkbenchView').setupState.showNavigationConfirm); this.button('WorkbenchView', '离开并继续').props.onClick(); await settle(); assert.equal(find('WritingPanel'), null) },
    beforeUnload() { const event = { prevented: false, returnValue: undefined, preventDefault() { this.prevented = true } }; for (const fn of listeners.get('beforeunload') ?? []) fn(event); return event },
    clipboardReject(value = true) { clipboardRejected = value }, dispose() { app.unmount(); delete globalThis[key] },
  }
}
