import type {
  AutopilotDraft,
  AutopilotDraftInput,
  AutopilotEvent,
  AutopilotTaskSnapshot,
  Chapter,
  ChapterAuthorFields,
  ChapterRepairPreview,
  ChapterRepairRequest,
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
  IdeaApplyResult,
  Novel,
  OutlineCheckResult,
  OutlineNode,
  OutlineApplyResult,
  OutlineVolumeDraft,
  QualityCurve,
  Relationship,
  RevisionSourceInput,
  SettleResult,
  SettleApplyResult,
  StoryCheckResult,
  WordLimits,
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
    throw Object.assign(new Error(detail), { code: body?.code, status: resp.status, task_id: body?.task_id })
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
  onDone: (data: any) => void | Promise<void>
  onError: (message: string, code?: string) => void | Promise<void>
}

export async function streamSSE(
  url: string,
  body: Record<string, unknown>,
  handlers: StreamHandlers,
): Promise<void> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let terminal: 'done' | 'error' | null = null
  let errorReported = false
  const reportError = async (message: string, code?: string) => {
    errorReported = true
    await handlers.onError(message, code)
  }
  const processEvent = async (part: string) => {
    const payload = part.split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (!payload.trim()) return
    let obj: any
    try {
      obj = JSON.parse(payload)
    } catch {
      throw new Error('生成数据格式无效，未收到有效的完成确认')
    }
    if (!obj || typeof obj !== 'object') throw new Error('生成数据格式无效')
    if (obj.type === 'token') {
      if (typeof obj.content !== 'string') throw new Error('生成文本格式无效')
      await handlers.onToken(obj.content)
    } else if (obj.type === 'done') {
      terminal = 'done'
      await handlers.onDone(obj)
    } else if (obj.type === 'error') {
      terminal = 'error'
      const message = typeof obj.detail === 'string' ? obj.detail
        : typeof obj.message === 'string' ? obj.message : '生成失败'
      await reportError(message || '生成失败', typeof obj.code === 'string' ? obj.code : undefined)
    }
  }
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok || !resp.body) {
      const err = await resp.json().catch(() => null)
      await reportError(typeof err?.detail === 'string' ? err.detail : `HTTP ${resp.status}`, err?.code)
      return
    }
    reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (!terminal) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      buffer = buffer.replace(/\r\n/g, '\n')
      const parts = buffer.split('\n\n')
      buffer = parts.pop() ?? ''
      for (const part of parts) {
        await processEvent(part)
        if (terminal) break
      }
      if (done) {
        if (!terminal && buffer.trim()) await processEvent(buffer)
        if (!terminal) throw new Error('连接提前结束，未收到完成确认')
        break
      }
    }
  } catch (error) {
    if (errorReported) throw error
    const message = error instanceof Error ? error.message : '生成连接中断'
    await reportError(message, terminal === 'done' ? 'CLIENT_UPDATE_FAILED' : 'STREAM_INCOMPLETE')
  } finally {
    if (reader) {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  }
}

export interface EventStreamOptions {
  signal?: AbortSignal
  onError?: (message: string, code?: string) => void | Promise<void>
}

export async function streamEvents(
  url: string,
  onEvent: (event: any) => void | Promise<void>,
  options: EventStreamOptions = {},
): Promise<void> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let terminal: 'done' | 'error' | null = null
  let errorReported = false
  const reportError = async (message: string, code: string) => {
    errorReported = true
    if (options.onError) await options.onError(message, code)
    else await onEvent({ type: 'error', scope: 'task', fatal: true, message, code })
  }
  const processEvent = async (part: string) => {
    const payload = part.split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (!payload.trim()) return
    let event: any
    try {
      event = JSON.parse(payload)
    } catch {
      throw new Error('自动驾驶数据格式无效，未收到完成确认')
    }
    if (!event || typeof event !== 'object' || Array.isArray(event) || typeof event.type !== 'string') {
      throw new Error('自动驾驶事件格式无效')
    }
    if (event.type === 'text' && (typeof event.content !== 'string' || typeof event.node_id !== 'string')) {
      throw new Error('自动驾驶正文格式无效')
    }
    if (event.type === 'done') terminal = 'done'
    else if (event.type === 'error' && event.fatal === true) terminal = 'error'
    await onEvent(event)
  }
  try {
    const response = await fetch(url, { signal: options.signal })
    if (!response.ok || !response.body) {
      const error = await response.json().catch(() => null)
      await reportError(typeof error?.detail === 'string' ? error.detail : `HTTP ${response.status}`, 'STREAM_REQUEST_FAILED')
      return
    }
    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (!terminal) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      buffer = buffer.replace(/\r\n/g, '\n')
      const parts = buffer.split('\n\n')
      buffer = parts.pop() ?? ''
      for (const part of parts) {
        await processEvent(part)
        if (terminal) break
      }
      if (done) {
        if (!terminal && buffer.trim()) await processEvent(buffer)
        if (!terminal) throw new Error('自动驾驶连接提前结束，未收到完成确认')
        break
      }
    }
  } catch (error) {
    if (errorReported) throw error
    if (options.signal?.aborted) return
    const message = error instanceof Error ? error.message : '自动驾驶连接中断'
    await reportError(message, terminal === 'done' ? 'CLIENT_UPDATE_FAILED' : 'STREAM_INCOMPLETE')
  } finally {
    if (reader) {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
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
  settleChapter: (novelId: string, chapterId: string, expectedAuthorFields?: ChapterAuthorFields) =>
    request<SettleResult>(`${BASE}/novels/${novelId}/chapters/${chapterId}/settle`, {
      method: 'POST',
      ...(expectedAuthorFields ? { body: JSON.stringify({ expected_author_fields: expectedAuthorFields }) } : {}),
    }),
  settleApply: (novelId: string, chapterId: string, suggestions: Suggestion[], sourceFingerprint: string) =>
    request<SettleApplyResult>(
      `${BASE}/novels/${novelId}/chapters/${chapterId}/settle/apply`,
      { method: 'POST', body: JSON.stringify({ suggestions, source_fingerprint: sourceFingerprint }) },
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
  checkChapterOutline: (novelId: string, chapterId: string, content: string) =>
    request<OutlineCheckResult>(`${BASE}/novels/${novelId}/chapters/${chapterId}/outline-check`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  checkChapterStory: (novelId: string, chapterId: string, content: string, directive: string, wordLimits?: WordLimits | null) =>
    request<StoryCheckResult>(`${BASE}/novels/${novelId}/chapters/${chapterId}/story-check`, {
      method: 'POST',
      body: JSON.stringify({ content, directive, ...(wordLimits === undefined ? {} : { word_limits: wordLimits }) }),
    }),
  previewChapterRepair: (novelId: string, chapterId: string, data: ChapterRepairRequest) =>
    request<ChapterRepairPreview>(`${BASE}/novels/${novelId}/chapters/${chapterId}/repair-preview`, {
      method: 'POST', body: JSON.stringify(data),
    }),
  applyChapterRepair: (novelId: string, chapterId: string, baseFingerprint: string, content: string) =>
    request<Chapter>(`${BASE}/novels/${novelId}/chapters/${chapterId}/repair-apply`, {
      method: 'POST', body: JSON.stringify({ base_fingerprint: baseFingerprint, content }),
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
  listAutopilotDrafts: (novelId: string) =>
    request<AutopilotDraft[]>(`${BASE}/novels/${novelId}/autopilot/drafts`),
  saveAutopilotDraft: (novelId: string, id: string, data: AutopilotDraftInput) =>
    request<AutopilotDraft>(`${BASE}/novels/${novelId}/autopilot/drafts/${id}`, {
      method: 'PUT', body: JSON.stringify(data),
    }),
  deleteAutopilotDraft: (novelId: string, id: string, revision: number) =>
    request<{ id: string; deleted: boolean; revision: number }>(
      `${BASE}/novels/${novelId}/autopilot/drafts/${id}?revision=${revision}`, { method: 'DELETE' },
    ),
  autopilotStatus: (novelId: string) =>
    request<AutopilotTaskSnapshot | { running: false; task_id?: never }>(
      `${BASE}/novels/${novelId}/autopilot/status`,
    ),
  startAutopilot: (novelId: string, requestId: string, wordLimits?: WordLimits | null) =>
    request<AutopilotTaskSnapshot>(`${BASE}/novels/${novelId}/autopilot/start`, {
      method: 'POST', body: JSON.stringify({ request_id: requestId, ...(wordLimits === undefined ? {} : { word_limits: wordLimits }) }),
    }),
  pauseAutopilot: (novelId: string, taskId: string) =>
    request<AutopilotEvent>(`${BASE}/novels/${novelId}/autopilot/tasks/${taskId}/pause`, { method: 'POST' }),
  resumeAutopilot: (novelId: string, taskId: string) =>
    request<AutopilotEvent>(`${BASE}/novels/${novelId}/autopilot/tasks/${taskId}/resume`, { method: 'POST' }),
  injectAutopilot: (novelId: string, taskId: string, directive: string) =>
    request<AutopilotEvent>(`${BASE}/novels/${novelId}/autopilot/tasks/${taskId}/inject`, {
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
    request<IdeaApplyResult>(
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
    request<IdeaApplyResult>(
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
