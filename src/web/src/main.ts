import { createPinia } from 'pinia'
import { createApp } from 'vue'

import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import { router } from './router'
import './styles.css'

async function bootstrap() {
  const app = createApp(App)

  app.use(createPinia())
  app.use(router)
  app.use(ElementPlus, { locale: zhCn })

  for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
  }

  // 开发用 mock:仅在显式设置 VITE_USE_MOCK=1 时装载。
  // 生产构建里该条件为假 → 动态 import 被 tree-shake,mock.ts 不进入产物。
  if (import.meta.env.VITE_USE_MOCK === '1') {
    const { installMock } = await import('./api/mock')
    installMock()
  }

  app.mount('#app')
}

void bootstrap()
