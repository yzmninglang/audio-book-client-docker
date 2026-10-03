/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 置为 "1" 时启用前端内置 mock(仅供开发/演示,生产构建默认关闭) */
  readonly VITE_USE_MOCK?: string
  /** 真实模式下 dev server 把 /api 代理到的后端地址 */
  readonly VITE_API_PROXY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
