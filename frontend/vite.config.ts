import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyRes', (proxyRes, _request, response) => {
            // 上游中途断流时 pipe 不会结束下游，须让浏览器收到连接错误。
            const abortResponse = () => {
              if (!response.writableEnded && !response.destroyed) response.destroy()
            }
            proxyRes.once('aborted', abortResponse)
            proxyRes.once('error', abortResponse)
          })
        },
      },
    },
  },
})
