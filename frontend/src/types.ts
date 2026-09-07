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

export interface Suggestion {
  type: 'character' | 'timeline_event' | 'foreshadowing'
  data: Record<string, unknown>
}

export interface SettleResult {
  chapter_id: string
  summary: string
  suggestions: Suggestion[]
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

export interface AutopilotEvent {
  type:
    | 'progress'
    | 'text'
    | 'check'
    | 'chapter_done'
    | 'tension'
    | 'drift'
    | 'paused'
    | 'error'
    | 'done'
  [key: string]: any
}
