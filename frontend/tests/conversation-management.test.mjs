import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { conversation, deferred, nodes, projectRuntime, settle } from './project-draft-fixtures.mjs'

const source = (id = 'a', patch = {}) => ({ ...conversation(id), ...patch })
async function runtime(overrides = {}, options = {}) {
  const r = await projectRuntime({ listConversations: async () => [source('a'), source('b')],
    updateConversation: async (id, payload) => source(id, payload),
    createConversation: async novelId => source('created', { novel_id: novelId }), ...overrides }, options)
  const c = await r.mount(); await c.vm.loadAll(); return { r, c }
}

test('重命名已存原提交后保留后来title，关闭再开保稿且原record跨真实setup共享锁', { timeout: 5000 }, async () => {
  const gate = deferred(), { r, c } = await runtime({ updateConversation: () => gate.promise })
  c.vm.openRename(source()); c.vm.renameTitle.value = '原提交'; const pending = c.vm.submitRename()
  c.vm.renameTitle.value = '后来唯一title'; c.vm.closeRename(); c.dialogs.at(-1).onPositiveClick(); c.stop()
  const current = await r.mount(); await current.vm.loadAll(); current.vm.openRename(source()); await current.vm.submitRename()
  assert.equal(r.calls.filter(row => row.name === 'updateConversation').length, 1)
  gate.resolve(source('a', { title: '原提交' })); await pending
  assert.equal(current.vm.renameTitle.value, '后来唯一title'); assert.equal(current.vm.renameState.value.saved.title, '原提交')
  assert.equal(current.vm.renameTarget.value.id, 'a'); assert.equal(c.notices.length, 0); r.stop()
})

test('重命名A迟200不关闭B新稿，sameitem第二handler不发HTTP且不同cid独立', { timeout: 5000 }, async () => {
  const gates = { a: deferred(), b: deferred() }, { r, c } = await runtime({ updateConversation: id => gates[id].promise })
  c.vm.openRename(source('a')); c.vm.renameTitle.value = 'A提交'; const a = c.vm.submitRename(); const duplicate = c.vm.submitRename()
  assert.equal(r.calls.filter(row => row.name === 'updateConversation').length, 1)
  c.vm.openRename(source('b')); c.vm.renameTitle.value = 'B提交'; const b = c.vm.submitRename(); c.vm.renameTitle.value = 'B新稿'
  gates.a.resolve(source('a', { title: 'A提交' })); await a; await duplicate
  assert.equal(c.vm.renameTarget.value.id, 'b'); assert.equal(c.vm.renameTitle.value, 'B新稿'); assert.notEqual(c.vm.renameState.value.pending, null)
  gates.b.resolve(source('b', { title: 'B提交' })); await b
  assert.deepEqual(r.calls.filter(row => row.name === 'updateConversation').map(row => row.args), [['a',{ title:'A提交' }],['b',{ title:'B提交' }]])
  assert.equal(c.vm.renameTitle.value, 'B新稿'); r.stop()
})

test('新会话同入口pending仅POST一次，作者另选B后旧201不抢选择，known先采用再目录失败', { timeout: 5000 }, async () => {
  const gate = deferred(); let fail = false
  const { r, c } = await runtime({ createConversation: () => gate.promise, listConversations: async () => { if (fail) throw new Error('后置目录不可读'); return [source('a'),source('b')] } })
  const pending = c.vm.newGlobal(), duplicate = c.vm.newGlobal()
  assert.equal(r.calls.filter(row => row.name === 'createConversation').length, 1)
  c.vm.select('b'); fail = true; gate.resolve(source('created')); await pending; await duplicate
  assert.equal(r.calls.filter(row => row.name === 'createConversation').length, 1)
  assert.equal(c.vm.selectedId.value, 'b'); assert.ok(c.vm.conversations.value.some(row => row.id === 'created'))
  assert.equal(c.vm.creationRecords.value[0].phase, 'saved'); assert.equal(c.vm.creationRecords.value[0].saved.id, 'created')
  assert.equal(c.notices.length, 0); r.stop()
})

test('新会话当前来源201先采用完整五字段，后置目录失败仍known且可明确打开', async () => {
  let fail = false
  const { r, c } = await runtime({ createConversation: async () => { fail = true; return source('created') }, listConversations: async () => { if (fail) throw new Error('后置目录不可读'); return [source()] } })
  await c.vm.newGlobal()
  assert.equal(c.vm.creationRecords.value[0].phase, 'saved'); assert.equal(c.vm.selected.value.id, 'created')
  assert.ok(c.notices.some(row => /已创建.*目录/.test(row.text)))
  c.vm.select('a'); c.vm.openCreatedConversation(c.vm.creationRecords.value[0]); assert.equal(c.vm.selectedId.value, 'created')
  assert.equal(r.calls.filter(row => row.name === 'createConversation').length, 1); r.stop()
})

test('重命名保存等待改回baseline仍是未保存作者修改，下一次仅明确送改回值', async () => {
  const gate = deferred(); let count = 0
  const { r, c } = await runtime({ updateConversation: async (id,payload) => ++count === 1 ? gate.promise : source(id,payload) })
  c.vm.openRename(source()); c.vm.renameTitle.value = '本次提交'; const pending = c.vm.submitRename()
  c.vm.renameTitle.value = 'a'; gate.resolve(source('a',{title:'本次提交'})); await pending
  assert.equal(c.vm.renameTitle.value,'a'); assert.equal(c.vm.renameState.value.baseline,'本次提交'); assert.equal(c.vm.managerDirty.value,true)
  await c.vm.submitRename(); await c.vm.submitRename()
  assert.deepEqual(r.calls.filter(row=>row.name==='updateConversation').map(row=>row.args),[['a',{title:'本次提交'}],['a',{title:'a'}]])
  assert.equal(c.vm.managerDirty.value,false); r.stop()
})

test('重命名A→B→A同共享record仍busy，旧成功不清新输入或旧toast', async () => {
  const gate=deferred(),{r,c}=await runtime({updateConversation:()=>gate.promise})
  c.vm.openRename(source());c.vm.renameTitle.value='A提交';const pending=c.vm.submitRename()
  c.vm.openRename(source('b'));c.vm.openRename(source());c.vm.renameTitle.value='回A新输入';await c.vm.submitRename()
  gate.resolve(source('a',{title:'A提交'}));await pending
  assert.equal(c.vm.renameTitle.value,'回A新输入');assert.equal(c.vm.renameState.value.saved.title,'A提交');assert.equal(c.notices.length,0)
  assert.equal(r.calls.filter(row=>row.name==='updateConversation').length,1);r.stop()
})

test('重命名unknown不能按当前相同title冒回执；明确采用资料保持稿，再次保存仅真实变化', async () => {
  const {r,c}=await runtime({updateConversation:async()=>{throw Object.assign(new Error('已提交但回应未知'),{status:503})},listConversations:async()=>[source('a',{title:'原未知提交'})]})
  c.vm.openRename(source());c.vm.renameTitle.value='原未知提交';await c.vm.submitRename();c.vm.renameTitle.value='后来新稿'
  await c.vm.submitRename();await c.vm.checkRename()
  assert.equal(c.vm.renameState.value.phase,'unknown');assert.equal(c.vm.renameState.value.saved,null);assert.equal(c.vm.renameTitle.value,'后来新稿')
  c.vm.continueRename();const dialog=c.dialogs.at(-1);assert.match(dialog.content,/不证明原提交/)
  c.vm.renameTitle.value='确认期间修改';c.vm.renameTitle.value='后来新稿';assert.equal(dialog.onPositiveClick(),false)
  c.vm.continueRename();c.dialogs.at(-1).onPositiveClick();assert.equal(c.vm.renameState.value.phase,'idle')
  assert.equal(c.vm.renameState.value.baseline,'原未知提交');assert.equal(c.vm.renameTitle.value,'后来新稿')
  assert.equal(r.calls.filter(row=>row.name==='updateConversation').length,1);r.stop()
})

test('重命名bad五字段/错ID/错title都unknown，4xx也保稿而不盲重发', async () => {
  for(const result of [null,{},source('b',{title:'提交'}),source('a',{title:'错标题'}),source('a',{title:'提交',created_at:null})]){
    const {r,c}=await runtime({updateConversation:async()=>result});c.vm.openRename(source());c.vm.renameTitle.value='提交';await c.vm.submitRename()
    assert.equal(c.vm.renameState.value.phase,'unknown');assert.equal(c.vm.renameState.value.saved,null);assert.equal(c.vm.renameTitle.value,'提交');await c.vm.submitRename()
    assert.equal(r.calls.filter(row=>row.name==='updateConversation').length,1);r.stop()
  }
  const {r,c}=await runtime({updateConversation:async()=>{throw Object.assign(new Error('400尚未核对'),{status:400})}})
  c.vm.openRename(source());c.vm.renameTitle.value='原提交';await c.vm.submitRename();assert.equal(c.vm.renameState.value.phase,'unknown');r.stop()
})

test('重命名known200先采用完整原实体，目录失败不变unknown、旧read不能回滚已确认title', async () => {
  let read=0;const old=deferred(),{r,c}=await runtime({listConversations:async()=> ++read===3?old.promise:read===4?Promise.reject(new Error('后置列表500')):[source()]})
  c.vm.openRename(source());const pendingRead=c.vm.loadAll();c.vm.renameTitle.value='已保存新title';await c.vm.submitRename()
  assert.equal(c.vm.renameState.value.phase,'saved');assert.equal(c.vm.conversations.value.find(row=>row.id==='a').title,'已保存新title')
  old.resolve([source()]);await pendingRead
  assert.equal(c.vm.conversations.value.find(row=>row.id==='a').title,'已保存新title');assert.ok(c.notices.some(row=>/标题已保存.*目录/.test(row.text)));r.stop()
})

test('明读缺失源保恢复/copy入口但禁PATCH，核对A的迟read不改B observed', async () => {
  let reads=0;const gate=deferred(),{r,c}=await runtime({listConversations:async()=>++reads<=2?[source(),source('b')]:reads===3?gate.promise:[]})
  c.vm.openRename(source());c.vm.renameTitle.value='A未保存';const checking=c.vm.checkRename();const a=c.vm.renameState.value
  c.vm.openRename(source('b'));gate.resolve([source('a',{title:'A当前'}),source('b',{title:'B当前'})]);await checking
  assert.equal(a.observed.title,'A当前');assert.equal(c.vm.renameState.value.observed,null);assert.equal(c.notices.length,0)
  await c.vm.checkRename();c.vm.renameTitle.value='B尚需保留';await c.vm.submitRename();await c.vm.copyRename()
  assert.equal(c.vm.renameState.value.exists,false);assert.equal(r.calls.filter(row=>row.name==='updateConversation').length,0);assert.equal(r.copied.at(-1),'B尚需保留')
  assert.ok(nodes(c.render(),node=>node.type?.name==='NButton'&&JSON.stringify(node.children?.default?.()).includes('会话标题草稿')).length);r.stop()
})

test('关闭确认title editback保护、空白dirty导航beforeunload保留且copy空白精确', async () => {
  const {r,c}=await runtime();c.vm.openRename(source());c.vm.renameTitle.value=' \n　';c.vm.closeRename();const dialog=c.dialogs.at(-1)
  c.vm.renameTitle.value='另改';c.vm.renameTitle.value=' \n　';assert.equal(dialog.onPositiveClick(),false);assert.equal(c.vm.renameTarget.value.id,'a')
  let prevented=false;c.listeners.get('beforeunload')({preventDefault:()=>prevented=true});assert.equal(prevented,true)
  c.vm.enterWorkbench('novel-a');assert.equal(c.events.length,0);assert.match(c.dialogs.at(-1).content,/会话标题稿/)
  await c.vm.copyRename();assert.equal(r.copied.at(-1),' \n　');c.vm.closeRename();c.dialogs.at(-1).onPositiveClick();c.vm.openRename(source());assert.equal(c.vm.renameTitle.value,' \n　');r.stop()
})

test('重命名空title是明确清空可保存，复制失败仍提供原生可访问readonly完整文本', async () => {
  const {r,c}=await runtime({}, {clipboardError:true});c.vm.openRename(source());c.vm.renameTitle.value='';await c.vm.submitRename()
  assert.deepEqual(r.calls.find(row=>row.name==='updateConversation').args,['a',{title:''}]);await c.vm.copyRename(true)
  assert.equal(c.vm.showTitleCopy.value,true);assert.equal(c.vm.titleCopy.value,'')
  const box=nodes(c.render(),node=>node.props?.['input-props']?.['aria-label']==='手动复制会话标题')[0];assert.ok(box);assert.equal(Object.hasOwn(box.props,'readonly'),true)
  c.vm.renameTitle.value=' \n😀\u0000唯一标题　';await c.vm.copyRename();assert.equal(c.vm.titleCopy.value,' \n😀\u0000唯一标题　');r.stop()
})

test('创建global/novel两个entry独立；pending跨close/remount0重复，原global晚回不抢当前B', async () => {
  const gates={global:deferred(),book:deferred()},r=await projectRuntime({listNovels:async()=>[{id:'book',title:'书'}],listConversations:async()=>[source('b')],createConversation:id=>gates[id??'global'].promise})
  const old=await r.mount();await old.vm.loadAll();const global=old.vm.newGlobal();old.stop()
  const current=await r.mount();await current.vm.loadAll();await current.vm.newGlobal();const bound=current.vm.newNovelConversation('book')
  assert.equal(r.calls.filter(row=>row.name==='createConversation').length,2);gates.global.resolve(source('global'));await global
  assert.equal(current.vm.selectedId.value,'b');assert.notEqual(current.vm.creationRecords.value.find(row=>row.novelId==='book').pending,null)
  gates.book.resolve(source('bound',{novel_id:'book'}));await bound;assert.equal(current.vm.creationRecords.value.find(row=>row.novelId===null).saved.id,'global');r.stop()
})

test('创建malformed/wrongnovel回包保持unknown，目录名称不能解门禁，另建需明确核对', async () => {
  for(const response of [null,{},source('created',{novel_id:'wrong-book'}),source('created',{updated_at:[]})]){
    const {r,c}=await runtime({createConversation:async()=>response});await c.vm.newGlobal();await c.vm.newGlobal()
    const record=c.vm.creationRecords.value[0];assert.equal(record.phase,'unknown');assert.equal(record.saved,null);assert.equal(r.calls.filter(row=>row.name==='createConversation').length,1)
    await c.vm.checkConversationCreation(record);assert.equal(record.phase,'unknown');c.vm.freshConversationIntent(record);assert.equal(record.phase,'unknown')
    c.dialogs.at(-1).onPositiveClick();assert.equal(record.phase,'idle');assert.equal(r.calls.filter(row=>row.name==='createConversation').length,1);r.stop()
  }
})

test('真实Sidebar收到entry flags后pending/unknown禁按钮且不同作品可用，父handler不依赖disabled', async () => {
  const gate=deferred(),r=await projectRuntime({listNovels:async()=>[{id:'book',title:'作品'}],listConversations:async()=>[source()],createConversation:()=>gate.promise}),c=await r.mount();await c.vm.loadAll()
  const pending=c.vm.newGlobal();const props={novels:c.vm.novels.value,conversations:c.vm.conversations.value,selectedId:c.vm.selectedId.value,creationBusy:c.vm.conversationBusy.value,creationBlocked:c.vm.conversationBlocked.value}
  const sidebar=await r.mountSidebar(props),buttons=nodes(sidebar.render(),node=>node.type?.name==='NButton')
  const global=buttons.find(node=>JSON.stringify(node.children?.default?.()).includes('+ 新对话'));assert.equal(global.props.disabled,true);assert.equal(global.props.loading,true)
  const book=buttons.find(node=>node.props?.title==='新会话');assert.ok(book);assert.equal(Boolean(book.props.disabled),false)
  await c.vm.newGlobal();assert.equal(r.calls.filter(row=>row.name==='createConversation').length,1)
  gate.resolve(source('created'));await pending;r.stop()
})

test('新一轮未知请求不能借上一轮目录直接解除门禁，必须重新明确读取', async () => {
  const {r,c}=await runtime({updateConversation:async()=>{throw new Error('回应未知')},createConversation:async()=>{throw new Error('创建回应未知')}})
  c.vm.openRename(source());c.vm.renameTitle.value='首次标题';await c.vm.submitRename();await c.vm.checkRename()
  c.vm.continueRename();c.dialogs.at(-1).onPositiveClick();c.vm.renameTitle.value='第二次标题';await c.vm.submitRename()
  assert.equal(c.vm.renameState.value.observed,null);const dialogs=c.dialogs.length;c.vm.continueRename();assert.equal(c.dialogs.length,dialogs)
  assert.equal(c.vm.renameState.value.phase,'unknown')
  await c.vm.newGlobal();const state=c.vm.creationRecords.value[0];await c.vm.checkConversationCreation(state)
  c.vm.freshConversationIntent(state);c.dialogs.at(-1).onPositiveClick();await c.vm.newGlobal()
  assert.equal(state.directory,null);const count=c.dialogs.length;c.vm.freshConversationIntent(state);assert.equal(c.dialogs.length,count)
  assert.equal(state.phase,'unknown');assert.equal(r.calls.filter(row=>row.name==='createConversation').length,2);r.stop()
})

test('当前标题/原提交/复制回退标签经真实Naive NInput落在原生textbox而非外层generic', async () => {
  const previousWindow=globalThis.window;delete globalThis.window;const {NInput}=await import('naive-ui');globalThis.window=previousWindow
  const {r,c}=await runtime({}, {clipboardError:true});c.vm.openRename(source());c.vm.renameTitle.value='原title😀';await c.vm.submitRename();c.vm.renameTitle.value='后来标题😀';await c.vm.copyRename()
  for(const label of ['当前会话标题','原提交会话标题','手动复制会话标题']){
    const box=nodes(c.render(),node=>node.props?.['input-props']?.['aria-label']===label)[0];assert.ok(box)
    const props={...box.props,inputProps:box.props['input-props'],readonly:Object.hasOwn(box.props,'readonly'),'onUpdate:value':undefined};delete props['input-props']
    const html=await renderToString(createSSRApp({render:()=>h(NInput,props)}));assert.ok(html.includes(`aria-label="${label}"`));assert.match(html,/<(?:textarea|input)[^>]+aria-label=/)
    if(props.readonly)assert.match(html,/<(?:textarea|input)[^>]+readonly/)
  }r.stop()
})
