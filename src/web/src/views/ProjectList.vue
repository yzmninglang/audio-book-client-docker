<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import PathBrowser from '@/components/PathBrowser.vue'
import { ApiError } from '@/api/client'
import { useProjectsStore } from '@/stores/projects'
import { useSettingsStore } from '@/stores/settings'

const router = useRouter()
const projects = useProjectsStore()
const settings = useSettingsStore()

const dialogVisible = ref(false)
const creating = ref(false)
const form = reactive({ name: '', source_path: '' })

onMounted(async () => {
  try {
    await settings.load()
    if (!form.source_path) form.source_path = settings.settings?.workspace_root || '/workspace'
  } catch {
    /* 设置失败不阻塞工程列表 */
  }
  await projects.loadList()
})

function openCreate() {
  form.name = ''
  form.source_path = settings.settings?.workspace_root || '/workspace'
  dialogVisible.value = true
}

async function submitCreate() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写工程名(书名)')
    return
  }
  if (!form.source_path) {
    ElMessage.warning('请选择 JSON 源目录')
    return
  }
  creating.value = true
  try {
    const detail = await projects.create({ name: form.name.trim(), source_path: form.source_path })
    dialogVisible.value = false
    ElMessage.success(`工程「${detail.name}」已创建`)
    router.push(`/projects/${encodeURIComponent(detail.name)}`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    creating.value = false
  }
}

function openProject(name: string) {
  router.push(`/projects/${encodeURIComponent(name)}`)
}

function goSynthesis(name: string) {
  router.push(`/projects/${encodeURIComponent(name)}/synthesis`)
}

function percent(p?: number) {
  return Math.round((p || 0) * 100)
}
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="page-title">工程列表</h2>
      <span class="spacer" />
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新建工程
      </el-button>
      <el-button :loading="projects.loadingList" @click="projects.loadList()">刷新</el-button>
    </div>

    <el-empty
      v-if="!projects.loadingList && projects.list.length === 0"
      description="工作区内暂无工程,点击「新建工程」导入 JSON"
    />

    <div class="grid">
      <el-card v-for="p in projects.list" :key="p.name" shadow="hover" class="card">
        <div class="card-head">
          <el-icon :size="20"><Notebook /></el-icon>
          <span class="card-title">{{ p.name }}</span>
          <el-tag v-if="p.has_manifest" size="small" type="info" effect="plain">有台账</el-tag>
        </div>
        <div class="card-path muted">{{ p.path }}</div>
        <div class="card-meta">
          <span>章节 {{ p.chapter_count }}</span>
          <span>句数 {{ p.sentence_count ?? '-' }}</span>
          <span>已完成 {{ p.done_count ?? 0 }}/{{ p.chapter_count }}</span>
        </div>
        <el-progress :percentage="percent(p.progress)" :stroke-width="10" />
        <div class="card-actions">
          <el-button size="small" type="primary" @click="openProject(p.name)">打开(角色绑定)</el-button>
          <el-button size="small" @click="goSynthesis(p.name)">合成进度</el-button>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" title="新建工程" width="640px">
      <el-form label-width="90px">
        <el-form-item label="工程名" required>
          <el-input v-model="form.name" placeholder="书名,如:三体" />
        </el-form-item>
        <el-form-item label="JSON 源目录" required>
          <PathBrowser
            v-model="form.source_path"
            :start-path="settings.settings?.workspace_root || '/workspace'"
            :selectable="false"
            title="选择含章节 JSON 的容器内目录"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.card-title {
  font-weight: 700;
  font-size: 16px;
  flex: 1;
}
.card-path {
  font-size: 12px;
  margin-bottom: 10px;
  word-break: break-all;
}
.card-meta {
  display: flex;
  gap: 14px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.card-actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}
</style>
