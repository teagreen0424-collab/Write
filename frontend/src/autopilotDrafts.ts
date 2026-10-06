import type { AutopilotDraftInput } from './types'

export type DraftSaveState = 'pending' | 'saving' | 'saved' | 'error' | 'deleted'
interface SaveStatus { state: DraftSaveState; error?: string }
interface DraftTransport {
  save: (novelId: string, id: string, data: AutopilotDraftInput) => Promise<unknown>
  remove: (novelId: string, id: string, revision: number) => Promise<unknown>
}
interface PendingWrite { kind: 'save' | 'delete'; revision: number; due: number }

/** 每作品只有一条写队列；删除在迟到写之后执行，并阻止同一 ID 再入队。 */
export class AutopilotDraftWriter {
  private records = new Map<string, AutopilotDraftInput>()
  private pending = new Map<string, PendingWrite>()
  private deleted = new Set<string>()
  private deleteRevisions = new Map<string, number>()
  private statuses = new Map<string, SaveStatus>()
  private listeners = new Set<(id: string, status: SaveStatus) => void>()
  private busy = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private idleWaiters: (() => void)[] = []
  private deleteWaiters = new Map<string, ((saved: boolean) => void)[]>()

  constructor(readonly novelId: string, private transport: DraftTransport, private delay = 300) {}

  subscribe(listener: (id: string, status: SaveStatus) => void) {
    this.listeners.add(listener)
    for (const [id, status] of this.statuses) listener(id, status)
    return () => { this.listeners.delete(listener) }
  }

  private report(id: string, status: SaveStatus) {
    this.statuses.set(id, status)
    for (const listener of this.listeners) listener(id, status)
  }

  save(id: string, data: AutopilotDraftInput, immediate = false) {
    if (this.deleted.has(id)) return
    const previous = this.records.get(id)
    const revision = Math.max(previous?.revision || 0, data.revision || 0) + 1
    this.records.set(id, { ...data, revision })
    this.pending.set(id, { kind: 'save', revision, due: Date.now() + (immediate || !previous ? 0 : this.delay) })
    this.report(id, { state: 'pending' })
    this.schedule()
  }

  async remove(id: string, revision: number): Promise<boolean> {
    this.deleted.add(id)
    const nextRevision = Math.max(this.records.get(id)?.revision || 0, revision) + 1
    this.deleteRevisions.set(id, nextRevision)
    this.pending.set(id, { kind: 'delete', revision: nextRevision, due: 0 })
    this.report(id, { state: 'pending' })
    const result = new Promise<boolean>((resolve) => {
      this.deleteWaiters.set(id, [...(this.deleteWaiters.get(id) || []), resolve])
    })
    this.schedule()
    return result
  }

  async flush() {
    for (const operation of this.pending.values()) operation.due = 0
    this.schedule()
    if (!this.busy && !this.pending.size) return
    await new Promise<void>((resolve) => this.idleWaiters.push(resolve))
  }

  async retry() {
    for (const [id, status] of this.statuses) {
      if (status.state !== 'error') continue
      const record = this.records.get(id)
      if (this.deleted.has(id)) {
        this.pending.set(id, { kind: 'delete', revision: this.deleteRevisions.get(id) || 1, due: 0 })
      } else if (record) {
        // 网络失败可能发生在服务器已写入之后，同版本同正文重试保持幂等。
        this.pending.set(id, { kind: 'save', revision: record.revision, due: 0 })
      }
      this.report(id, { state: 'pending' })
    }
    await this.flush()
  }

  private schedule() {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    if (this.busy) return
    const now = Date.now()
    const ready = [...this.pending].find(([, operation]) => operation.due <= now)
    if (ready) { void this.write(ready[0], ready[1]); return }
    if (this.pending.size) {
      this.timer = setTimeout(() => this.schedule(), Math.max(0, Math.min(...[...this.pending.values()].map((op) => op.due)) - now))
    } else {
      for (const resolve of this.idleWaiters.splice(0)) resolve()
    }
  }

  private async write(id: string, operation: PendingWrite) {
    this.busy = true
    this.pending.delete(id)
    this.report(id, { state: 'saving' })
    let success = false
    try {
      if (operation.kind === 'delete') await this.transport.remove(this.novelId, id, operation.revision)
      else await this.transport.save(this.novelId, id, { ...this.records.get(id)!, revision: operation.revision })
      success = true
      if (!this.pending.has(id)) this.report(id, { state: operation.kind === 'delete' ? 'deleted' : 'saved' })
    } catch (error) {
      if (!this.pending.has(id)) this.report(id, { state: 'error', error: (error as Error).message })
    } finally {
      if (operation.kind === 'delete') {
        for (const resolve of this.deleteWaiters.get(id) || []) resolve(success)
        this.deleteWaiters.delete(id)
      }
      this.busy = false
      this.schedule()
    }
  }
}

// 组件离开再打开时沿用同作品队列，防止旧请求与新页面并发直写。
const writers = new Map<string, AutopilotDraftWriter>()
export function getAutopilotDraftWriter(novelId: string, transport: DraftTransport) {
  let writer = writers.get(novelId)
  if (!writer) {
    writer = new AutopilotDraftWriter(novelId, transport)
    writers.set(novelId, writer)
  }
  return writer
}
