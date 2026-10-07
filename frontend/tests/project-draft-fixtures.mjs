import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
let sequence = 0
export const settle = async () => { for (let i = 0; i < 20; i++) { await Promise.resolve(); await nextTick() } }
export const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return { promise, resolve, reject } }
export const fields = (summary = '原梗概') => ({ title: '原书名', genre: '故事', summary })
export const novel = (patch = {}) => ({ id: 'novel-saved', ...fields(), style_anchor: '文风不变', created_at: '', updated_at: '', ...patch })
export const conversation = id => ({ id, novel_id: null, title: `会话${id}`, created_at: '', updated_at: '' })
export function nodes(value, predicate, result = []) {
  if (!value || typeof value !== 'object') return result
  if (predicate(value)) result.push(value)
  if (Array.isArray(value.children)) value.children.forEach(item => nodes(item, predicate, result))
  else if (value.children && typeof value.children === 'object') for (const slot of Object.values(value.children)) if (typeof slot === 'function') {
    const children = slot(); (Array.isArray(children) ? children : [children]).forEach(item => nodes(item, predicate, result))
  }
  return result
}

export async function projectRuntime(overrides = {}, options = {}) {
  const calls = [], contexts = [], copied = [], key = `__projectDraft${++sequence}`
  let active
  const api = {
    listNovels: async () => [], listConversations: async () => [],
    createNovel: async data => novel(data), promoteConversation: async (_id, data) => novel(data),
    updateNovel: async (_id, data) => novel(data), ...overrides,
  }
  const harness = { api: Object.fromEntries(Object.entries(api).map(([name, fn]) => [name, (...args) => { calls.push({ name, args }); return fn(...args) }])),
    useMessage: () => { const context = active; return Object.fromEntries(['success','warning','error'].map(type => [type, text => context.notices.push({ type, text })])) },
    useDialog: () => { const context = active; return { warning: settings => context.dialogs.push(settings) } },
    onMounted: fn => active.hooks.mount = fn, onBeforeUnmount: fn => active.hooks.unmount = fn,
  }
  for (const name of ['NButton','NForm','NFormItem','NInput','NModal','NSelect']) harness[name] = { name }
  globalThis[key] = harness
  const helpers = {}
  for (const name of ['chatSubmission', 'projectDraft']) helpers[name] = url(compile((await readFile(new URL(`../src/utils/${name}.ts`, import.meta.url),'utf8'))
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)) + `\n// ${key}`)
  const descriptor = parse(await readFile(new URL('../src/components/ChatView.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: key })
  const source = `const __harness=globalThis.${key}\n` + script.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1}=__harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const {api}=__harness')
    .replace(/from ['"]\.\.\/utils\/(chatSubmission|projectDraft)['"]/g, (_all, name) => `from '${helpers[name]}'`)
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g, 'const $1={name:"$1"}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(').replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const definition = (await import(url(compile(source)))).default
  const template = compileTemplate({ source: descriptor.template.content, filename: 'ChatView.vue', id: key, compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const rendering = await import(url(compile(template.code.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`))))
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => { if (options.clipboardError) throw new Error('clipboard blocked'); copied.push(text) } } } })
  async function mount() {
    const listeners = new Map(), context = { hooks: {}, notices: [], dialogs: [], events: [], listeners }
    globalThis.window = { addEventListener: (name, fn) => listeners.set(name,fn), removeEventListener: name => listeners.delete(name) }
    active = context; const scope = effectScope()
    context.vm = scope.run(() => definition.setup(reactive({}), { expose() {}, emit: (...args) => context.events.push(args) }))
    context.hooks.mount?.(); await settle()
    context.render = () => rendering.render({}, [], {}, proxyRefs(context.vm), {}, {})
    context.stop = () => { context.hooks.unmount?.(); scope.stop() }
    contexts.push(context); return context
  }
  return { mount, calls, copied, helper: await import(helpers.projectDraft), stop: () => contexts.forEach(context => context.stop()) }
}
