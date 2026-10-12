import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const scopeUrl = url(compile(await readFile(new URL('../src/utils/asyncScope.ts', import.meta.url), 'utf8')))
let sequence = 0
export const settle = async () => { for (let n = 0; n < 20; n++) { await Promise.resolve(); await nextTick() } }
export const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return { promise, resolve, reject } }
export const draft = () => ({ title: '原卷😀', summary: '\n　原概要🐾\n', chapters: [{ title: '直属章', summary: '原章概要' }], acts: [] })
export const receipt = requestId => ({ volume_id: 'volume', node_ids: ['volume', 'chapter'], request_id: requestId, append_replayed: false })
export async function appendCard(options = {}) {
  const props = reactive({ novelId: 'novel-a', ...options.props }), requests = [], messages = [], emitted = [], hooks = {}
  const defaults = {
    checkOutlineAppend: async () => { throw Object.assign(new Error('尚无原请求'), { status: 404, code: 'outline_append_not_found' }) },
    applyOutline: async (novelId, plan, requestId) => receipt(requestId),
    applyMessageOutline: async () => ({ volume_id: 'chat-volume', node_ids: ['chat-volume','chat-chapter'] }),
    previewOutline: async () => draft(), previewMessageOutline: async () => draft(),
  }
  const api = Object.fromEntries(Object.entries({ ...defaults, ...options.api }).map(([method, implementation]) => [method, (...args) => {
    const serialized = JSON.parse(JSON.stringify(args))
    requests.push({ method, args: serialized }); return implementation(...serialized)
  }]))
  globalThis.__outlineAppendHarness = { api, onBeforeUnmount: fn => { hooks.unmount=fn },
    useMessage: () => Object.fromEntries(['success','error','warning'].map(type => [type, text => messages.push({type,text})])),
    NButton:'nbutton', NInput:'ninput', NSpin:'nspin' }
  const descriptor = parse(await readFile(new URL('../src/components/OutlinePlanCard.vue', import.meta.url), 'utf8')).descriptor
  const compiled = compileScript(descriptor, { id: `append-${++sequence}` })
  const source = 'const __harness = globalThis.__outlineAppendHarness\n' + compiled.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/from ['"]\.\.\/utils\/asyncScope['"]/g, `from '${scopeUrl}'`)
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(url(compile(source) + `\n// instance ${sequence}`))).default
  const scope = effectScope(), vm = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  vm.sourceText.value = '原始构思'; vm.plan.value = draft(); await settle()
  return { vm, props, requests, messages, emitted, dispose: () => { hooks.unmount?.(); scope.stop() } }
}
