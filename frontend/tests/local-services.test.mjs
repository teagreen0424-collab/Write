import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const checker = fileURLToPath(new URL('../../scripts/check_services.ps1', import.meta.url))
const lingmoHealth = JSON.stringify({ status: 'ok', app: 'lingmo-writer', version: '0.1.0' })
const lingmoPage = '<!doctype html><title>Lingmo</title><div id="app"></div>'

async function serve(t, handler) {
  const server = createServer(handler)
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  t.after(async () => {
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  })
  return 'http://127.0.0.1:' + server.address().port
}

function respond(response, content, contentType = 'application/json', status = 200) {
  response.writeHead(status, { 'Content-Type': contentType + '; charset=utf-8' })
  response.end(content)
}

function runChecker(backendUrl, frontendUrl) {
  return new Promise((resolve, reject) => {
    const child = spawn('powershell.exe', [
      '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', checker,
      '-BackendUrl', backendUrl, '-FrontendUrl', frontendUrl,
    ], { windowsHide: true })
    const timer = setTimeout(() => {
      child.kill()
      reject(new Error('Service checker exceeded its test time limit'))
    }, 10000)
    let output = ''
    child.stdout.on('data', (chunk) => { output += chunk })
    child.stderr.on('data', (chunk) => { output += chunk })
    child.once('error', (error) => {
      clearTimeout(timer)
      reject(error)
    })
    child.once('close', (code) => {
      clearTimeout(timer)
      resolve({ code, output })
    })
  })
}

const options = { skip: process.platform !== 'win32' }

test('启动检查确认真实后端身份、前端页面和API代理', options, async (t) => {
  const backend = await serve(t, (request, response) => {
    if (request.url === '/api/v1/health') respond(response, lingmoHealth)
    else respond(response, 'not found', 'text/plain', 404)
  })
  const frontend = await serve(t, (request, response) => {
    if (request.url === '/') respond(response, lingmoPage, 'text/html')
    else if (request.url === '/api/v1/health') respond(response, lingmoHealth)
    else respond(response, 'not found', 'text/plain', 404)
  })
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 0, result.output)
})

test('启动检查拒绝其他程序返回的HTTP200与假健康状态', options, async (t) => {
  const backend = await serve(t, (_request, response) => respond(response, '{"status":"ok","app":"other-app"}'))
  const frontend = await serve(t, (_request, response) => respond(response, lingmoPage, 'text/html'))
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 1, result.output)
  assert.match(result.output, /not a ready Lingmo backend/)
})

test('启动检查拒绝非JSON的HTTP200后端页面', options, async (t) => {
  const backend = await serve(t, (_request, response) => respond(response, '<title>Other docs</title>', 'text/html'))
  const frontend = await serve(t, (_request, response) => respond(response, lingmoPage, 'text/html'))
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 1, result.output)
  assert.match(result.output, /did not return Lingmo health JSON/)
})

test('启动检查拒绝相同端口上其他应用的首页', options, async (t) => {
  const backend = await serve(t, (_request, response) => respond(response, lingmoHealth))
  const frontend = await serve(t, (_request, response) => respond(response, '<title>Other app</title><div id="app"></div>', 'text/html'))
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 1, result.output)
  assert.match(result.output, /not the Lingmo frontend/)
})

test('启动检查拒绝首页可用但API代理未连接的前端', options, async (t) => {
  const backend = await serve(t, (_request, response) => respond(response, lingmoHealth))
  const frontend = await serve(t, (request, response) => {
    if (request.url === '/') respond(response, lingmoPage, 'text/html')
    else respond(response, 'proxy unavailable', 'text/plain', 502)
  })
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 1, result.output)
  assert.match(result.output, /\[ERROR\]/)
})

test('启动检查对无响应的服务在短超时后退出失败', options, async (t) => {
  const backend = await serve(t, () => {})
  const frontend = await serve(t, (_request, response) => respond(response, lingmoPage, 'text/html'))
  const started = performance.now()
  const result = await runChecker(backend, frontend)
  assert.equal(result.code, 1, result.output)
  assert.match(result.output, /\[ERROR\]/)
  assert.ok(performance.now() - started < 8000, '单次就绪检查不应长期挂起')
})
