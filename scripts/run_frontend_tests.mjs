import { spawn } from 'node:child_process'
import { readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

// 显式列出文件，兼容不支持 --test glob 的 Node 18 与 Windows shell。
const frontend = new URL('../frontend/', import.meta.url)
const testDirectory = new URL('tests/', frontend)
const files = (await readdir(testDirectory, { withFileTypes: true }))
  .filter((entry) => entry.isFile() && entry.name.endsWith('.test.mjs'))
  .map((entry) => join(fileURLToPath(testDirectory), entry.name))
  .sort()

if (files.length === 0) throw new Error('No frontend tests were discovered')

const child = spawn(process.execPath, ['--test', ...files], {
  cwd: fileURLToPath(frontend),
  stdio: 'inherit',
  windowsHide: true,
})
child.once('error', (error) => {
  console.error(error.message)
  process.exitCode = 1
})
child.once('exit', (code) => { process.exitCode = code ?? 1 })
