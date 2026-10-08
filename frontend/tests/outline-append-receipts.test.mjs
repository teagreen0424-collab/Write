import assert from 'node:assert/strict'
import test from 'node:test'
import { appendCard, deferred, settle, receipt } from './outline-append-fixture.mjs'

test('大纲未知追加重试同一UUID和原树，核对不吞掉后来构思与节点编辑', async () => {
  const saved = new Map(); let fail = true
  const c = await appendCard({ api: { applyOutline: async (id, plan, requestId) => {
    if (!saved.has(requestId)) saved.set(requestId, structuredClone(plan))
    if (fail) { fail=false; throw new Error('真实保存后回应丢失夹具') }
    return { ...receipt(requestId), append_replayed: true }
  } } })
  try {
    await c.vm.apply(); assert.equal(c.vm.appendUnconfirmed.value, true)
    c.vm.sourceText.value='后来构思'; c.vm.plan.value.title='后来卷标题'; c.vm.plan.value.chapters[0].summary='后来章节概要'
    await c.vm.apply()
    const writes=c.requests.filter(r => r.method==='applyOutline')
    assert.equal(writes.length,2); assert.deepEqual(writes[1].args,writes[0].args)
    assert.match(writes[0].args[2], /^[0-9a-f-]{36}$/)
    assert.equal(saved.size,1); assert.equal(c.vm.plan.value.title,'后来卷标题')
    assert.equal(c.vm.plan.value.chapters[0].summary,'后来章节概要'); assert.equal(c.vm.sourceText.value,'后来构思')
    assert.equal(c.emitted.length,0); assert.equal(c.vm.appendUnconfirmed.value,false)
    assert.equal(c.vm.appendSubmission.value,null); assert.match(c.messages.at(-1).text,/新增编辑已保留/)
  } finally { c.dispose() }
})
test('大纲未知结果期间重新预览不启动模型，也不替换原提交', async () => {
  const c=await appendCard({ api:{ applyOutline:async()=>{throw Object.assign(new Error('500未知'),{status:500})} } })
  try {
    await c.vm.apply(); const original=JSON.parse(JSON.stringify(c.vm.appendSubmission.value))
    c.vm.sourceText.value='新构思'; await c.vm.preview()
    assert.equal(c.requests.filter(r=>r.method==='previewOutline').length,0)
    assert.deepEqual(c.vm.appendSubmission.value,original); assert.equal(c.messages[0].type,'warning')
  } finally { c.dispose() }
})
test('大纲等待期间重复确认零额外写，原提交冻结，后来稿保持', async () => {
  const pending=deferred(),c=await appendCard({api:{applyOutline:async(id,plan,requestId)=>{await pending.promise;return receipt(requestId)}}})
  try {
    const applying=c.vm.apply(); await settle(); c.vm.plan.value.summary='等待期间后来概要'
    await c.vm.apply(); assert.equal(c.requests.filter(r=>r.method==='applyOutline').length,1)
    assert.equal(c.requests[0].args[1].summary,'\n　原概要🐾\n')
    pending.resolve(); await applying
    assert.equal(c.vm.plan.value.summary,'等待期间后来概要'); assert.equal(c.emitted.length,0)
  } finally {pending.resolve();c.dispose()}
})
test('大纲明确未保存的400允许修正草稿，以新请求提交', async () => {
  let fail=true
  const c=await appendCard({api:{applyOutline:async(id,plan,requestId)=>{if(fail){fail=false;throw Object.assign(new Error('校验拒绝'),{status:400})}return receipt(requestId)}}})
  try {
    await c.vm.apply(); assert.equal(c.vm.appendUnconfirmed.value,false); assert.equal(c.vm.appendSubmission.value,null)
    c.vm.plan.value.title='修正的卷'; await c.vm.apply()
    assert.notEqual(c.requests[0].args[2],c.requests[1].args[2]); assert.equal(c.requests[1].args[1].title,'修正的卷')
    assert.equal(c.emitted[0][0],'applied')
  } finally {c.dispose()}
})
test('大纲busy409仍未知，核对必须保持同一原请求', async () => {
  let fail=true
  const c=await appendCard({api:{applyOutline:async(id,plan,requestId)=>{if(fail){fail=false;throw Object.assign(new Error('写锁忙'),{status:409,code:'outline_append_busy'})}return receipt(requestId)}}})
  try {
    await c.vm.apply(); assert.equal(c.vm.appendUnconfirmed.value,true)
    await c.vm.apply(); assert.equal(c.requests[0].args[2],c.requests[1].args[2])
    assert.equal(c.emitted.length,1)
  } finally {c.dispose()}
})
test('大纲已删除原节点核对409保留原请求与后来稿，不用新UUID复活', async () => {
  let calls=0
  const c=await appendCard({api:{applyOutline:async()=>{if(++calls===1)throw new Error('丢回应');throw Object.assign(new Error('原节点已删除'),{status:409,code:'outline_append_deleted'})}}})
  try {
    await c.vm.apply(); c.vm.plan.value.summary='保留稿件'; await c.vm.apply(); await c.vm.apply()
    assert.equal(new Set(c.requests.map(r=>r.args[2])).size,1)
    assert.equal(c.vm.appendUnconfirmed.value,true); assert.equal(c.vm.plan.value.summary,'保留稿件'); assert.equal(c.emitted.length,0)
  } finally {c.dispose()}
})
test('大纲A迟到回应不清B原稿与请求，新作品按自身UUID提交', async () => {
  const pending=deferred();let calls=0
  const c=await appendCard({api:{applyOutline:async(id,plan,requestId)=>{if(++calls===1)await pending.promise;return receipt(requestId)}}})
  try {
    const applying=c.vm.apply(); await settle();c.props.novelId='novel-b';await settle()
    c.vm.plan.value={title:'B卷',summary:'B原稿',chapters:[{title:'B章',summary:''}],acts:[]}
    pending.resolve();await applying
    assert.equal(c.vm.plan.value.summary,'B原稿');assert.equal(c.messages.length,0);assert.equal(c.emitted.length,0)
    await c.vm.apply();assert.notEqual(c.requests[0].args[2],c.requests[1].args[2]);assert.equal(c.requests[1].args[0],'novel-b')
  } finally {pending.resolve();c.dispose()}
})
test('聊天大纲沿用原消息动作合同，不发送工作台UUID或改变聊天错误处理', async () => {
  const c=await appendCard({props:{conversationId:'cid',messageId:'mid'},api:{applyMessageOutline:async()=>{throw new Error('聊天受控失败')}}})
  try {
    await c.vm.apply();assert.equal(c.requests[0].method,'applyMessageOutline');assert.equal(c.requests[0].args.length,3)
    assert.equal(c.vm.appendSubmission.value,null);assert.equal(c.vm.appendUnconfirmed.value,false);assert.equal(c.messages[0].type,'error')
  } finally {c.dispose()}
})
