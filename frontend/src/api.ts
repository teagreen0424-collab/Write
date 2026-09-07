import type {
  Chapter,
  Conflict,
  EntityRecord,
  Novel,
  Relationship,
  SettleResult,
  Suggestion,
} from './types'

const BASE = '/api/v1'

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => null)
    const detail =
      typeof body?.detail === 'string'
        ? body.detail
        : body?.detail?.[0]?.msg || `HTTP ${resp.status}`
    throw new Error(detail)
  }
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

async function fetchText(url: string): Promise<string> {
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return await resp.text()
}

export interface StreamHandlers {
  onToken: (text: string) => void
  onDone: (data: any) => void
  onError: (message: string) => void
}

export async function streamSSE(
  url: string,
  body: Record<string, unknown>,
  handlers: StreamHandlers,
): Promise<void> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => null)
    handlers.onError(err?.detail ?? `HTTP ${resp.status}`)
    return
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''
    for (const part of parts) {
      const line = part.trim()
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      let obj: any
      try {
        obj = JSON.parse(payload)
      } catch {
        continue
      }
      if (obj.type === 'token') handlers.onToken(obj.content)
      else if (obj.type === 'done') handlers.onDone(obj)
      else if (obj.type === 'error') handlers.onError(obj.message)
    }
  }
}

export const api = {
  // 作品
  listNovels: () => request<Novel[]>(`${BASE}/novels`),
  createNovel: (data: { title: string; genre?: string; summary?: string }) =>
    request<Novel>(`${BASE}/novels`, { method: 'POST', body: JSON.stringify(data) }),
  updateNovel: (id: string, data: Partial<Novel>) =>
    request<Novel>(`${BASE}/novels/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteNovel: (id: string) => request<void>(`${BASE}/novels/${id}`, { method: 'DELETE' }),

  // 六类实体（作品作用域下）
  listEntities: (novelId: string, apiPath: string) =>
    request<EntityRecord[]>(`${BASE}/novels/${novelId}/${apiPath}`),
  createEntity: (novelId: string, apiPath: string, data: Record<string, unknown>) =>
    request<EntityRecord>(`${BASE}/novels/${novelId}/${apiPath}`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateEntity: (novelId: string, apiPath: string, id: string, data: Record<string, unknown>) =>
    request<EntityRecord>(`${BASE}/novels/${novelId}/${apiPath}/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteEntity: (novelId: string, apiPath: string, id: string) =>
    request<void>(`${BASE}/novels/${novelId}/${apiPath}/${id}`, { method: 'DELETE' }),

  // 关系
  listRelationships: (novelId: string) =>
    request<Relationship[]>(`${BASE}/novels/${novelId}/relationships`),

  // 章节
  listChapters: (novelId: string) =>
    request<Chapter[]>(`${BASE}/novels/${novelId}/chapters`),
  createChapter: (novelId: string, data: Record<string, unknown>) =>
    request<Chapter>(`${BASE}/novels/${novelId}/chapters`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateChapter: (novelId: string, id: string, data: Record<string, unknown>) =>
    request<Chapter>(`${BASE}/novels/${novelId}/chapters/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteChapter: (novelId: string, id: string) =>
    request<void>(`${BASE}/novels/${novelId}/chapters/${id}`, { method: 'DELETE' }),

  // 定稿沉淀
  settleChapter: (novelId: string, chapterId: string) =>
    request<SettleResult>(`${BASE}/novels/${novelId}/chapters/${chapterId}/settle`, {
      method: 'POST',
    }),
  settleApply: (novelId: string, chapterId: string, suggestions: Suggestion[]) =>
    request<{ created: { type: string; id: string }[] }>(
      `${BASE}/novels/${novelId}/chapters/${chapterId}/settle/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions }) },
    ),

  // 一致性检查
  checkConsistency: (novelId: string, content: string) =>
    request<{ conflicts: Conflict[] }>(`${BASE}/novels/${novelId}/consistency`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),

  // 导出
  exportMarkdown: (novelId: string) => fetchText(`${BASE}/novels/${novelId}/export/markdown`),
  exportTxt: (novelId: string) => fetchText(`${BASE}/novels/${novelId}/export/txt`),
}
