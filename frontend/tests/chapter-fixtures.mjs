// 受控服务端夹具：目录只投影元信息，完整章节只由详情/写入响应提供。
export function metadataFixture(chapter) {
  const { id, novel_id, outline_node_id = '', seq = 1, title = '', status = 'draft',
    tension_score = null, drift_score = null, created_at = '', updated_at = '' } = chapter
  return { id, novel_id, outline_node_id, seq, title, status, tension_score, drift_score, created_at, updated_at,
    word_count: Array.from((chapter.content ?? '').replace(/[\p{White_Space}\u001c-\u001f]/gu, '')).length }
}

export function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size }, key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key), clear: () => values.clear(), values,
  }
}

export function splitChapterFixtures(implementations, fallback) {
  const snapshots = new Map()
  const remember = (novelId, row) => snapshots.set(`${novelId}/${row.id}`, structuredClone(row))
  const api = { ...implementations }
  if (implementations.listChapterMetadata) api.listChapterMetadata = async (novelId) => {
    const rows = await implementations.listChapterMetadata(novelId)
    for (const row of rows) remember(novelId, row)
    return rows.map(metadataFixture)
  }
  if (!api.getChapter) api.getChapter = async (novelId, id) => {
    const row = snapshots.get(`${novelId}/${id}`) ?? fallback?.(novelId, id)
    if (!row) throw new Error(`受控详情不存在：${novelId}/${id}`)
    return structuredClone(row)
  }
  for (const name of ['createChapter', 'updateChapter', 'applyChapterRepair']) {
    if (implementations[name]) api[name] = async (...args) => {
      const row = await implementations[name](...args)
      if (row?.id) remember(args[0], row)
      return name === 'createChapter' ? { created_at: '', updated_at: '', creation_replayed: false, author_changed_since_creation: false, ...row } : row
    }
  }
  return api
}
