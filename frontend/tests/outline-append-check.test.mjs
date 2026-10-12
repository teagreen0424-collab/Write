import assert from 'node:assert/strict'
import test from 'node:test'
import { appendCard, deferred, settle, receipt } from './outline-append-fixture.mjs'

test('旧服务原生两字段回应不能确认；普通404核对不再次追加', async () => {
  const c = await appendCard({ api: {
    applyOutline: async () => ({ volume_id: 'volume', node_ids: ['volume','chapter'] }),
    checkOutlineAppend: async () => { throw Object.assign(new Error('Not Found'), {status:404}) },
  } })
  try {
    await c.vm.apply(); const original = JSON.parse(JSON.stringify(c.vm.appendSubmission.value))
    c.vm.plan.value.summary='后来稿😀'; await c.vm.apply(); await c.vm.apply()
    assert.equal(c.requests.filter(r=>r.method==='applyOutline').length,1)
    assert.equal(c.requests.filter(r=>r.method==='checkOutlineAppend').length,2)
    assert.deepEqual(c.vm.appendSubmission.value,original); assert.equal(c.vm.plan.value.summary,'后来稿😀')
    assert.equal(c.vm.appendUnconfirmed.value,true); assert.equal(c.emitted.length,0)
    assert.match(c.messages.at(-1).text,/更新并重启/)
  } finally { c.dispose() }
})

test('错身份、非布尔标记、少节点、重复节点与错卷的回应均保留未知原稿', async () => {
  for (const patch of [{request_id:'another'}, {append_replayed:1}, {node_ids:['volume']},
    {node_ids:['volume','volume']}, {volume_id:'other'}, {node_ids:['volume',null]}]) {
    const c=await appendCard({api:{applyOutline:async(id,plan,rid)=>({...receipt(rid),...patch})}})
    try { await c.vm.apply(); assert.equal(c.vm.appendUnconfirmed.value,true); assert.ok(c.vm.appendSubmission.value); assert.equal(c.emitted.length,0) }
    finally { c.dispose() }
  }
})

test('核对回应不完整也不能转为再次追加', async () => {
  const c=await appendCard({api:{applyOutline:async()=>{throw new Error('回应丢失')},checkOutlineAppend:async()=>({volume_id:'volume',node_ids:['volume','chapter']})}})
  try { await c.vm.apply(); await c.vm.apply(); assert.equal(c.requests.filter(r=>r.method==='applyOutline').length,1); assert.equal(c.vm.appendUnconfirmed.value,true); assert.equal(c.emitted.length,0) }
  finally { c.dispose() }
})

test('只读核对等待中零重复，A迟到事实不改B稿件', async () => {
  const pending=deferred()
  const c=await appendCard({api:{applyOutline:async()=>{throw new Error('未知')},checkOutlineAppend:async(id,rid)=>{await pending.promise;return {...receipt(rid),append_replayed:true}}}})
  try {
    await c.vm.apply(); const checking=c.vm.apply(); await settle(); await c.vm.apply()
    assert.equal(c.requests.filter(r=>r.method==='checkOutlineAppend').length,1)
    c.props.novelId='novel-b'; await settle(); c.vm.plan.value={title:'B卷',summary:'B原稿',chapters:[{title:'B章',summary:''}],acts:[]}
    const messageCount=c.messages.length; pending.resolve(); await checking
    assert.equal(c.vm.plan.value.summary,'B原稿'); assert.equal(c.messages.length,messageCount); assert.equal(c.emitted.length,0)
  } finally { pending.resolve(); c.dispose() }
})

test('核对405与500保留原身份，绝不把服务失败当查无回执', async () => {
  for (const status of [405,500]) {
    const c=await appendCard({api:{applyOutline:async()=>{throw new Error('未知')},checkOutlineAppend:async()=>{throw Object.assign(new Error('核对失败'),{status})}}})
    try { await c.vm.apply(); const id=c.vm.appendSubmission.value.requestId; await c.vm.apply(); assert.equal(c.requests.filter(r=>r.method==='applyOutline').length,1); assert.equal(c.vm.appendSubmission.value.requestId,id); assert.equal(c.vm.appendUnconfirmed.value,true) }
    finally { c.dispose() }
  }
})
