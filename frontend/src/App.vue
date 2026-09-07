<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NConfigProvider, NSpin, NTag } from 'naive-ui'

const loading = ref(true)
const ok = ref(false)
const message = ref('')

onMounted(async () => {
  try {
    const resp = await fetch('/api/v1/health')
    const data = await resp.json()
    ok.value = resp.ok
    message.value = resp.ok
      ? `后端连接成功：${data.app} v${data.version}`
      : `后端返回异常：HTTP ${resp.status}`
  } catch (e) {
    ok.value = false
    message.value = `无法连接后端：${(e as Error).message}`
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <NConfigProvider>
    <div class="container">
      <h1>灵墨 Lingmo</h1>
      <p class="subtitle">AI 网文协作写作工作台</p>
      <div class="health">
        <NSpin v-if="loading" size="small" />
        <NTag v-else :type="ok ? 'success' : 'error'" size="large">
          {{ message }}
        </NTag>
      </div>
    </div>
  </NConfigProvider>
</template>

<style scoped>
.container {
  max-width: 640px;
  margin: 80px auto;
  text-align: center;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
}
.subtitle {
  color: #888;
}
.health {
  margin-top: 24px;
}
</style>
