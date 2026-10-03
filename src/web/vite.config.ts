import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // 生产默认关闭 mock(容器化约束:mock 不得进入生产构建)。
  // 仅当显式设置 VITE_USE_MOCK=1 时才启用(开发/演示用)。
  const useMock = env.VITE_USE_MOCK === '1'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      // mock 模式下不需要后端代理;真实模式下把 /api 转给 FastAPI。
      proxy: useMock
        ? undefined
        : {
            '/api': {
              target: env.VITE_API_PROXY || 'http://localhost:8000',
              changeOrigin: true,
            },
          },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
  }
})
