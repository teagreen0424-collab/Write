import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { createServer, loadConfigFromFile } from 'vite'

const root = fileURLToPath(new URL('../', import.meta.url))
const configPath = fileURLToPath(new URL('../vite.config.ts', import.meta.url))
const fragment = 'data: {"type":"progress","phase":"generating","node_id":"test-node"}\n\n'
  + 'data: {"type":"text","node_id":"test-node","content":"已收到的半稿。"}\n\n'
const done = 'data: {"type":"done","status":"completed"}\n\n'

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  assert.ok(address && typeof address !== 'string')
  assert.notEqual(address.port, 5173)
  return 'http://127.0.0.1:' + address.port
}

async function close(server) {
  server.closeAllConnections()
  await new Promise((resolve) => server.close(resolve))
}

async function setupProxy(t, handler) {
  const upstream = createHttpServer(handler)
  t.after(() => close(upstream))
  const target = await listen(upstream)
  const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, configPath, root)
  assert.ok(loaded)
  const apiProxy = loaded.config.server?.proxy?.['/api']
  assert.ok(apiProxy && typeof apiProxy === 'object')
  // 加载真正的配置与 configure 回调，只替换测试上游；不会启动现有开发端口。
  const vite = await createServer({
    ...loaded.config,
    root,
    configFile: false,
    logLevel: 'silent',
    server: {
      ...loaded.config.server,
      middlewareMode: true,
      hmr: false,
      ws: false,
      watch: null,
      proxy: { '/api': { ...apiProxy, target } },
    },
  })
  t.after(() => vite.close())
  const proxy = createHttpServer((request, response) => vite.middlewares(request, response))
  t.after(() => close(proxy))
  return { url: (await listen(proxy)) + '/api/test-stream', vite }
}

async function readFragment(reader) {
  const decoder = new TextDecoder()
  let text = ''
  while (!text.includes('已收到的半稿。')) {
    const chunk = await reader.read()
    assert.equal(chunk.done, false, '片段到达前不应结束连接')
    text += decoder.decode(chunk.value, { stream: true })
  }
  return text
}

test('真实 Vite 代理让完整 SSE 正常结束并保留原始事件', async (t) => {
  const { url } = await setupProxy(t, (_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/event-stream' })
    response.end(fragment + done)
  })
  const response = await fetch(url, { signal: AbortSignal.timeout(4000) })
  assert.equal(response.status, 200)
  assert.equal(await response.text(), fragment + done)
})

test('上游已发片段后断开，实际客户端报告失败且不会悬挂', async (t) => {
  let upstreamResponse
  const { url, vite } = await setupProxy(t, (_request, response) => {
    upstreamResponse = response
    response.on('error', () => {})
    response.writeHead(200, { 'Content-Type': 'text/event-stream' })
    response.flushHeaders()
    response.write(fragment)
  })
  // 不同 Fetch 实现可能将损坏的响应视为读取失败或裸 EOF；两者均不能冒充任务完成。
  const { streamEvents } = await vite.ssrLoadModule('/src/api.ts')
  const received = []
  await assert.rejects(streamEvents(url, (event) => {
    received.push(event)
    if (event.type === 'text') {
      assert.ok(upstreamResponse)
      upstreamResponse.destroy(new Error('测试上游响应中途断开'))
    }
  }, {
    signal: AbortSignal.timeout(4000),
    onError: (message, code) => {
      const failure = new Error(message)
      failure.code = code
      throw failure
    },
  }), (error) => {
    assert.equal(error.code, 'STREAM_INCOMPLETE', '不得靠测试超时或取消代替断流传播')
    return true
  })
  assert.ok(received.some((event) => event.type === 'text' && event.content === '已收到的半稿。'))
  assert.equal(received.some((event) => event.type === 'done'), false)
})

test('下游主动取消连接会释放测试上游响应', async (t) => {
  let upstreamResponse
  let confirmUpstreamClosed
  const upstreamClosed = new Promise((resolve) => { confirmUpstreamClosed = resolve })
  const { url } = await setupProxy(t, (_request, response) => {
    upstreamResponse = response
    response.once('close', confirmUpstreamClosed)
    response.writeHead(200, { 'Content-Type': 'text/event-stream' })
    response.flushHeaders()
    response.write(fragment)
  })
  const response = await fetch(url, { signal: AbortSignal.timeout(4000) })
  const reader = response.body.getReader()
  await readFragment(reader)
  await reader.cancel()
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('下游取消后上游仍未释放')), 2000)
    upstreamClosed.then(() => {
      clearTimeout(timeout)
      resolve()
    })
  })
  assert.equal(upstreamResponse.destroyed, true)
})
