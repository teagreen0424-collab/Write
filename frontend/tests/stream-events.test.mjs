// 直接运行当前 api.ts 的流式函数，所有 fetch 都在测试内模拟。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { streamEvents } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const encode = new TextEncoder()
const frame = (event) => `data: ${JSON.stringify(event)}\n\n`
const text = { type: 'text', node_id: 'chapter-a', content: '未完成稿。' }
const completed = { type: 'done', status: 'completed', done: 2, total: 2 }

function response(wire, chunkSize = Infinity) {
  const bytes = encode.encode(wire)
  return new Response(new ReadableStream({
    start(controller) {
      for (let index = 0; index < bytes.length; index += chunkSize) {
        controller.enqueue(bytes.slice(index, index + chunkSize))
      }
      controller.close()
    },
  }))
}

async function run(mockFetch, options = {}) {
  const prior = globalThis.fetch
  globalThis.fetch = mockFetch
  const events = []
  const errors = []
  try {
    await streamEvents('/local-test-stream', options.onEvent ?? ((event) => events.push(event)), {
      onError: options.onError ?? ((message, code) => errors.push({ message, code })),
      signal: options.signal,
    })
    return { events, errors }
  } finally {
    globalThis.fetch = prior
  }
}

test('章节失败和评分失败不会终止后续章，只有 done 才确认结束', async () => {
  const chapterError = { type: 'error', scope: 'chapter', fatal: false, saved: false, node_id: 'chapter-a' }
  const qualityError = { type: 'error', scope: 'quality', fatal: false, saved: true, node_id: 'chapter-b' }
  const wire = [text, chapterError, { type: 'chapter_done', node_id: 'chapter-b' }, qualityError,
    { ...completed, status: 'incomplete' }, { ...text, content: '终态后的无效文本' }].map(frame).join('')
  const result = await run(async () => response(wire))
  assert.deepEqual(result.events.map((event) => event.type), ['text', 'error', 'chapter_done', 'error', 'done'])
  assert.deepEqual(result.errors, [])
})

test('paused 保持连接并继续处理恢复后的章节', async () => {
  const result = await run(async () => response([frame({ type: 'paused' }), frame(text), frame(completed)].join('')))
  assert.deepEqual(result.events.map((event) => event.type), ['paused', 'text', 'done'])
  assert.deepEqual(result.errors, [])
})

test('fatal task error 是明确终态且不继续读取同块文本', async () => {
  const result = await run(async () => response(frame({ type: 'error', scope: 'task', fatal: true }) + frame(text)))
  assert.equal(result.events.length, 1)
  assert.deepEqual(result.errors, [])
})

test('逐字节 UTF-8 和 CRLF 分块不损坏中文', async () => {
  const wire = (frame(text) + frame(completed)).replaceAll('\n', '\r\n')
  const result = await run(async () => response(wire, 1))
  assert.equal(result.events[0].content, text.content)
  assert.equal(result.events.at(-1).type, 'done')
  assert.deepEqual(result.errors, [])
})

test('多行 data 与注释按标准事件解析', async () => {
  const result = await run(async () => response(': keepalive\n\ndata: {"type":\ndata: "done"}\n\n'))
  assert.deepEqual(result.events, [{ type: 'done' }])
  assert.deepEqual(result.errors, [])
})

test('没有末尾空行的 done 仍会处理', async () => {
  const result = await run(async () => response(frame(text) + frame(completed).trimEnd()))
  assert.equal(result.events.at(-1).type, 'done')
  assert.deepEqual(result.errors, [])
})

test('正文后裸 EOF 不冒充成功，并保留已经分发的文本', async () => {
  const result = await run(async () => response(frame(text)))
  assert.deepEqual(result.events, [text])
  assert.equal(result.errors.length, 1)
  assert.equal(result.errors[0].code, 'STREAM_INCOMPLETE')
  assert.match(result.errors[0].message, /完成确认/)
})

test('空连接和纯 keepalive 都缺少完成确认', async () => {
  for (const wire of ['', ': ping\n\n']) {
    const result = await run(async () => response(wire))
    assert.equal(result.errors.length, 1)
    assert.equal(result.errors[0].code, 'STREAM_INCOMPLETE')
  }
})

test('坏 JSON 不能忽略后继续显示成功', async () => {
  const result = await run(async () => response(frame(text) + 'data: {bad}\n\n' + frame(completed)))
  assert.deepEqual(result.events, [text])
  assert.equal(result.errors.length, 1)
  assert.match(result.errors[0].message, /格式无效/)
})

test('非法对象和正文事件触发协议错误', async () => {
  for (const event of [[], null, 'bad', {}, { type: 'text', content: 123, node_id: 'chapter-a' },
    { type: 'text', content: '正文', node_id: null }]) {
    const result = await run(async () => response(frame(event) + frame(completed)))
    assert.deepEqual(result.events, [])
    assert.equal(result.errors.length, 1)
  }
})

test('HTTP 409 提供请求错误而不是完成事件', async () => {
  const result = await run(async () => new Response(JSON.stringify({ detail: '仍在运行' }), { status: 409 }))
  assert.deepEqual(result.events, [])
  assert.deepEqual(result.errors, [{ message: '仍在运行', code: 'STREAM_REQUEST_FAILED' }])
})

test('无 response.body 的响应明确报错', async () => {
  const result = await run(async () => new Response(null, { status: 204 }))
  assert.equal(result.errors[0].code, 'STREAM_REQUEST_FAILED')
})

test('fetch 网络失败有一次明确收尾', async () => {
  const result = await run(async () => { throw new TypeError('网络中断') })
  assert.deepEqual(result.errors, [{ message: '网络中断', code: 'STREAM_INCOMPLETE' }])
})

test('已收到正文后读取网络失败，文本仍已分发且没有完成事件', async () => {
  let sent = false
  const body = new ReadableStream({
    pull(controller) {
      if (!sent) {
        sent = true
        controller.enqueue(encode.encode(frame(text)))
      } else controller.error(new TypeError('读取连接中断'))
    },
  }, { highWaterMark: 0 })
  const result = await run(async () => new Response(body))
  assert.deepEqual(result.events, [text])
  assert.deepEqual(result.errors, [{ message: '读取连接中断', code: 'STREAM_INCOMPLETE' }])
})

test('异步完成回调被等待，刷新异常区分已收到完成确认', async () => {
  let waited = false
  const result = await run(async () => response(frame(completed)), {
    onEvent: async () => {
      await Promise.resolve()
      waited = true
      throw new Error('页面同步失败')
    },
  })
  assert.equal(waited, true)
  assert.deepEqual(result.errors, [{ message: '页面同步失败', code: 'CLIENT_UPDATE_FAILED' }])
})

test('普通事件的处理异常不再被吞掉', async () => {
  const result = await run(async () => response(frame(text) + frame(completed)), {
    onEvent: () => { throw new Error('处理失败') },
  })
  assert.deepEqual(result.errors, [{ message: '处理失败', code: 'STREAM_INCOMPLETE' }])
})

test('onError 自身拒绝仅传播一次', async () => {
  let calls = 0
  await assert.rejects(run(async () => response(''), {
    onError: async () => { calls += 1; throw new Error('回调失败') },
  }), /回调失败/)
  assert.equal(calls, 1)
})

test('显式取消传递 signal，并忽略离开页面后的错误回调', async () => {
  const controller = new AbortController()
  const result = await run(async (_url, options) => {
    assert.equal(options.signal, controller.signal)
    controller.abort()
    throw new DOMException('Aborted', 'AbortError')
  }, { signal: controller.signal })
  assert.deepEqual(result.events, [])
  assert.deepEqual(result.errors, [])
})
