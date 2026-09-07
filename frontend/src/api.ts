import type { EntityRecord, Novel, Relationship } from './types'

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

  // 关系（与实体接口一致，单独保留类型便于图谱使用）
  listRelationships: (novelId: string) =>
    request<Relationship[]>(`${BASE}/novels/${novelId}/relationships`),
}
