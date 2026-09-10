import { createApp, h } from 'vue'
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import App from './App.vue'

// 根级注入 Naive UI Provider，保证 App 及其子组件可使用 useMessage / useDialog
createApp({
  render() {
    return h(NConfigProvider, null, {
      default: () =>
        h(NDialogProvider, null, {
          default: () => h(NMessageProvider, null, { default: () => h(App) }),
        }),
    })
  },
}).mount('#app')
