<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { ApiError, api } from '@/api/client'
import type { FsBrowse } from '@/api/types'

/**
 * 服务端卷内路径浏览器(容器化约束 #8 / api/design.md §3)。
 *
 * ❌ 不使用浏览器本机文件目录选择器,不假设浏览器能访问本机文件系统。
 * ✅ 调用 GET /api/fs/browse?path=,在页面内渲染容器卷内的目录树与面包屑。
 */
const props = withDefaults(
  defineProps<{
    modelValue: string
    /** 初始/默认起始目录(为空则由后端返回默认卷根) */
    startPath?: string
    /** 选定后是否允许「选择此目录」 */
    selectable?: boolean
    title?: string
  }>(),
  {
    startPath: '',
    selectable: true,
    title: '选择容器内目录',
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'select', value: string): void
}>()

const current = ref('')
const data = ref<FsBrowse | null>(null)
const loading = ref(false)
const errorMsg = ref('')
const manualPath = ref('')

const breadcrumbs = computed(() => {
  const p = current.value || '/'
  const parts = p.split('/').filter(Boolean)
  const crumbs: { label: string; path: string }[] = [{ label: '/', path: '/' }]
  let acc = ''
  for (const part of parts) {
    acc += `/${part}`
    crumbs.push({ label: part, path: acc })
  }
  return crumbs
})

async function load(path: string) {
  loading.value = true
  errorMsg.value = ''
  try {
    const result = await api.browseFs(path || props.startPath || '')
    data.value = result
    current.value = result.path
    manualPath.value = result.path
    emit('update:modelValue', result.path)
  } catch (err) {
    const message = err instanceof ApiError ? err.message : String(err)
    errorMsg.value = message
    data.value = null
  } finally {
    loading.value = false
  }
}

function enter(name: string) {
  const base = current.value.replace(/\/$/, '')
  load(`${base}/${name}`)
}

function goUp() {
  if (data.value?.parent) load(data.value.parent)
}

function choose() {
  emit('select', current.value)
  emit('update:modelValue', current.value)
  ElMessage.success(`已选择:${current.value}`)
}

onMounted(() => load(props.modelValue || props.startPath))

watch(
  () => props.modelValue,
  (val) => {
    // 外部改动且与当前不同步时才重新加载(避免循环)
    if (val && val !== current.value) load(val)
  },
)
</script>

<template>
  <div class="path-browser">
    <div class="pb-head">
      <span class="pb-title">{{ title }}</span>
      <el-tag v-if="data?.is_project" type="success" size="small">工程目录</el-tag>
      <el-tag v-else type="info" size="small" effect="plain">普通目录</el-tag>
    </div>

    <div class="pb-bar">
      <el-button size="small" :disabled="!data?.parent" @click="goUp">上级</el-button>
      <el-breadcrumb separator="/" class="pb-crumbs">
        <el-breadcrumb-item v-for="c in breadcrumbs" :key="c.path">
          <a @click.prevent="load(c.path)">{{ c.label }}</a>
        </el-breadcrumb-item>
      </el-breadcrumb>
      <el-button size="small" :loading="loading" @click="load(current)">刷新</el-button>
    </div>

    <el-alert v-if="errorMsg" type="error" :closable="false" :title="errorMsg" show-icon />

    <div v-loading="loading" class="pb-list">
      <el-empty v-if="!loading && data && data.dirs.length === 0" description="此目录下没有子目录" :image-size="60" />
      <div
        v-for="dir in data?.dirs || []"
        :key="dir.name"
        class="pb-item"
        @click="enter(dir.name)"
      >
        <el-icon class="pb-icon"><Folder /></el-icon>
        <span class="pb-name">{{ dir.name }}</span>
        <el-icon v-if="dir.has_children" class="pb-arrow"><ArrowRight /></el-icon>
      </div>
    </div>

    <div class="pb-foot">
      <el-input
        v-model="manualPath"
        size="small"
        class="pb-manual"
        placeholder="也可直接输入容器内绝对路径,如 /workspace/三体"
        @keyup.enter="load(manualPath)"
      >
        <template #prepend>路径</template>
      </el-input>
      <el-button v-if="selectable" type="primary" size="small" :disabled="!data" @click="choose">
        选择此目录
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.path-browser {
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 12px;
}
.pb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-weight: 600;
}
.pb-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.pb-crumbs {
  flex: 1;
  overflow: hidden;
}
.pb-crumbs a {
  cursor: pointer;
  color: var(--el-color-primary);
}
.pb-list {
  min-height: 120px;
  max-height: 280px;
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 4px;
}
.pb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.pb-item:hover {
  background: var(--el-fill-color-light);
}
.pb-icon {
  color: var(--el-color-warning);
}
.pb-name {
  flex: 1;
}
.pb-arrow {
  color: var(--el-text-color-secondary);
}
.pb-foot {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.pb-manual {
  flex: 1;
}
</style>
