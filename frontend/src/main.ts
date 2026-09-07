import { createApp, h } from 'vue'
import { NConfigProvider, NMessageProvider } from 'naive-ui'
import App from './App.vue'

// 根级注入 Naive UI Provider，保证 App 及其子组件可使用 useMessage
createApp({
  render() {
    return h(NConfigProvider, null, {
      default: () => h(NMessageProvider, null, { default: () => h(App) }),
    })
  },
}).mount('#app')
