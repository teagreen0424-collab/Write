import assert from 'node:assert/strict'
import test from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { deferred, fields, novel, nodes, projectRuntime } from './project-draft-fixtures.mjs'

test('工作台创建等待新梗概正常关闭再开仍可恢复，原已存作品与新稿分开', async () => {
  const gate = deferred(), r = await projectRuntime({ createNovel: () => gate.promise }), c = await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value = fields(); const pending = c.vm.submitCreate()
  c.vm.createForm.value.summary = '等待期间唯一新梗概'; gate.resolve(novel()); await pending
  c.vm.closeCreate(); c.dialogs.at(-1).onPositiveClick(); c.vm.openCreate()
  assert.equal(c.vm.createForm.value.summary,'等待期间唯一新梗概'); assert.equal(c.vm.createState.value.savedNovel.summary,'原梗概')
  assert.ok(nodes(c.render(), node => node.props?.['aria-label']==='页内作品构思').length); r.stop()
})

test('空工作台首次保存只确认作品事实，明确打开前不自动选择新作品', async () => {
  let items = []
  const r = await projectRuntime({ listNovels: async () => items, createNovel: async payload => { items = [novel(payload)]; return items[0] } })
  const c = await r.mountWorkbench(); assert.equal(c.vm.novelId.value, null)
  c.vm.openCreate(); c.vm.createForm.value = fields(); await c.vm.submitCreate()
  assert.equal(c.vm.novelId.value, null); assert.equal(c.vm.createState.value.savedNovel.id, 'novel-saved')
  assert.equal(c.vm.showCreate.value, true); c.vm.openSavedProject(); assert.equal(c.vm.novelId.value, 'novel-saved')
  r.stop()
})

test('同helper module聊天→工作台→聊天跨卸载共享原pending，0第二POST且旧200不清后来稿', { timeout:5000 }, async () => {
  const gate = deferred(), r = await projectRuntime({ createNovel: () => gate.promise }), chat = await r.mount()
  chat.vm.openCreate(); chat.vm.createForm.value = fields(); const pending = chat.vm.submitCreate(); chat.stop()
  const workbench = await r.mountWorkbench(); workbench.vm.openCreate(); workbench.vm.createForm.value.summary='工作台后来新稿'; await workbench.vm.submitCreate()
  assert.equal(workbench.vm.creating.value,true); workbench.stop(); const current = await r.mount(); current.vm.openCreate(); await current.vm.submitCreate()
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1)
  gate.resolve(novel()); await pending
  assert.equal(current.vm.createForm.value.summary,'工作台后来新稿'); assert.equal(current.vm.createState.value.savedNovel.id,'novel-saved')
  assert.equal(chat.notices.length,0); assert.equal(workbench.notices.length,0); r.stop()
})

test('工作台原提交unknown后同名目录不构成回执，正常重开不丢新稿或自动POST', async () => {
  const r = await projectRuntime({ createNovel: async () => { throw new Error('传输回应丢失') }, listNovels:async()=>[novel()] }), c = await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value=fields(); await c.vm.submitCreate(); c.vm.createForm.value.summary='未知后来新稿'
  await c.vm.checkProjectDirectory(); await c.vm.submitCreate()
  assert.equal(c.vm.createState.value.phase,'unknown'); assert.equal(c.vm.createState.value.savedNovel,null)
  c.vm.closeCreate(); c.dialogs.at(-1).onPositiveClick(); c.vm.openCreate(); assert.equal(c.vm.createForm.value.summary,'未知后来新稿')
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1); r.stop()
})

test('工作台→聊天→工作台共享pending，旧200只更新原record事实且正常入口可复制', async () => {
  const gate=deferred(), r=await projectRuntime({createNovel:()=>gate.promise}), old=await r.mountWorkbench()
  old.vm.openCreate(); old.vm.createForm.value=fields(' 原提交\n😀 '); const pending=old.vm.submitCreate(); old.stop()
  const chat=await r.mount(); chat.vm.openCreate(); chat.vm.createForm.value.summary='聊天后来输入'; await chat.vm.submitCreate(); chat.stop()
  const current=await r.mountWorkbench(); current.vm.openCreate(); assert.equal(current.vm.createForm.value.summary,'聊天后来输入')
  gate.resolve(novel(fields(' 原提交\n😀 '))); await pending
  await current.vm.copyProject(true); assert.match(r.copied.at(-1),/ 原提交\n😀 /)
  await current.vm.copyProject(); assert.match(r.copied.at(-1),/聊天后来输入/)
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1); assert.equal(old.notices.length,0)
  assert.equal(current.vm.showCreate.value,true); r.stop()
})

test('工作台novel A→B→A不释放原创建锁，迟201不改选择/关闭/吐司', async () => {
  const gate=deferred(), r=await projectRuntime({createNovel:()=>gate.promise}), c=await r.mountWorkbench()
  c.vm.novelId.value='a'; c.vm.openCreate(); c.vm.createForm.value=fields(); const pending=c.vm.submitCreate()
  c.vm.changeNovel('b'); assert.equal(c.vm.novelId.value,'a'); c.vm.confirmNavigation(); c.vm.changeNovel('a'); c.vm.confirmNavigation()
  c.vm.createForm.value.summary='回A新梗概'; await c.vm.submitCreate(); assert.equal(c.vm.creating.value,true)
  gate.resolve(novel()); await pending
  assert.equal(c.vm.novelId.value,'a'); assert.equal(c.vm.showCreate.value,true); assert.equal(c.vm.createForm.value.summary,'回A新梗概')
  assert.equal(c.notices.length,0); assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1); r.stop()
})

test('工作台明确partialPUT等待期间改回原值仍dirty，不带未修改外部genre/style', async () => {
  const gate=deferred(), r=await projectRuntime({updateNovel:()=>gate.promise}), c=await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value=fields(); await c.vm.submitCreate(); c.vm.createForm.value.summary='本次新梗概'
  const saving=c.vm.saveProjectChanges(); await c.vm.saveProjectChanges(); c.vm.createForm.value.summary='原梗概'
  gate.resolve(novel({summary:'本次新梗概',genre:'服务器新题材',style_anchor:'新文风'})); await saving
  assert.deepEqual(r.calls.filter(row=>row.name==='updateNovel').map(row=>row.args),[['novel-saved',{summary:'本次新梗概'}]])
  assert.deepEqual(r.helper.projectChanges(c.vm.createState.value),{summary:'原梗概'}); assert.equal(c.vm.projectUnsaved.value,true)
  assert.equal(c.vm.createState.value.savedNovel.genre,'服务器新题材'); assert.equal(c.vm.createForm.value.genre,'故事'); r.stop()
})

test('工作台knownID PUT unknown后明确只读保新稿，未sent外部字段不变下一次意图', async () => {
  let saved=novel()
  const r=await projectRuntime({listNovels:async()=>[saved],updateNovel:async(_id,payload)=>{saved=novel({...payload,genre:'外部新题材',style_anchor:'外部文风'});throw new Error('真实200已提交但回应丢失')} }), c=await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value=fields(); await c.vm.submitCreate(); c.vm.createForm.value.summary='新梗概'; await c.vm.saveProjectChanges()
  c.vm.createForm.value.title='未知期间新标题'; await c.vm.saveProjectChanges(); await c.vm.checkProjectDirectory()
  assert.equal(c.vm.createState.value.phase,'saved'); assert.deepEqual(r.helper.projectChanges(c.vm.createState.value),{title:'未知期间新标题'})
  assert.equal(c.vm.createForm.value.genre,'故事'); assert.equal(c.vm.createState.value.savedNovel.genre,'外部新题材')
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1); assert.equal(r.calls.filter(row=>row.name==='updateNovel').length,1); r.stop()
})

test('工作台201后目录失败先保完整knownID，nochange零PUT/POST，打开作品须作者导航确认', async () => {
  let reads=0
  const r=await projectRuntime({listNovels:async()=>{if(++reads>1)throw new Error('目录失败');return[novel({id:'a'})]}}), c=await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value=fields(); await c.vm.submitCreate()
  assert.equal(c.vm.createState.value.phase,'saved'); assert.equal(c.vm.createState.value.savedNovel.id,'novel-saved'); assert.equal(c.vm.novelId.value,'a')
  assert.ok(c.notices.some(row=>/已保存.*刷新未完成/.test(row.text))); await c.vm.submitCreate(); await c.vm.saveProjectChanges()
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1); assert.equal(r.calls.filter(row=>row.name==='updateNovel').length,0)
  c.vm.settingsDirty.value=true; c.vm.openSavedProject(); assert.equal(c.vm.novelId.value,'a'); assert.match(c.vm.navigationWarning.value,/设定/)
  c.vm.cancelNavigation(); c.vm.openSavedProject(); c.vm.confirmNavigation(); assert.equal(c.vm.novelId.value,'novel-saved'); r.stop()
})

test('工作台坏201/错误PUT源不能误known，原稿/当前稿保留，未知后0盲写', async () => {
  for(const value of [null,{},novel({summary:'错误原文'}),novel({created_at:null})]) {
    const r=await projectRuntime({createNovel:async()=>value}),c=await r.mountWorkbench();c.vm.openCreate();c.vm.createForm.value=fields();await c.vm.submitCreate()
    assert.equal(c.vm.createState.value.phase,'unknown');assert.equal(c.vm.createState.value.savedNovel,null);await c.vm.submitCreate();await c.vm.saveProjectChanges()
    assert.equal(c.vm.createForm.value.summary,'原梗概');assert.equal(r.calls.filter(row=>row.name==='createNovel').length,1);r.stop()
  }
  const r=await projectRuntime({updateNovel:async()=>novel({id:'wrong-id',summary:'新梗概'})}),c=await r.mountWorkbench();c.vm.openCreate();c.vm.createForm.value=fields();await c.vm.submitCreate()
  c.vm.createForm.value.summary='新梗概';await c.vm.saveProjectChanges();assert.equal(c.vm.createState.value.phase,'unknown');assert.equal(c.vm.createState.value.savedNovel.id,'novel-saved');r.stop()
})

test('工作台关闭/fresh确认期间editback不能清稿，pending/unknown禁fresh，明确同稿另建合法', async () => {
  const r=await projectRuntime(),c=await r.mountWorkbench();c.vm.openCreate();c.vm.createForm.value=fields();c.vm.closeCreate();const close=c.dialogs.at(-1)
  c.vm.createForm.value.summary='改动';c.vm.createForm.value.summary='原梗概';assert.equal(close.onPositiveClick(),false);assert.equal(c.vm.showCreate.value,true)
  await c.vm.submitCreate();c.vm.startFreshCreate();const fresh=c.dialogs.at(-1);c.vm.createForm.value.summary='改动';c.vm.createForm.value.summary='原梗概'
  assert.equal(fresh.onPositiveClick(),false);c.vm.startFreshCreate();c.dialogs.at(-1).onPositiveClick();c.vm.createForm.value=fields();await c.vm.submitCreate()
  assert.equal(r.calls.filter(row=>row.name==='createNovel').length,2);c.vm.createState.value.phase='unknown';const count=c.dialogs.length;c.vm.startFreshCreate();assert.equal(c.dialogs.length,count);r.stop()
})

test('工作台空白稿beforeunload/返回/切页均有保护，取消不导航，确认只保页内稿不写库', async () => {
  const r=await projectRuntime(),c=await r.mountWorkbench();c.vm.openCreate();c.vm.createForm.value.summary=' \n　'
  assert.equal(c.vm.projectUnsaved.value,true);let prevented=false;c.listeners.get('beforeunload')({preventDefault:()=>{prevented=true},returnValue:undefined});assert.equal(prevented,true)
  c.vm.returnToChat();assert.equal(c.events.length,0);assert.match(c.vm.navigationWarning.value,/作品构思.*当前页面.*复制/);c.vm.cancelNavigation();assert.equal(c.events.length,0)
  c.vm.changeTab('outline');c.vm.cancelNavigation();assert.equal(c.vm.activeTab.value,'settings');c.vm.returnToChat();c.vm.confirmNavigation();assert.deepEqual(c.events,[['back']])
  assert.equal(c.vm.createForm.value.summary,' \n　');assert.equal(r.calls.some(row=>['createNovel','updateNovel'].includes(row.name)),false);c.stop();assert.equal(c.listeners.has('beforeunload'),false)
})

test('工作台真实模板用inputProps原生名称，copy故障readonly包含精确全文，目录失败仍unknown', async () => {
  const r=await projectRuntime({createNovel:async()=>{throw new Error('不确定回应')},listNovels:async()=>{throw new Error('目录也不可读')}},{clipboardError:true}),c=await r.mountWorkbench()
  c.vm.openCreate();c.vm.createForm.value=fields(' \n😀\u0000唯一新稿　');await c.vm.submitCreate();await c.vm.copyProject();await c.vm.checkProjectDirectory()
  assert.equal(c.vm.createState.value.phase,'unknown');assert.equal(c.vm.createForm.value.summary,' \n😀\u0000唯一新稿　')
  const labels=nodes(c.render(),node=>node.type?.name==='NInput'&&node.props?.['input-props'])
  assert.ok(['新建作品书名','新建作品题材','新建作品梗概','原提交构思','当前作品构思'].every(label=>labels.some(node=>node.props['input-props']['aria-label']===label)))
  const fallback=labels.find(node=>node.props['input-props']['aria-label']==='当前作品构思');assert.equal(Object.hasOwn(fallback.props,'readonly'),true);assert.match(fallback.props.value,/ \n😀\u0000唯一新稿　/);r.stop()
})

test('工作台作品输入及复制回退经真实Naive NInput渲染为有名称的原生可访问控件', async () => {
  const previousWindow = globalThis.window
  delete globalThis.window
  const { NInput } = await import('naive-ui')
  globalThis.window = previousWindow
  const r = await projectRuntime({}, { clipboardError: true }), c = await r.mountWorkbench()
  c.vm.openCreate(); c.vm.createForm.value = fields('精确构思😀'); await c.vm.submitCreate(); await c.vm.copyProject()
  const boxes = nodes(c.render(), node => node.type?.name === 'NInput' && node.props?.['input-props'])
  for (const label of ['新建作品书名','新建作品题材','新建作品梗概','原提交构思','当前作品构思']) {
    const box = boxes.find(node => node.props['input-props']['aria-label'] === label)
    assert.ok(box, `实际工作台模板包含 ${label}`)
    const props = { ...box.props, inputProps: box.props['input-props'], readonly: Object.hasOwn(box.props, 'readonly'), 'onUpdate:value': undefined }
    delete props['input-props']
    const html = await renderToString(createSSRApp({ render: () => h(NInput, props) }))
    assert.match(html, props.type === 'textarea' ? /<textarea[^>]+aria-label=/ : /<input[^>]+aria-label=/)
    assert.ok(html.includes(`aria-label="${label}"`))
    if (props.readonly) assert.match(html, /<textarea[^>]+readonly/)
    if (label === '当前作品构思') assert.ok(html.includes('精确构思😀'))
  }
  r.stop()
})
