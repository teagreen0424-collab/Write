import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, reactive } from 'vue'
import ts from 'typescript'

const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const asyncScopeUrl = url(compile(await readFile(new URL('../src/utils/asyncScope.ts', import.meta.url), 'utf8')))
let sequence = 0
export const settle = async () => { for (let i = 0; i < 20; i += 1) { await Promise.resolve(); await nextTick() } }
export const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
export async function outlineAuthor(options = {}) {
  const props = reactive({ novelId: 'novel-a' })
  const rows = new Map(['novel-a', 'novel-b'].map(novelId => [novelId, {
    id: `${novelId}-volume`, novel_id: novelId, title: '原标题', summary: '原概要',
    level: 'volume', parent_id: '', sort_order: 0, created_at: '2026-10-08T00:00:00', updated_at: '2026-10-08T00:00:00',
  }]))
  const writes = [], messages = [], hooks = {}
  globalThis.window = { confirm: () => true, addEventListener() {}, removeEventListener() {} }
  const api = {
    listOutline: async (novelId) => [...rows.values()].filter(row => row.novel_id === novelId).map(row => structuredClone(row)),
    updateOutline: async (novelId, id, payload) => {
      writes.push({ method: 'PUT', novelId, id, payload: structuredClone(payload) })
      await options.beforeUpdate?.(payload)
      const row = [...rows.values()].find(item => item.id === id)
      assert.equal(row.novel_id, novelId)
      Object.assign(row, payload, { updated_at: '2026-10-08T00:01:00' })
      const saved = structuredClone(row)
      await options.afterUpdate?.(saved)
      return saved
    },
    createOutline: async (novelId, payload) => {
      writes.push({ method: 'POST', novelId, payload: structuredClone(payload) })
      const saved = { id: `${novelId}-created`, novel_id: novelId, sort_order: 1,
        created_at: '2026-10-08T00:02:00', updated_at: '2026-10-08T00:02:00', ...payload }
      rows.set(saved.id, saved)
      await options.afterCreate?.(saved)
      return structuredClone(saved)
    },
  }
  const harness = { api, useMessage: () => Object.fromEntries(['success', 'error', 'warning'].map(type => [type, text => messages.push({ type, text })])),
    onMounted: fn => { hooks.mount = fn }, onBeforeUnmount: fn => { hooks.unmount = fn } }
  for (const name of ['NButton', 'NEmpty', 'NForm', 'NFormItem', 'NInput', 'NModal', 'NSelect', 'NTag']) harness[name] = name.toLowerCase()
  globalThis.__outlineAuthorHarness = harness
  const original = await readFile(new URL('../src/components/OutlinePanel.vue', import.meta.url), 'utf8')
  const compiled = compileScript(parse(original).descriptor, { id: `outline-author-${++sequence}` })
  const source = 'const __harness = globalThis.__outlineAuthorHarness\n' + compiled.content
    .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g, 'const {$1} = __harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g, 'const { api } = __harness')
    .replace(/from ['"]\.\.\/utils\/asyncScope['"]/g, `from '${asyncScopeUrl}'`)
    .replace(/import OutlinePlanCard from ['"]\.\/OutlinePlanCard.vue['"]/g, 'const OutlinePlanCard = {}')
    .replace(/\bonMounted\(/g, '__harness.onMounted(')
    .replace(/\bonBeforeUnmount\(/g, '__harness.onBeforeUnmount(')
  const component = (await import(url(compile(source) + `\n// instance ${sequence}`))).default
  const scope = effectScope()
  const vm = scope.run(() => component.setup(props, { expose() {}, emit() {} }))
  await hooks.mount?.(); await settle()
  return { vm, props, rows, writes, messages,
    edit: () => vm.openEdit(vm.nodes.value[0]),
    dispose: () => { hooks.unmount?.(); scope.stop() },
  }
}
