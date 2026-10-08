import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { effectScope, nextTick, proxyRefs, reactive } from 'vue'
import ts from 'typescript'

const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const moduleURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const vueURL = import.meta.resolve('vue')
let sequence = 0
export const settle = async () => { for (let i = 0; i < 25; i++) { await Promise.resolve(); await nextTick() } }
export const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return { promise, resolve, reject } }
export const chapter = (novelId='novel-a', patch={}) => ({ id: `${novelId}-chapter`, novel_id: novelId, seq: 1, title: '原章', content: '已保存正文', directive: '', outline_node_id: '', summary: '', status: 'draft', tension_score: null, drift_score: null, created_at: '', updated_at: '', ...patch })
export function nodes(node, predicate, result=[]) { if (node && typeof node === 'object') { if (predicate(node)) result.push(node); if (Array.isArray(node.children)) for (const child of node.children) nodes(child,predicate,result) } return result }

export async function cardRuntime(options={}) {
  const calls=[], notices=[], copied=[]
  let active
  const implementations = { listChapterMetadata: async id => [chapter(id)], listOutline: async () => [], writeMessageChapter: async () => {}, ...options.api }
  const harness = { api: Object.fromEntries(Object.entries(implementations).map(([name, fn]) => [name, (...args) => { calls.push({name,args}); return fn(...args) }])),
    useMessage: () => { const context=active; return Object.fromEntries(['success','warning','error','info'].map(type=>[type,text=>{ const notice={type,text}; context.notices.push(notice); notices.push(notice) }])) },
    useDialog: () => { const context=active; return { warning: settings => context.dialogs.push(settings) } },
    onMounted: fn => active.hooks.mount=fn, onBeforeUnmount: fn => active.hooks.unmount=fn,
  }
  for(const name of ['NButton','NInput','NSelect','NTag']) harness[name]=name.toLowerCase()
  const key=`__chapterGeneration${++sequence}`; globalThis[key]=harness
  const helper=await readFile(new URL('../src/utils/chapterGeneration.ts',import.meta.url),'utf8')
  const helperURL=moduleURL(compile(helper.replace(/from ['"]vue['"]/g,`from '${vueURL}'`))+`\n// ${key}`)
  const consistencyURL=moduleURL(compile(await readFile(new URL('../src/utils/generationConsistency.ts',import.meta.url),'utf8')))
  async function definition(name) {
  const descriptor=parse(await readFile(new URL(`../src/components/${name}.vue`,import.meta.url),'utf8')).descriptor
  const compiled=compileScript(descriptor,{id:key})
  let source=`const __harness=globalThis.${key}\n`+compiled.content.replace(/from ['"]vue['"]/g,`from '${vueURL}'`)
    .replace(/import \{([^}]+)\} from ['"]naive-ui['"]/g,'const {$1}=__harness')
    .replace(/import \{ api \} from ['"]\.\.\/api['"]/g,'const {api}=__harness')
    .replace(/import (\w+) from ['"]\.\/[^'"]+\.vue['"]/g,'const $1={name:"$1"}')
    .replace(/\bonMounted\(/g,'__harness.onMounted(').replace(/\bonBeforeUnmount\(/g,'__harness.onBeforeUnmount(')
  if(source.includes('../utils/chapterGeneration')) {
    source=source.replace(/from ['"]\.\.\/utils\/chapterGeneration['"]/g,`from '${helperURL}'`)
  }
  source=source.replace(/from ['"]\.\.\/utils\/generationConsistency['"]/g,`from '${consistencyURL}'`)
  const component=(await import(moduleURL(compile(source)+`\n// ${key}`))).default
  const template=compileTemplate({source:descriptor.template.content,id:key,filename:name+'.vue',compilerOptions:{bindingMetadata:compiled.bindings}})
  assert.deepEqual(template.errors,[])
  const rendering=await import(moduleURL(compile(template.code.replace(/from ['"]vue['"]/g,`from '${vueURL}'`))))
  return { component, rendering }
  }
  const card=await definition('ChapterWriteCard'), bubble=await definition('MessageBubble')
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>{if(options.clipboardError)throw new Error('clipboard denied');copied.push(text)}}}})
  async function mountDefinition({component,rendering},initial,onEmit) {
    const context={props:reactive(initial),hooks:{},notices:[],dialogs:[],events:[]}
    active=context; const scope=effectScope()
    context.vm=scope.run(()=>component.setup(context.props,{expose(){},emit:(...args)=>{context.events.push(args);onEmit?.(...args)}}))
    context.hooks.mount?.(); await settle()
    context.render=()=>rendering.render({},[],context.props,proxyRefs(context.vm),{}, {})
    context.stop=()=>{context.hooks.unmount?.();scope.stop()}
    return context
  }
  const mount=(patch={},onEmit)=>mountDefinition(card,{novelId:'novel-a',conversationId:'conversation-a',messageId:'message-a',sourceText:'作者构思',...patch},onEmit)
  const mountBubble=patch=>mountDefinition(bubble,{novelId:'novel-a',message:{id:'message-a',conversation_id:'conversation-a',role:'assistant',content:'作者构思',meta:null,created_at:''},actions:[],actionsStatus:'ready',...patch})
  return { mount,mountBubble,calls,notices,copied }
}
