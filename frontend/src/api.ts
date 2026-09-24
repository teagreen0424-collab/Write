import type {
  Chapter,
  ChatAction,
  ChapterRevision,
  ChapterRevisionMeta,
  ChatMessage,
  ClicheResult,
  BookSummary,
  Conflict,
  Conversation,
  DeepCheckResult,
  DeliveryPreflight,
  DeliveryRequest,
  EntityRecord,
  ForeshadowingTracking,
  IdeaChatResult,
  Novel,
  OutlineNode,
  OutlineApplyResult,
  OutlineVolumeDraft,
  QualityCurve,
  Relationship,
  RevisionSourceInput,
  SettleResult,
  Suggestion,
} from './types'

const BASE = '/api/v1'

/** 章节更新载荷：revision_source 决定覆盖前快照的来源，不落库到 chapter 表 */
export interface ChapterUpdatePayload extends Record<string, unknown> {
  revision_source?: RevisionSourceInput
}

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
      else if (obj.type === 'error') handlers.onError(obj.detail ?? obj.message)
    }
  }
}

export async function streamEvents(
  url: string,
  onEvent: (event: any) => void,
): Promise<void> {
  const resp = await fetch(url)
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => null)
    onEvent({ type: 'error', message: err?.detail ?? `HTTP ${resp.status}` })
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
      try {
        onEvent(JSON.parse(payload))
      } catch {
        /* ignore */
      }
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
  updateChapter: (novelId: string, id: string, data: ChapterUpdatePayload) =>
    request<Chapter>(`${BASE}/novels/${novelId}/chapters/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteChapter: (novelId: string, id: string) =>
    request<void>(`${BASE}/novels/${novelId}/chapters/${id}`, { method: 'DELETE' }),

  // 章节版本
  listChapterRevisions: (novelId: string, chapterId: string) =>
    request<ChapterRevisionMeta[]>(`${BASE}/novels/${novelId}/chapters/${chapterId}/revisions`),
  getChapterRevision: (novelId: string, chapterId: string, revisionId: string) =>
    request<ChapterRevision>(
      `${BASE}/novels/${novelId}/chapters/${chapterId}/revisions/${revisionId}`,
    ),
  restoreChapterRevision: (novelId: string, chapterId: string, revisionId: string) =>
    request<Chapter>(`${BASE}/novels/${novelId}/chapters/${chapterId}/revisions/${revisionId}/restore`, {
      method: 'POST',
    }),

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
  checkConsistencyDeep: (novelId: string, content: string) =>
    request<DeepCheckResult>(`${BASE}/novels/${novelId}/consistency/deep`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),

  // 质量治理（阶段七）
  scanCliche: (novelId: string, content: string) =>
    request<ClicheResult>(`${BASE}/novels/${novelId}/quality/cliche`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  getQualityCurve: (novelId: string) =>
    request<QualityCurve>(`${BASE}/novels/${novelId}/quality/curve`),
  getForeshadowings: (novelId: string) =>
    request<ForeshadowingTracking>(`${BASE}/novels/${novelId}/quality/foreshadowings`),

  // 导出
  exportMarkdown: (novelId: string) => fetchText(`${BASE}/novels/${novelId}/export/markdown`),
  exportTxt: (novelId: string) => fetchText(`${BASE}/novels/${novelId}/export/txt`),
  getBookSummary: (novelId: string) =>
    request<BookSummary>(`${BASE}/novels/${novelId}/book/summary`),
  preflightExport: (novelId: string, data: DeliveryRequest) =>
    request<DeliveryPreflight>(`${BASE}/novels/${novelId}/export/preflight`, {
      method: 'POST', body: JSON.stringify(data),
    }),
  downloadBookFile: async (novelId: string, data: DeliveryRequest): Promise<Blob> => {
    const resp = await fetch(`${BASE}/novels/${novelId}/export/file`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    })
    if (!resp.ok) {
      const body = await resp.json().catch(() => null)
      throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${resp.status}`)
    }
    return resp.blob()
  },

  // 大纲
  listOutline: (novelId: string) =>
    request<OutlineNode[]>(`${BASE}/novels/${novelId}/outline`),
  createOutline: (novelId: string, data: Record<string, unknown>) =>
    request<OutlineNode>(`${BASE}/novels/${novelId}/outline`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateOutline: (novelId: string, id: string, data: Record<string, unknown>) =>
    request<OutlineNode>(`${BASE}/novels/${novelId}/outline/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteOutline: (novelId: string, id: string) =>
    request<void>(`${BASE}/novels/${novelId}/outline/${id}`, { method: 'DELETE' }),
  reorderOutline: (novelId: string, orderedIds: string[]) =>
    request<OutlineNode[]>(`${BASE}/novels/${novelId}/outline/reorder`, {
      method: 'POST',
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  previewOutline: (novelId: string, sourceText: string) =>
    request<OutlineVolumeDraft>(`${BASE}/novels/${novelId}/outline/preview`, {
      method: 'POST', body: JSON.stringify({ source_text: sourceText }),
    }),
  applyOutline: (novelId: string, plan: OutlineVolumeDraft) =>
    request<OutlineApplyResult>(`${BASE}/novels/${novelId}/outline/apply`, {
      method: 'POST', body: JSON.stringify({ plan }),
    }),
  rebuildSettingIndex: (novelId: string) =>
    request<{ indexed: number; failed: number }>(`${BASE}/novels/${novelId}/memory/rebuild-settings`, {
      method: 'POST',
    }),

  // 自动驾驶
  autopilotStatus: (novelId: string) =>
    request<{ running: boolean; paused?: boolean; current_node_id?: string; fail_count?: number }>(
      `${BASE}/novels/${novelId}/autopilot/status`,
    ),
  pauseAutopilot: (novelId: string) =>
    request<{ status: string }>(`${BASE}/novels/${novelId}/autopilot/pause`, { method: 'POST' }),
  resumeAutopilot: (novelId: string) =>
    request<{ status: string }>(`${BASE}/novels/${novelId}/autopilot/resume`, { method: 'POST' }),
  injectAutopilot: (novelId: string, directive: string) =>
    request<{ status: string }>(`${BASE}/novels/${novelId}/autopilot/inject`, {
      method: 'POST',
      body: JSON.stringify({ directive }),
    }),

  // 对话式构思
  ideaChat: (novelId: string, text: string) =>
    request<IdeaChatResult>(`${BASE}/novels/${novelId}/ideas/chat`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  ideaApply: (
    novelId: string,
    suggestions: Record<string, unknown>[],
    rawText = '',
    channel = 'chat',
  ) =>
    request<{ applied: { type: string; action: string; id: string }[]; outline_updates: unknown[]; consistency: { conflicts: Conflict[] } }>(
      `${BASE}/novels/${novelId}/ideas/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions, raw_text: rawText, channel }) },
    ),

  // 全局对话
  listConversations: () => request<Conversation[]>(`${BASE}/conversations`),
  createConversation: (novelId?: string | null) =>
    request<Conversation>(`${BASE}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ novel_id: novelId ?? null }),
    }),
  updateConversation: (id: string, data: { title?: string; novel_id?: string }) =>
    request<Conversation>(`${BASE}/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteConversation: (id: string) =>
    request<void>(`${BASE}/conversations/${id}`, { method: 'DELETE' }),
  listMessages: (id: string) => request<ChatMessage[]>(`${BASE}/conversations/${id}/messages`),
  listMessageActions: (id: string, messageId: string) =>
    request<ChatAction[]>(`${BASE}/conversations/${id}/messages/${messageId}/actions`),
  previewMessageSettings: (id: string, messageId: string) =>
    request<IdeaChatResult>(`${BASE}/conversations/${id}/messages/${messageId}/settings/preview`, {
      method: 'POST',
    }),
  applyMessageSettings: (id: string, messageId: string, suggestions: Record<string, unknown>[]) =>
    request<{ applied: { type: string; action: string; id: string }[] }>(
      `${BASE}/conversations/${id}/messages/${messageId}/settings/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions }) },
    ),
  previewMessageOutline: (id: string, messageId: string) =>
    request<OutlineVolumeDraft>(`${BASE}/conversations/${id}/messages/${messageId}/outline/preview`, {
      method: 'POST',
    }),
  applyMessageOutline: (id: string, messageId: string, plan: OutlineVolumeDraft) =>
    request<OutlineApplyResult>(`${BASE}/conversations/${id}/messages/${messageId}/outline/apply`, {
      method: 'POST', body: JSON.stringify({ plan }),
    }),
  writeMessageChapter: (
    id: string, messageId: string,
    data: { title: string; directive: string; outline_node_id: string; previous_chapter_id: string },
    handlers: StreamHandlers,
  ) => streamSSE(`${BASE}/conversations/${id}/messages/${messageId}/chapter/write`, data, handlers),
  updateMessage: (id: string, messageId: string, meta: Record<string, unknown>) =>
    request<ChatMessage>(`${BASE}/conversations/${id}/messages/${messageId}`, {
      method: 'PATCH',
      body: JSON.stringify({ meta }),
    }),
  promoteConversation: (id: string, data: { title: string; genre?: string; summary?: string }) =>
    request<Novel>(`${BASE}/conversations/${id}/promote`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  sendMessage: (id: string, content: string, handlers: StreamHandlers) =>
    streamSSE(`${BASE}/conversations/${id}/messages`, { content }, handlers),
}
