import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { styleAnchorRuntime, settle } from './style-anchor-fixtures.mjs'

const novel = (id='a',patch={}) => ({id,title:id,genre:'原题材',summary:'原梗概',style_anchor:`${id}原基准`,created_at:'2026-10-08',updated_at:'2026-10-08',...patch})
const wait = () => { let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve} }
const vm = c => c.instance('SettingsPanel').setupState
const wb = c => c.instance('WorkbenchView').setupState
function findVNode(node, predicate) {
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  if (node.component) { const found = findVNode(node.component.subTree, predicate); if (found) return found }
  if (Array.isArray(node.children)) for (const child of node.children) { const found = findVNode(child, predicate); if (found) return found }
  return null
}
const inputProps = (c, label) => {
  const node = findVNode(c.instance('SettingsPanel').subTree, node => (node.props?.['input-props'] ?? node.props?.inputProps)?.['aria-label'] === label)
  assert.ok(node, `actual compiled template input ${label}`)
  return { ...node.props, inputProps: node.props['input-props'] ?? node.props.inputProps }
}
async function enter(c, id) {
  wb(c).changeNovel(id); if (wb(c).showNavigationConfirm) wb(c).confirmNavigation(); await settle()
  assert.equal(wb(c).novelId, id)
}
async function leave(c) {
  wb(c).returnToChat(); if (wb(c).showNavigationConfirm) wb(c).confirmNavigation(); await settle()
  assert.equal(c.instance('SettingsPanel'), null)
}
async function mounted(overrides={},options={}) {
  const c=await styleAnchorRuntime({app:true,...options,api:{listNovels:async()=>[novel(),novel('b')],listEntities:async()=>[],listChapterMetadata:async()=>[],updateNovel:async(id,payload)=>novel(id,payload),...overrides}})
  c.vm.onEnterWorkbench('a');await settle();return c
}

test('文风同作品等待锁跨真实App卸载重挂阻止第二PUT，旧完成保新输入且无旧toast',async()=>{
  const gate=wait();let calls=0,c
  try {
    c=await mounted({updateNovel:async(id,payload)=>++calls===1?gate.promise:novel(id,payload)})
    const old=vm(c);old.styleAnchor='原提交';const pending=old.saveAnchor()
    c.instance('WorkbenchView').setupState.returnToChat();c.instance('WorkbenchView').setupState.confirmNavigation();await settle()
    assert.equal(c.instance('WorkbenchView'),null);c.vm.onEnterWorkbench('a');await settle()
    const current=vm(c);current.styleAnchor='重挂作者新稿';await current.saveAnchor();assert.equal(calls,1)
    gate.resolve(novel('a',{style_anchor:'原提交'}));await pending
    assert.equal(current.styleAnchor,'重挂作者新稿');assert.equal(current.anchorBaseline,'原提交');assert.equal(current.dirty,true)
    assert.equal(c.messages.some(row=>row.type==='success'),false)
  } finally {gate.resolve(novel('a',{style_anchor:'原提交'}));c?.dispose();await settle()}
})

test('文风损坏或来源错误的200不能推进已保存基线，保原提交与当前输入',async()=>{
  for(const response of [{},novel('b',{style_anchor:'原提交'}),novel('a',{style_anchor:'错原文'}),novel('a',{style_anchor:'原提交',genre:null})]){
    const c=await mounted({updateNovel:async()=>response});try {
      const p=vm(c);p.styleAnchor='原提交';await p.saveAnchor()
      assert.equal(p.anchorBaseline,'a原基准');assert.equal(p.styleAnchor,'原提交');assert.equal(p.anchorState.phase,'unknown');assert.equal(p.anchorState.original,'原提交')
    }finally{c.dispose()}
  }
})

test('文风unknown不能盲第二PUT，原提交和后来稿独立保留供作者核对',async()=>{
  let count=0;const c=await mounted({updateNovel:async()=>{count++;throw new Error('原200传输丢失')}})
  try{
    const p=vm(c);p.styleAnchor='原提交';await p.saveAnchor();p.styleAnchor='后来唯一稿';await p.saveAnchor()
    assert.equal(count,1);assert.equal(p.anchorState.phase,'unknown');assert.equal(p.anchorState.original,'原提交');assert.equal(p.styleAnchor,'后来唯一稿');assert.equal(p.dirty,true)
  }finally{c.dispose()}
})

test('文风保存只发原始style_anchor，后来稿与编辑改回值不被旧确认清空', async () => {
  const gate=wait(), calls=[], first=' \n原始文风😀\0\r\n ', later='后来唯一文风😀\0'
  const c=await mounted({updateNovel:async(id,payload)=>{calls.push({id,payload});return gate.promise}})
  try {
    const p=vm(c);p.styleAnchor=first;const pending=p.saveAnchor();p.styleAnchor=later;p.styleAnchor=first;p.styleAnchor=later
    assert.deepEqual(calls,[{id:'a',payload:{style_anchor:first}}]);gate.resolve(novel('a',{style_anchor:first,genre:'外部真实新题材',summary:'外部真实新梗概'}));await pending
    assert.equal(p.styleAnchor,later);assert.equal(p.anchorBaseline,first);assert.equal(p.anchorState.saved.genre,'外部真实新题材');assert.equal(p.dirty,true)
    assert.equal(inputProps(c,'原提交文风基准').value,first);assert.equal(inputProps(c,'当前文风基准').value,later)
  } finally {gate.resolve(novel('a',{style_anchor:first}));c.dispose()}
})

test('同作品ABA仍单等待，不同作品可独立保存且旧A响应不清B表单或提示', async () => {
  const gate=wait(), calls=[], rows=[novel(),novel('b')];const c=await mounted({listNovels:async()=>structuredClone(rows),updateNovel:async(id,payload)=>{calls.push({id,payload});const saved=id==='a'?await gate.promise:novel(id,payload);Object.assign(rows.find(row=>row.id===id),saved);return saved}})
  try {
    const a=vm(c);a.styleAnchor='A原提交';const pending=a.saveAnchor();await enter(c,'b')
    const b=vm(c);b.styleAnchor='B明确新基准';await b.saveAnchor();assert.equal(b.dirty,false);assert.equal(b.anchorState.phase,'saved')
    await enter(c,'a');vm(c).styleAnchor='A返回后来稿';await vm(c).saveAnchor();assert.equal(calls.length,2);await enter(c,'b')
    vm(c).form.name='B未保存人物';const before=c.messages.length;gate.resolve(novel('a',{style_anchor:'A原提交'}));await pending
    assert.equal(b.styleAnchor,'B明确新基准');assert.equal(b.form.name,'B未保存人物');assert.equal(c.messages.length,before)
    await enter(c,'a');assert.equal(vm(c).anchorBaseline,'A原提交');assert.equal(vm(c).styleAnchor,'A返回后来稿');assert.equal(calls.length,2)
  } finally {gate.resolve(novel('a',{style_anchor:'A原提交'}));c.dispose()}
})

test('unknown明确读取当前文风不冒认原回执，采用仅推进编辑基线且新稿仍须另点保存', async () => {
  let rows=[novel(),novel('b')],calls=0;const c=await mounted({listNovels:async()=>rows,updateNovel:async(id,payload)=>{if(++calls===1)throw new Error('原回应未知');return novel(id,payload)}})
  try {
    const p=vm(c);p.styleAnchor='未知原提交';await p.saveAnchor();p.styleAnchor='后来作者稿'
    rows=[novel('a',{style_anchor:'人工读取所得当前文风'}),novel('b')];await p.checkAnchor()
    assert.equal(p.anchorState.phase,'unknown');assert.equal(p.anchorState.saved,null);assert.equal(p.anchorBaseline,'a原基准');assert.equal(p.styleAnchor,'后来作者稿');assert.equal(calls,1)
    assert.equal(inputProps(c,'核对所得当前文风').value,'人工读取所得当前文风')
    p.adoptCurrentAnchor();assert.equal(calls,1);assert.equal(p.anchorBaseline,'人工读取所得当前文风');assert.equal(p.styleAnchor,'后来作者稿');assert.equal(p.anchorState.original,'未知原提交');assert.match(p.anchorState.error,/原请求结果仍未知/)
    await p.saveAnchor();assert.equal(calls,2);assert.equal(p.anchorState.saved.style_anchor,'后来作者稿');assert.equal(p.dirty,false)
  } finally {c.dispose()}
})

test('未知文风读取失败或损坏资料不开放盲保存，采用取消不修改原提交和新稿', async () => {
  for (const result of ['failure','invalid','cancel']) {
    let reads=0,writes=0;const c=await mounted({listNovels:async()=>{if(++reads<=2)return [novel(),novel('b')];if(result==='failure')throw new Error('当前资料读取失败');return [result==='invalid'?novel('a',{summary:null}):novel('a',{style_anchor:'当前读到的文风'})]},updateNovel:async()=>{writes++;throw new Error('未知')}},{confirm:()=>false})
    try {
      const p=vm(c);p.styleAnchor='原提交';await p.saveAnchor();p.styleAnchor='当前后来稿';await p.checkAnchor();p.adoptCurrentAnchor();await p.saveAnchor()
      assert.equal(writes,1);assert.equal(p.anchorState.phase,'unknown');assert.equal(p.anchorState.original,'原提交');assert.equal(p.styleAnchor,'当前后来稿');assert.equal(p.anchorBaseline,'a原基准')
      assert.equal(p.anchorState.observed!==null,result==='cancel')
    } finally {c.dispose()}
  }
})

test('已初始化clean文风真实App重挂读取新当前资料，已有作者稿仍不被刷新替换', async () => {
  let rows=[novel(),novel('b')];const c=await mounted({listNovels:async()=>rows})
  try {
    assert.equal(vm(c).dirty,false);await leave(c);rows=[novel('a',{style_anchor:'外部已保存新文风'}),novel('b')];c.vm.onEnterWorkbench('a');await settle()
    const p=vm(c);assert.equal(p.styleAnchor,'外部已保存新文风');assert.equal(p.anchorBaseline,'外部已保存新文风');assert.equal(p.dirty,false)
    p.styleAnchor='作者未保存独立稿';rows=[novel('a',{style_anchor:'外部再保存文风'}),novel('b')];await p.loadNovelMeta()
    assert.equal(p.styleAnchor,'作者未保存独立稿');assert.equal(p.anchorBaseline,'外部再保存文风');assert.equal(p.dirty,true)
  } finally {c.dispose()}
})

test('保存等待期间启动的旧metadata不覆盖后来已确认基线与当前新稿', async () => {
  const update=wait(), meta=wait();let reads=0;const c=await mounted({listNovels:async()=>++reads<=2?[novel(),novel('b')]:meta.promise,updateNovel:async()=>update.promise})
  try {
    const p=vm(c);p.styleAnchor='原提交';const pending=p.saveAnchor();const reading=p.loadNovelMeta();p.styleAnchor='后来唯一稿'
    update.resolve(novel('a',{style_anchor:'原提交'}));await pending;meta.resolve([novel(),novel('b')]);await reading
    assert.equal(p.anchorBaseline,'原提交');assert.equal(p.styleAnchor,'后来唯一稿');assert.equal(p.anchorState.phase,'saved');assert.equal(p.dirty,true)
  } finally {update.resolve(novel('a',{style_anchor:'原提交'}));meta.resolve([novel(),novel('b')]);c.dispose()}
})

test('旧metadata存在事实不能推翻明确当前读取所得的删除事实，禁止复活保存', async () => {
  const meta=wait();let reads=0,writes=0;const c=await mounted({listNovels:async()=>{reads++;return reads<=2?[novel(),novel('b')]:reads===3?meta.promise:[novel('b')]},updateNovel:async(id,payload)=>{writes++;return novel(id,payload)}})
  try {
    const p=vm(c);p.styleAnchor='原提交';await p.saveAnchor();const reading=p.loadNovelMeta();await p.checkAnchor();assert.equal(p.anchorState.exists,false)
    meta.resolve([novel(),novel('b')]);await reading;p.styleAnchor='已删作品后来稿';await p.saveAnchor()
    assert.equal(p.anchorState.exists,false);assert.equal(writes,1);assert.equal(p.anchorState.original,'原提交');assert.equal(p.styleAnchor,'已删作品后来稿')
  } finally {meta.resolve([novel(),novel('b')]);c.dispose()}
})

test('其他作品与已删来源的页内文风有实际只读复制入口，B本身保持clean且可独立写', async () => {
  const copied=[];let rows=[novel(),novel('b')],writes=0;const c=await mounted({listNovels:async()=>rows,updateNovel:async(id,payload)=>{writes++;return novel(id,payload)}},{copied})
  try {
    const a=vm(c);a.styleAnchor='A原提交';await a.saveAnchor();a.styleAnchor='A后来不可遗失稿';rows=[novel('b')];await a.checkAnchor();assert.equal(a.anchorState.exists,false)
    await enter(c,'b');const b=vm(c);assert.equal(b.dirty,false);assert.equal(b.otherAnchorDrafts.length,1)
    assert.equal(inputProps(c,'其他作品当前文风 · a').value,'A后来不可遗失稿');assert.equal(inputProps(c,'其他作品原提交文风 · a').value,'A原提交')
    const state=b.otherAnchorDrafts[0];await b.copyAnchor(false,state);await b.copyAnchor(true,state);assert.deepEqual(copied,['A后来不可遗失稿','A原提交']);assert.equal(writes,1)
    b.styleAnchor='B明确文风';await b.saveAnchor();assert.equal(writes,2);assert.equal(b.anchorState.saved.id,'b');assert.equal(state.exists,false)
  } finally {c.dispose()}
})

test('A文风pending或unknown在真实Settings卸载回Chat后仍阻整页离开，Bclean不误标dirty', async () => {
  for (const phase of ['pending','unknown']) {
    const gate=wait();const c=await mounted({updateNovel:async()=>phase==='pending'?gate.promise:Promise.reject(new Error('unknown'))})
    let pending
    try {
      const a=vm(c);a.styleAnchor='A原提交';pending=a.saveAnchor();if(phase==='unknown')await pending
      await leave(c);let blocked=false;for(const listener of c.listeners.get('beforeunload')??[])listener({preventDefault(){blocked=true}})
      assert.equal(blocked,true);c.vm.onEnterWorkbench('b');await settle();assert.equal(vm(c).dirty,false);assert.equal(vm(c).otherAnchorDrafts[0].phase,phase)
    } finally {gate.resolve(novel('a',{style_anchor:'A原提交'}));await pending;c.dispose()}
  }
})

test('文风复制失败提供原生只读手动回退，原提交与当前稿的inputProps名称和原文均正确', async () => {
  const first=' \n原提交😀\0\r\n ',later='后来文风😀\0';const c=await mounted({updateNovel:async()=>{throw new Error('未知')}},{clipboardError:true})
  try {
    const p=vm(c);p.styleAnchor=first;await p.saveAnchor();p.styleAnchor=later;await p.copyAnchor();await settle()
    assert.equal(p.showAnchorCopy,true);assert.equal(p.anchorCopy,later);const current=inputProps(c,'手动复制当前文风');assert.ok(current.readonly===true||current.readonly==='');assert.equal(current.value,later)
    await p.copyAnchor(true);await settle();const original=inputProps(c,'手动复制原提交文风');assert.ok(original.readonly===true||original.readonly==='');assert.equal(original.value,first)
    const oldWindow=globalThis.window;delete globalThis.window;const {NInput}=await import('naive-ui');globalThis.window=oldWindow
    for(const label of ['当前文风基准','原提交文风基准','手动复制原提交文风']){
      const props=inputProps(c,label);const html=await renderToString(createSSRApp({render:()=>h(NInput,props)}));assert.match(html,new RegExp(`<(?:textarea|input)[^>]+aria-label="${label}"`));if(label!=='当前文风基准')assert.match(html,/<(?:textarea|input)[^>]+readonly/)
    }
  } finally {c.dispose()}
})
