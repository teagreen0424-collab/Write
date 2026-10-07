import assert from 'node:assert/strict'
import test from 'node:test'
import { cardRuntime, chapter, deferred, nodes, settle } from './chapter-generation-fixtures.mjs'

test('聊天写章冲突保留精确候选、新输入和复制入口，等待期同handler只有一次请求', async()=>{
  const held=deferred();let handlers
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{handlers=h;await held.promise;h.onError('最新章已变，本次候选未保存','chapter_write_conflict',409)}}})
  const context=await runtime.mount();const pending=context.vm.write();await settle()
  const duplicate=context.vm.write();await settle()
  const requestCount=runtime.calls.filter(row=>row.name==='writeMessageChapter').length
  handlers.onToken(' 😀候选\u0000\r\n ');context.vm.directive.value='等待期间作者的新构思'
  held.resolve();await Promise.all([pending,duplicate])
  assert.equal(requestCount,1)
  assert.equal(context.vm.previewText.value,' 😀候选\u0000\r\n ')
  assert.equal(context.vm.directive.value,'等待期间作者的新构思')
  assert.equal(context.vm.candidateStatus.value,'failed')
  assert.equal(nodes(context.render(),node=>node.type==='textarea'&&node.props?.['aria-label']==='未完成章节候选').length,1)
  await context.vm.copyCandidate();assert.equal(runtime.copied[0],' 😀候选\u0000\r\n ')
  assert.equal(context.events.length,0);context.stop()
})

test('A到B到A与卸载后旧回包静默，不向当前来源发已保存事件',async()=>{
  for(const unload of [false,true]) {
    const held=deferred();let handlers
    const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{handlers=h;await held.promise;h.onDone({chapter:chapter('novel-a',{directive:payload.directive})})}}})
    const context=await runtime.mount();const pending=context.vm.write();await settle()
    if(unload)context.stop();else{context.props.novelId='novel-b';context.props.conversationId='conversation-b';await settle();context.props.novelId='novel-a';context.props.conversationId='conversation-a';await settle()}
    held.resolve();await pending
    assert.equal(context.events.length,0);assert.equal(context.notices.filter(row=>row.type==='success').length,0)
    if(!unload)context.stop()
  }
})

test('生成成功等待中改稿或改回不关闭卡片，真实回执与未保存构思分别保留',async()=>{
  for(const changedBack of [false,true]) {
    const held=deferred();const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{await held.promise;h.onDone({chapter:chapter('novel-a',{directive:payload.directive})})}}})
    const context=await runtime.mount();const pending=context.vm.write();await settle()
    context.vm.directive.value='新构思';if(changedBack)context.vm.directive.value='作者构思'
    held.resolve();await pending
    assert.equal(context.vm.candidateStatus.value,'saved');assert.equal(context.vm.savedChapter.value.id,'novel-a-chapter')
    assert.equal(context.vm.unsavedEdits.value,true);assert.equal(context.events.length,0)
    await context.vm.copyCurrentIdea();assert.match(runtime.copied.at(-1),new RegExp(changedBack?'作者构思':'新构思'))
    context.stop()
  }
})

test('同一编译模块卸载重挂保留在途写锁、候选和作者新构思',async()=>{
  const held=deferred();let handlers
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{handlers=h;await held.promise;h.onDone({chapter:chapter('novel-a',{directive:payload.directive})})}}})
  const first=await runtime.mount();const saving=first.vm.write();await settle();handlers.onToken('原候选')
  first.vm.title.value='等待期间的新标题';first.stop()
  const second=await runtime.mount();await second.vm.write()
  assert.equal(runtime.calls.filter(row=>row.name==='writeMessageChapter').length,1)
  assert.equal(second.vm.previewText.value,'原候选');assert.equal(second.vm.title.value,'等待期间的新标题');assert.equal(second.vm.generating.value,true)
  held.resolve();await saving
  assert.equal(second.vm.generating.value,false);assert.equal(second.vm.candidateStatus.value,'saved')
  assert.equal(second.vm.title.value,'等待期间的新标题');assert.equal(second.vm.unsavedEdits.value,true)
  assert.equal(first.events.length,0);assert.equal(second.events.length,0);assert.equal(second.notices.length,0)
  second.stop()
})

test('冲突后只有明确读取最新来源和确认放弃旧候选才发新请求，读取保留作者选择',async()=>{
  let reads=0,writes=0
  const runtime=await cardRuntime({api:{listChapterMetadata:async()=>[chapter('novel-a',{id:++reads===1?'previous-1':'previous-2',seq:reads})],
    listOutline:async()=>[{id:'outline-a',level:'chapter',title:'大纲甲'},{id:'outline-b',level:'chapter',title:'大纲乙'}],
    writeMessageChapter:async(cid,mid,payload,h)=>{writes++;h.onToken(`候选${writes}`);h.onError('来源已变','chapter_write_conflict',409)},
  }})
  const context=await runtime.mount();await context.vm.write()
  context.vm.title.value='作者的新标题';context.vm.directive.value='作者的新构思';context.vm.outlineNodeId.value='outline-b'
  await context.vm.write();assert.equal(writes,1)
  await context.vm.loadSource()
  assert.equal(writes,1);assert.equal(context.vm.previewText.value,'候选1')
  assert.equal(context.vm.title.value,'作者的新标题');assert.equal(context.vm.directive.value,'作者的新构思');assert.equal(context.vm.outlineNodeId.value,'outline-b')
  const canceled=context.vm.write();await settle();context.dialogs.at(-1).onNegativeClick();await canceled
  assert.equal(writes,1);assert.equal(context.vm.previewText.value,'候选1')
  const confirmed=context.vm.write();await settle();context.dialogs.at(-1).onPositiveClick();await confirmed
  assert.equal(writes,2)
  const call=runtime.calls.filter(row=>row.name==='writeMessageChapter').at(-1)
  assert.deepEqual(call.args.slice(0,3),['conversation-a','message-a',{title:'作者的新标题',directive:'作者的新构思',outline_node_id:'outline-b',previous_chapter_id:'previous-2'}])
  assert.equal(context.vm.previewText.value,'候选2');context.stop()
})

test('重新生成确认期间作者再编辑或改回不发请求、不清候选',async()=>{
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{h.onToken('保留旧候选');h.onError('来源变化','chapter_write_conflict',409)}}})
  const context=await runtime.mount();await context.vm.write();await context.vm.loadSource()
  const starting=context.vm.write();await settle()
  context.vm.directive.value='临时新构思';context.vm.directive.value='作者构思'
  context.dialogs.at(-1).onPositiveClick();await starting
  assert.equal(runtime.calls.filter(row=>row.name==='writeMessageChapter').length,1)
  assert.equal(context.vm.previewText.value,'保留旧候选');assert.equal(context.vm.generating.value,false)
  context.stop()
})

test('初次目录迟到不覆盖作者的大纲选择，切作品旧读取不清新loading',async()=>{
  const a=deferred(),b=deferred()
  const runtime=await cardRuntime({api:{listChapterMetadata:async id=>id==='novel-a'?a.promise:b.promise,
    listOutline:async()=>[{id:'suggested',title:'默认',level:'chapter'},{id:'chosen',title:'作者选择',level:'chapter'}],
  }})
  const context=await runtime.mount();context.vm.outlineNodeId.value='chosen'
  a.resolve([]);await settle();assert.equal(context.vm.outlineNodeId.value,'chosen')
  context.props.novelId='novel-b';context.props.conversationId='conversation-b';await settle()
  assert.equal(context.vm.sourceLoading.value,true)
  b.resolve([]);await settle();assert.equal(context.vm.sourceLoading.value,false)
  context.stop()
})

test('未知断流与缺失或错作品done不冒称未保存或已保存，保留候选与构思',async()=>{
  for(const kind of ['incomplete','missing','foreign','bad-score']) {
    const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{
      h.onToken('候选未丢')
      if(kind==='incomplete')h.onError('连接提前结束','STREAM_INCOMPLETE')
      else h.onDone(kind==='missing'?{}:{chapter:chapter(kind==='foreign'?'novel-b':'novel-a',{directive:payload.directive,...(kind==='bad-score'?{drift_score:'0.3'}:{})})})
    }}})
    const context=await runtime.mount();await context.vm.write()
    assert.equal(context.vm.candidateStatus.value,'unknown');assert.equal(context.events.length,0)
    assert.equal(context.vm.previewText.value,'候选未丢');assert.equal(context.vm.directive.value,'作者构思')
    assert.equal(context.notices.some(row=>row.type==='success'),false);context.stop()
  }
})

test('复制失败仍可从真实readonly文本框选择候选与构思，关闭重挂保留稿件',async()=>{
  const runtime=await cardRuntime({clipboardError:true,api:{writeMessageChapter:async(cid,mid,payload,h)=>{h.onToken('候选保留');h.onError('来源变化','chapter_write_conflict',409)}}})
  const context=await runtime.mount();context.vm.title.value='作者标题';await context.vm.write()
  await context.vm.copyCandidate();await context.vm.copyCurrentIdea()
  const textareas=nodes(context.render(),node=>node.type==='textarea'&&Object.hasOwn(node.props??{},'readonly'))
  assert.equal(textareas.find(node=>node.props['aria-label']==='未完成章节候选').props.value,'候选保留')
  assert.match(textareas.find(node=>node.props['aria-label']==='当前构思').props.value,/作者标题[\s\S]*作者构思/)
  context.vm.cancelCard();assert.equal(context.events[0][0],'cancel');context.stop()
  const remounted=await runtime.mount();assert.equal(remounted.vm.previewText.value,'候选保留');assert.equal(remounted.vm.title.value,'作者标题')
  remounted.stop()
})

test('合法完成无新编辑按原合同发应用事件，旧已保存动作回执不冒称新构思已写入',async()=>{
  for(const replayed of [false,true]) {
    const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>h.onDone({chapter:chapter('novel-a',{directive:replayed?'之前保存的构思':payload.directive})})}})
    const context=await runtime.mount();await context.vm.write()
    assert.equal(context.vm.candidateStatus.value,'saved')
    assert.equal(context.events.length,replayed?0:1)
    assert.equal(context.vm.unsavedEdits.value,replayed)
    if(replayed) {
      context.vm.finishSaved();assert.equal(context.dialogs.length,1)
      context.vm.title.value='确认期间又改标题';context.dialogs[0].onPositiveClick();assert.equal(context.events.length,0)
      context.vm.finishSaved();context.dialogs[1].onPositiveClick();assert.equal(context.events[0][0],'applied')
    }
    context.stop()
  }
})

test('不同作品请求独立，旧请求结束不释放新作品在途锁或污染提示',async()=>{
  const a=deferred(),b=deferred()
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{await(cid==='conversation-a'?a.promise:b.promise);h.onError(`${cid}冲突`,'chapter_write_conflict',409)}}})
  const context=await runtime.mount();const first=context.vm.write();await settle()
  context.props.novelId='novel-b';context.props.conversationId='conversation-b';await settle()
  const second=context.vm.write();await settle()
  a.resolve();await first;assert.equal(context.vm.generating.value,true);assert.equal(context.notices.length,0)
  b.resolve();await second;assert.equal(context.vm.generating.value,false);assert.equal(context.notices.length,1)
  context.stop()
})

test('已保存动作批量返回后真实父模板仍可重开未保存构思，仅读取不新建其他回复稿件',async()=>{
  const held=deferred();const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{h.onToken('真实候选');await held.promise;h.onDone({chapter:chapter('novel-a',{content:'真实候选',directive:payload.directive})})}}})
  const parent=await runtime.mountBubble();parent.vm.showWriting.value=true
  const vnode=nodes(parent.render(),node=>node.type?.name==='ChapterWriteCard')[0]
  const card=await runtime.mount({novelId:vnode.props['novel-id'],conversationId:vnode.props['conversation-id'],messageId:vnode.props['message-id'],sourceText:vnode.props['source-text']},(event,...args)=>{if(event==='applied')vnode.props.onApplied(...args)})
  const writing=card.vm.write();await settle();card.vm.directive.value='唯一的新构思';card.stop();parent.stop();held.resolve();await writing
  const newParent=await runtime.mountBubble({actions:[{action_type:'chapter',status:'done',result_id:'novel-a-chapter',result_summary:{chapter_id:'novel-a-chapter',title:'原章'},target_deleted:false}]})
  assert.equal(newParent.vm.chapterResult.value.chapter_id,'novel-a-chapter')
  const reopen=nodes(newParent.render(),node=>node.props?.['aria-label']==='写章构思与候选')[0]
  assert.ok(reopen,'已确认动作旁必须能找回本页未保存构思')
  reopen.props.onClick();await settle()
  const reopenedVNode=nodes(newParent.render(),node=>node.type?.name==='ChapterWriteCard')[0]
  assert.ok(reopenedVNode)
  const reopened=await runtime.mount({novelId:reopenedVNode.props['novel-id'],conversationId:reopenedVNode.props['conversation-id'],messageId:reopenedVNode.props['message-id'],sourceText:reopenedVNode.props['source-text']})
  assert.equal(reopened.vm.directive.value,'唯一的新构思');await reopened.vm.copyCurrentIdea();assert.match(runtime.copied.at(-1),/唯一的新构思/)
  await reopened.vm.write();assert.equal(runtime.calls.filter(row=>row.name==='writeMessageChapter').length,1)
  const unrelated=await runtime.mountBubble({message:{id:'other-message',conversation_id:'conversation-a',role:'assistant',content:'另一回复',meta:null,created_at:''}})
  assert.equal(nodes(unrelated.render(),node=>node.props?.['aria-label']==='写章构思与候选').length,0)
  reopened.stop();newParent.stop();unrelated.stop()
})

test('done重放另一真实正文不把本次token冒称已保存，候选仍可复制并明确确认关闭',async()=>{
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{h.onToken('本次未落库候选');h.onDone({chapter:chapter('novel-a',{content:'已保存的不同正文',directive:payload.directive})})}}})
  const context=await runtime.mount();await context.vm.write()
  assert.equal(context.vm.savedChapter.value.content,'已保存的不同正文')
  assert.equal(context.vm.previewText.value,'本次未落库候选');assert.equal(context.vm.candidateDiffers.value,true)
  assert.equal(context.events.length,0);await context.vm.copyCandidate();assert.equal(runtime.copied[0],'本次未落库候选')
  assert.ok(nodes(context.render(),node=>typeof node.children==='string'&&node.children.includes('以下候选未保存')).length)
  context.vm.finishSaved();assert.equal(context.dialogs.length,1);context.dialogs[0].onNegativeClick?.()
  assert.equal(context.events.length,0);context.stop()
})

test('动作读取失败时已有卡片可复制和编辑，但直接handler与模板均禁止未核对的新生成',async()=>{
  const runtime=await cardRuntime({api:{writeMessageChapter:async(cid,mid,payload,h)=>{h.onToken('候选');h.onError('来源变化','chapter_write_conflict',409)}}})
  const parent=await runtime.mountBubble();const card=await runtime.mount();await card.vm.write();await card.vm.loadSource()
  parent.vm.showWriting.value=true;parent.props.actionsStatus='failed';await settle()
  const vnode=nodes(parent.render(),node=>node.type?.name==='ChapterWriteCard')[0]
  assert.equal(vnode.props['actions-ready'],false)
  card.props.actionsReady=vnode.props['actions-ready'];card.vm.directive.value='仍可编辑的作者构思'
  await card.vm.copyCurrentIdea();assert.match(runtime.copied.at(-1),/仍可编辑的作者构思/)
  await card.vm.write();assert.equal(runtime.calls.filter(row=>row.name==='writeMessageChapter').length,1)
  assert.ok(nodes(card.render(),node=>node.props?.onClick===card.vm.write&&node.props.disabled).length)
  card.stop();parent.stop()
})
