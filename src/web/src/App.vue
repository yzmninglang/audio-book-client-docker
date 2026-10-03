<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { isMockEnabled } from '@/api/client'

const route = useRoute()
const router = useRouter()

const mock = isMockEnabled()

const activeMenu = computed(() => {
  if (route.path.startsWith('/voices')) return '/voices'
  if (route.path.startsWith('/settings')) return '/settings'
  return '/projects'
})

function go(path: string) {
  router.push(path)
}
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <div class="brand" @click="go('/projects')">
        <el-icon :size="22"><Headset /></el-icon>
        <span>有声书合成客户端</span>
      </div>
      <el-menu :default-active="activeMenu" mode="horizontal" class="app-menu" :ellipsis="false" router>
        <el-menu-item index="/projects">工程</el-menu-item>
        <el-menu-item index="/voices">音色库</el-menu-item>
        <el-menu-item index="/settings">设置</el-menu-item>
      </el-menu>
      <div class="header-right">
        <el-tag v-if="mock" type="warning" effect="dark" size="small">MOCK 模式</el-tag>
        <el-tag v-else type="success" effect="plain" size="small">已连接后端</el-tag>
      </div>
    </el-header>

    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100%;
}
.app-header {
  display: flex;
  align-items: center;
  gap: 20px;
  border-bottom: 1px solid var(--el-border-color);
  background: var(--el-bg-color);
  padding: 0 24px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 17px;
  cursor: pointer;
  white-space: nowrap;
}
.app-menu {
  flex: 1;
  border-bottom: none;
}
.header-right {
  white-space: nowrap;
}
.app-main {
  background: var(--el-fill-color-blank);
  padding: 24px;
  overflow-y: auto;
}
</style>
