<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { UploadUserFile } from 'element-plus'
import { ElMessage, ElMessageBox } from 'element-plus'

import PathBrowser from '@/components/PathBrowser.vue'
import { ApiError, CHAPTER_FILE_LIMITS, validateChapterFiles } from '@/api/client'
import { useJobsStore } from '@/stores/jobs'
import { useProjectsStore } from '@/stores/projects'
import { useSettingsStore } from '@/stores/settings'

const router = useRouter()
const projects = useProjectsStore()
const settings = useSettingsStore()
const jobs = useJobsStore()

/** 正在合成的工程名;用于提前禁用删除入口(服务端也会 409) */
const runningProject = computed(() => jobs.runningProject)

const dialogVisible = ref(false)
const creating = ref(false)
const mode = ref('path')
const form = reactive({ name: '', source_path: '' })
const uploadFileList = ref<UploadUserFile[]>([])

onMounted(async () => {
  try {
    await settings.load()
    if (!form.source_path) form.source_path = settings.settings?.workspace_root || '/workspace'
  } catch {
    /* 设置失败不阻塞工程列表 */
  }
  await projects.loadList()
  // 取一次任务状态,据此禁用「正在合成」工程的删除入口
  await jobs.refreshState().catch(() => undefined)
})

async function removeProject(name: string) {
  try {
    await ElMessageBox.confirm(
      `将删除工程《${name}》。其目录会移入回收站(.trash/),不会立即丢失,但需手动清理。`,
      '删除工程',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    const result = await projects.deleteProject(name)
    ElMessage.success(`已删除《${name}》,移入 ${result.trashed_to}`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

function openCreate() {
  form.name = ''
  form.source_path = settings.settings?.workspace_root || '/workspace'
  mode.value = 'path'
  uploadFileList.value = []
  dialogVisible.value = true
}

function rawFiles(): File[] {
  const out: File[] = []
  for (const f of uploadFileList.value) if (f.raw) out.push(f.raw)
  return out
}

async function submitCreate() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写工程名(书名)')
    return
  }

  if (mode.value === 'upload') {
    const files = rawFiles()
    const invalid = validateChapterFiles(files)
    if (invalid) {
      ElMessage.warning(invalid)
      return
    }
    creating.value = true
    try {
      const detail = await projects.uploadCreate({ name: form.name.trim(), files })
      dialogVisible.value = false
      ElMessage.success(`工程「${detail.name}」已创建(上传 ${files.length} 个章节)`)
      router.push(`/projects/${encodeURIComponent(detail.name)}`)
    } catch (err) {
      ElMessage.error(err instanceof ApiError ? err.message : String(err))
    } finally {
      creating.value = false
    }
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

function mb(bytes: number) {
  return Math.round((bytes / (1024 * 1024)) * 10) / 10
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
          <el-tooltip :disabled="runningProject !== p.name" content="正在合成,无法删除">
            <span>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="runningProject === p.name"
                @click="removeProject(p.name)"
              >
                删除
              </el-button>
            </span>
          </el-tooltip>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" title="新建工程" width="680px">
      <el-form label-width="100px">
        <el-form-item label="工程名" required>
          <el-input v-model="form.name" placeholder="书名,如:三体" />
        </el-form-item>

        <el-tabs v-model="mode">
          <el-tab-pane label="从服务器路径导入" name="path">
            <PathBrowser
              v-model="form.source_path"
              :start-path="settings.settings?.workspace_root || '/workspace'"
              :selectable="false"
              title="选择含章节 JSON 的容器内目录"
            />
          </el-tab-pane>

          <el-tab-pane label="上传 JSON 文件" name="upload">
            <el-upload
              v-model:file-list="uploadFileList"
              :auto-upload="false"
              multiple
              accept=".json,application/json"
              drag
            >
              <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
              <div class="el-upload__text">把章节 JSON 拖到这里,或<em>点击选择</em></div>
              <template #tip>
                <div class="muted">
                  可一次选多个;只接受 .json。单文件 ≤ {{ mb(CHAPTER_FILE_LIMITS.maxFileBytes) }} MB、
                  单次 ≤ {{ CHAPTER_FILE_LIMITS.maxCount }} 个、总计 ≤ {{ mb(CHAPTER_FILE_LIMITS.maxTotalBytes) }} MB。
                </div>
              </template>
            </el-upload>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">
          {{ mode === 'upload' ? '上传并创建' : '创建' }}
        </el-button>
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
