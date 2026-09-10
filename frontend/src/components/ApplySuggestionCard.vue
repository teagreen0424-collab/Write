<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NEmpty, NInput, NSpin, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { IdeaChatResult } from '../types'

const props = defineProps<{ novelId: string; text: string }>()
const emit = defineEmits<{
  (e: 'applied', summary: { type: string; action: string; id: string }[]): void
  (e: 'cancel'): void
}>()

const message = useMessage()
const loading = ref(true)
const applying = ref(false)
const result = ref<IdeaChatResult | null>(null)
const dataTexts = ref<string[]>([])

const TYPE_LABEL: Record<string, string> = {
  character: '人物',
  location: '地点',
  world_setting: '世界观',
  timeline_event: '时间线',
  foreshadowing: '伏笔',
  relationship: '关系',
}

async function load() {
  loading.value = true
  try {
    const r = await api.ideaChat(props.novelId, props.text)
    result.value = r
    dataTexts.value = r.changes.map((c) => JSON.stringify(c.data, null, 2))
  } catch (e) {
    message.error((e as Error).message)
    emit('cancel')
  } finally {
    loading.value = false
  }
}

async function apply() {
  if (!result.value) return
  const suggestions: Record<string, unknown>[] = []
  for (let i = 0; i < result.value.changes.length; i++) {
    const c = result.value.changes[i]
    try {
      suggestions.push({ type: c.type, action: c.action, data: JSON.parse(dataTexts.value[i]) })
    } catch {
      message.error('变更数据 JSON 格式错误')
      return
    }
  }
  if (!suggestions.length) {
    message.error('无可应用的变更')
    return
  }
  applying.value = true
  try {
    const r = await api.ideaApply(props.novelId, suggestions)
    message.success(`已应用 ${r.applied.length} 条变更`)
    emit('applied', r.applied)
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    applying.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="apply-card">
    <n-spin :show="loading">
      <template v-if="result">
        <div class="reason">
          <n-tag type="info" size="small">解析结果</n-tag>
          <span v-if="result.reason">原因：{{ result.reason }}</span>
        </div>

        <div class="targets">
          <div v-for="(m, i) in result.matched" :key="i" class="target">
            <n-tag :type="m.matched ? 'success' : 'warning'" size="small">{{ m.name }}</n-tag>
            <span v-if="m.matched">{{ m.type }}（已匹配）</span>
            <span v-else>未匹配（将新建）</span>
          </div>
        </div>

        <div v-for="(c, i) in result.changes" :key="i" class="change-card">
          <div class="change-head">
            <n-tag size="small">{{ TYPE_LABEL[c.type] ?? c.type }}</n-tag>
            <n-tag :type="c.action === 'update' ? 'info' : 'success'" size="small">
              {{ c.action === 'update' ? '修改' : '新增' }}
            </n-tag>
          </div>
          <n-input
            v-model:value="dataTexts[i]"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
          />
        </div>

        <div class="actions">
          <n-button size="small" @click="emit('cancel')">取消</n-button>
          <n-button size="small" type="primary" :loading="applying" @click="apply">确认应用</n-button>
        </div>
      </template>
      <n-empty v-else-if="!loading" description="未解析出可应用的变更" />
    </n-spin>
  </div>
</template>

<style scoped>
.apply-card {
  margin-top: 8px;
  border: 1px solid #e5e5e5;
  border-radius: 6px;
  padding: 12px;
  background: #fafafa;
}
.reason {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.targets {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.target {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #666;
  font-size: 12px;
}
.change-card {
  margin-bottom: 10px;
}
.change-head {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
