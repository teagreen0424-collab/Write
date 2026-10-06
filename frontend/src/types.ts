// 实体类型与字段配置

export type EntityType =
  | 'character'
  | 'location'
  | 'world_setting'
  | 'timeline_event'
  | 'foreshadowing'
  | 'relationship'

export type FieldType = 'text' | 'textarea' | 'select' | 'json' | 'entity-select'

export interface FieldOption {
  label: string
  value: string
}

export interface FieldConfig {
  key: string
  label: string
  type: FieldType
  required?: boolean
  options?: any[]
  placeholder?: string
}

export interface EntityTypeConfig {
  type: EntityType
  label: string
  apiPath: string
  nameField: string
  fields: FieldConfig[]
}

export interface Novel {
  id: string
  title: string
  genre: string
  summary: string
  style_anchor: string
  created_at: string
  updated_at: string
}

export interface Relationship {
  id: string
  novel_id: string
  subject_id: string
  object_id: string
  relation_type: string
  note: string
  created_at: string
  updated_at: string
}

// 通用实体记录
export type EntityRecord = Record<string, any> & { id: string; novel_id: string }

export interface GraphNode {
  id: string
  name: string
  type: EntityType
  typeLabel: string
}

export interface GraphEdge {
  source: string
  target: string
  label: string
}

export const FORESHADOW_STATUS_OPTIONS: FieldOption[] = [
  { label: '已埋 planted', value: 'planted' },
  { label: '已悬置 suspended', value: 'suspended' },
  { label: '已消费 consumed', value: 'consumed' },
]

export const ENTITY_TYPES: EntityTypeConfig[] = [
  {
    type: 'character',
    label: '人物',
    apiPath: 'characters',
    nameField: 'name',
    fields: [
      { key: 'name', label: '姓名', type: 'text', required: true },
      { key: 'role', label: '身份', type: 'text' },
      { key: 'personality', label: '性格', type: 'textarea' },
      { key: 'goal', label: '目标', type: 'textarea' },
      { key: 'appearance', label: '外貌', type: 'textarea' },
      { key: 'catchphrase', label: '口头禅', type: 'text' },
      { key: 'pov', label: 'POV 视角', type: 'text' },
      { key: 'frequency', label: '登场频率', type: 'text' },
      { key: 'known_info', label: '已知信息', type: 'textarea' },
      { key: 'extras', label: '扩展字段(JSON)', type: 'json', placeholder: '{"阵营":"青云宗"}' },
    ],
  },
  {
    type: 'location',
    label: '地点',
    apiPath: 'locations',
    nameField: 'name',
    fields: [
      { key: 'name', label: '名称', type: 'text', required: true },
      { key: 'level', label: '层级', type: 'text' },
      { key: 'parent_id', label: '上级地点 ID', type: 'text' },
      { key: 'description', label: '描述', type: 'textarea' },
    ],
  },
  {
    type: 'world_setting',
    label: '世界观',
    apiPath: 'world-settings',
    nameField: 'name',
    fields: [
      { key: 'name', label: '条目名', type: 'text', required: true },
      { key: 'category', label: '类型', type: 'text' },
      { key: 'description', label: '说明', type: 'textarea' },
      { key: 'tags', label: '三元组标签(JSON)', type: 'json', placeholder: '["主体","关系","客体"]' },
    ],
  },
  {
    type: 'timeline_event',
    label: '时间线',
    apiPath: 'timeline-events',
    nameField: 'title',
    fields: [
      { key: 'title', label: '事件名', type: 'text', required: true },
      { key: 'time_point', label: '时间点', type: 'text' },
      { key: 'participants', label: '参与人物(JSON)', type: 'json', placeholder: '["人物ID"]' },
      { key: 'cause_id', label: '因果前序事件 ID', type: 'text' },
      { key: 'description', label: '描述', type: 'textarea' },
    ],
  },
  {
    type: 'foreshadowing',
    label: '伏笔',
    apiPath: 'foreshadowings',
    nameField: 'content',
    fields: [
      { key: 'content', label: '伏笔内容', type: 'textarea', required: true },
      { key: 'status', label: '状态', type: 'select', options: FORESHADOW_STATUS_OPTIONS },
      { key: 'related_characters', label: '关联人物(JSON)', type: 'json', placeholder: '["人物ID"]' },
      { key: 'planted_chapter_id', label: '埋设章节 ID', type: 'text' },
      { key: 'consumed_chapter_id', label: '消费章节 ID', type: 'text' },
    ],
  },
  {
    type: 'relationship',
    label: '关系',
    apiPath: 'relationships',
    nameField: 'relation_type',
    fields: [
      { key: 'subject_id', label: '主体实体', type: 'entity-select', required: true },
      { key: 'object_id', label: '客体实体', type: 'entity-select', required: true },
      { key: 'relation_type', label: '关系类型', type: 'text', required: true },
      { key: 'note', label: '备注', type: 'textarea' },
    ],
  },
]

export function getEntityConfig(type: EntityType): EntityTypeConfig {
  return ENTITY_TYPES.find((t) => t.type === type)!
}

// ================= 章节与写作 =================
export interface Chapter {
  id: string
  novel_id: string
  outline_node_id: string
  seq: number
  title: string
  content: string
  status: 'draft' | 'finalized'
  summary: string
  tension_score: number | null
  drift_score: number | null
  directive: string
  created_at: string
  updated_at: string
}

export interface Conflict {
  rule: string
  message: string
  start: number
  end: number
  entity_id: string
}

// ================= 章节版本（阶段八第一轮） =================
export type RevisionSource =
  | 'manual_save'
  | 'rewrite_apply'
  | 'continue'
  | 'rewrite'
  | 'restore'

/** PUT 更新章节时允许前端指定的来源，其余来源由后端写作用例决定 */
export type RevisionSourceInput = 'manual_save' | 'rewrite_apply'

/** 版本列表项：不包含正文 */
export interface ChapterRevisionMeta {
  id: string
  novel_id: string
  chapter_id: string
  revision_no: number
  source: RevisionSource
  title: string
  directive: string
  status: 'draft' | 'finalized'
  summary: string
  tension_score: number | null
  drift_score: number | null
  word_count: number
  created_at: string
}

/** 单个版本详情：包含完整正文 */
export interface ChapterRevision extends ChapterRevisionMeta {
  content: string
}

// ================= 成品交付（阶段八第二轮） =================
export type DeliveryFormat = 'markdown' | 'txt' | 'docx'
export type DeliveryScope = 'all' | 'volume' | 'chapters'

export interface DeliveryRequest {
  format: DeliveryFormat
  scope: DeliveryScope
  volume_id: string
  chapter_ids: string[]
  include_drafts: boolean
}

export interface BookChapterSummary {
  id: string
  seq: number
  title: string
  status: 'draft' | 'finalized'
  word_count: number
  outline_node_id: string
  volume_id: string
  volume_title: string
  tension_score: number | null
  drift_score: number | null
}

export interface BookSummary {
  novel_id: string
  title: string
  chapter_count: number
  word_count: number
  draft_count: number
  finalized_count: number
  volumes: { id: string; title: string }[]
  chapters: BookChapterSummary[]
}

export interface DeliveryFinding {
  id: string
  seq?: number
  title?: string
  content?: string
  status?: string
}

export interface DeliveryRuleConflict extends Conflict {
  chapter_id: string
  seq: number
  title: string
}

export interface DeliveryPreflight {
  chapter_count: number
  word_count: number
  draft_chapters: DeliveryFinding[]
  unbound_chapters: DeliveryFinding[]
  unresolved_foreshadowings: DeliveryFinding[]
  rule_conflicts: DeliveryRuleConflict[]
  ready: boolean
}

export const REVISION_SOURCE_LABELS: Record<RevisionSource, string> = {
  manual_save: '手工保存',
  rewrite_apply: '改写应用',
  continue: '续写',
  rewrite: '直接改写',
  restore: '恢复前快照',
}

export function revisionSourceLabel(source: string): string {
  return REVISION_SOURCE_LABELS[source as RevisionSource] ?? source
}

// 正文分段：冲突一种底色、套路命中一种底色
export interface Segment {
  text: string
  kind: 'plain' | 'conflict' | 'cliche'
}

export interface Suggestion {
  type: 'character' | 'timeline_event' | 'foreshadowing'
  data: Record<string, unknown>
}

export interface SettleResult {
  chapter_id: string
  summary: string
  suggestions: Suggestion[]
  drift: number | null
  conflicts: Conflict[]
}

// ================= 大纲 / 自动驾驶 =================
export type OutlineLevel = 'volume' | 'act' | 'chapter'

export interface OutlineNode {
  id: string
  novel_id: string
  level: OutlineLevel
  title: string
  summary: string
  parent_id: string
  sort_order: number
  created_at: string
  updated_at: string
}

export interface OutlineChapterDraft {
  title: string
  summary: string
}

export interface OutlineActDraft {
  title: string
  summary: string
  chapters: OutlineChapterDraft[]
}

export interface OutlineVolumeDraft {
  title: string
  summary: string
  acts: OutlineActDraft[]
  chapters: OutlineChapterDraft[]
}

export interface OutlineApplyResult {
  volume_id: string
  node_ids: string[]
}

export interface ChatAction {
  action_type: string
  status: string
  result_id: string
  result_summary: Record<string, unknown>
  target_deleted: boolean
}

export interface AutopilotEvent {
  type:
    | 'snapshot'
    | 'control'
    | 'progress'
    | 'text'
    | 'check'
    | 'story_check'
    | 'word_check'
    | 'chapter_done'
    | 'tension'
    | 'drift'
    | 'paused'
    | 'error'
    | 'done'
  scope?: 'chapter' | 'quality' | 'task'
  phase?: 'starting' | 'generating' | 'skipped' | 'generated'
  status?: AutopilotTaskStatus
  task_id?: string
  novel_id?: string
  event_id?: number
  action?: 'pause' | 'resume' | 'inject'
  saved?: boolean
  fatal?: boolean
  node_id?: string
  title?: string
  code?: string
  generated?: number
  skipped?: number
  failed?: number
  draft_id?: string
  draft_revision?: number
  draft_attempt?: number
  draft_persisted?: boolean
  issues?: StoryCheckIssue[]
  warnings?: string[]
  truncated?: boolean
  review_complete?: boolean
  word_count?: number
  min_words?: number | null
  max_words?: number | null
  within_limits?: boolean | null
  requirements?: StoryRequirement[]
  requirement_checks?: StoryRequirementCheck[]
  unreviewed_requirement_ids?: string[]
  [key: string]: any
}

export type AutopilotTaskStatus = 'starting' | 'running' | 'paused' | 'completed' | 'incomplete' | 'stopped' | 'interrupted'

export interface AutopilotActiveDraft {
  node_id: string
  title: string
  content: string
  draft_id: string
  draft_revision: number
  draft_attempt: number
  draft_persisted: boolean
}

export interface AutopilotTaskSnapshot extends AutopilotEvent {
  type: 'snapshot'
  task_id: string
  novel_id: string
  event_id: number
  status: AutopilotTaskStatus
  running: boolean
  paused: boolean
  pause_requested: boolean
  generated: number
  skipped: number
  failed: number
  done: number
  total: number
  current_node_id: string
  current_seq: number
  message: string
  task_notice: string
  terminal_reason: string
  active_drafts: AutopilotActiveDraft[]
  quality_warnings: string[]
  drift_warnings: { seq: number; score: number; message: string }[]
  word_limits: WordLimits | null
}

/** 暂存片段独立于正式章节，重开页面时 active 按未确认状态恢复。 */
export interface AutopilotDraft {
  id: string
  novel_id: string
  node_id: string
  title: string
  attempt: number
  content: string
  reason: string
  state: 'active' | 'failed' | 'unconfirmed'
  revision: number
  created_at: string
  updated_at: string
}

export type AutopilotDraftInput = Pick<AutopilotDraft,
  'node_id' | 'title' | 'attempt' | 'content' | 'reason' | 'state' | 'revision'>

// ================= 对话式构思 =================
export interface IdeaChange {
  type: string
  action: 'create' | 'update'
  data: Record<string, unknown>
}

export interface IdeaChatResult {
  text: string
  targets: string[]
  changes: IdeaChange[]
  reason: string
  matched: { name: string; matched: boolean; type?: string; id?: string }[]
}

// ================= 全局对话 =================
export interface Conversation {
  id: string
  novel_id: string | null
  title: string
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  meta: Record<string, unknown> | null
  created_at: string
}

// ================= 质量治理（阶段七） =================
export interface ClicheHit {
  term: string
  category: string
  start: number
  end: number
}

export interface ClicheResult {
  hits: ClicheHit[]
  total: number
  density: number
  by_category: Record<string, number>
}

export interface QualityCurvePoint {
  seq: number
  title: string
  status: string
  tension_score: number | null
  drift_score: number | null
}

export interface QualityCurve {
  chapters: QualityCurvePoint[]
  anchor_set: boolean
  drift_threshold: number
}

export interface ForeshadowingTrack {
  id: string
  content: string
  status: 'planted' | 'suspended' | 'consumed'
  related_characters: unknown[]
  planted_chapter_id: string
  consumed_chapter_id: string
  planted_seq: number | null
  suspended_chapters: number | null
  long_pending: boolean
}

export interface ForeshadowingTracking {
  groups: {
    planted: ForeshadowingTrack[]
    suspended: ForeshadowingTrack[]
    consumed: ForeshadowingTrack[]
  }
  latest_seq: number
  chapter_count: number
  long_pending_threshold: { chapters: number; ratio: number }
}

export interface DeepCheckConflict {
  rule: string
  type: string
  excerpt: string
  reason: string
  severity: 'high' | 'medium' | 'low'
  chunk_index: number
}

export interface DeepCheckResult {
  conflicts: DeepCheckConflict[]
  chunks_checked: number
  chunks_skipped: number
  truncated: boolean
}

export interface OutlineCheckIssue {
  kind: 'missing' | 'changed' | 'contradiction'
  severity: 'high' | 'medium' | 'low'
  outline_point: string
  excerpt: string
  reason: string
}

export interface OutlineCheckResult {
  issues: OutlineCheckIssue[]
  truncated: boolean
  outline_title: string
}

export interface StoryCheckIssue {
  category: 'directive' | 'continuity' | 'setting' | 'outline'
  severity: 'high' | 'medium' | 'low'
  requirement: string
  excerpt: string
  reason: string
}

export interface StoryCheckResult {
  issues: StoryCheckIssue[]
  truncated: boolean
  history_truncated: boolean
  review_complete: boolean
  previous_chapter_title: string
  chapter_title: string
  requirements: StoryRequirement[]
  requirement_checks: StoryRequirementCheck[]
  unreviewed_requirement_ids: string[]
  word_count: WordCountCheck | null
}

export interface StoryRequirement {
  id: string
  source: 'directive' | 'outline'
  text: string
}

export interface StoryRequirementCheck {
  requirement_id: string
  status: 'violation' | 'satisfied' | 'uncertain'
  excerpt: string
  reason: string
}

export interface WordLimits {
  min_words: number | null
  max_words: number | null
}

export interface WordCountCheck extends WordLimits {
  count: number
  within_limits: boolean | null
  warnings: string[]
}
