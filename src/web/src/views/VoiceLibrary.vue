<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import type { UploadFile } from 'element-plus'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import type { VoiceEntry } from '@/api/types'
import { useVoicesStore } from '@/stores/voices'

const voices = useVoicesStore()

const createVisible = ref(false)
const editVisible = ref(false)
const creating = ref(false)
const saving = ref(false)
const fileList = ref<UploadFile[]>([])

const createForm = reactive<{ name: string; prompt_text: string; file: File | null }>({
  name: '',
  prompt_text: '',
  file: null,
})

const editForm = reactive<{ name: string; rename: string; prompt_text: string; file: File | null }>({
  name: '',
  rename: '',
  prompt_text: '',
  file: null,
})

const previewVisible = ref(false)
const previewSrc = ref('')
const previewName = ref('')

onMounted(() => voices.load())

/** el-upload 内部的 file 列表变化时,取原始 File */
function handleCreateUploadChange(file: UploadFile) {
  createForm.file = (file.raw as File) || null
}
function handleEditUploadChange(file: UploadFile) {
  editForm.file = (file.raw as File) || null
}

function openCreate() {
  createForm.name = ''
  createForm.prompt_text = ''
  createForm.file = null
  fileList.value = []
  createVisible.value = true
}

async function submitCreate() {
  if (!createForm.name.trim()) {
    ElMessage.warning('请填写音色名(中文)')
    return
  }
  if (!createForm.file) {
    ElMessage.warning('请上传参考音频')
    return
  }
  creating.value = true
  try {
    await voices.create({
      name: createForm.name.trim(),
      prompt_text: createForm.prompt_text || null,
      file: createForm.file,
    })
    ElMessage.success('音色已创建并入库标准化')
    createVisible.value = false
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    creating.value = false
  }
}

function openEdit(voice: VoiceEntry) {
  editForm.name = voice.name
  editForm.rename = voice.name
  editForm.prompt_text = voice.prompt_text || ''
  editForm.file = null
  fileList.value = []
  editVisible.value = true
}

async function submitEdit() {
  if (!editForm.rename.trim()) {
    ElMessage.warning('音色名不能为空')
    return
  }
  saving.value = true
  try {
    await voices.update(editForm.name, {
      prompt_text: editForm.prompt_text,
      file: editForm.file,
      rename: editForm.rename.trim() !== editForm.name ? editForm.rename.trim() : null,
    })
    ElMessage.success('音色已更新')
    editVisible.value = false
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    saving.value = false
  }
}

async function remove(voice: VoiceEntry) {
  try {
    await ElMessageBox.confirm(
      `确定删除音色「${voice.name}」?正在引用它的工程绑定将变为「失效」。`,
      '删除音色',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await voices.remove(voice.name)
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function preview(voice: VoiceEntry) {
  try {
    if (previewSrc.value) URL.revokeObjectURL(previewSrc.value)
    previewSrc.value = await voices.previewUrl(voice.name)
    previewName.value = voice.name
    previewVisible.value = true
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

function closePreview() {
  if (previewSrc.value) {
    URL.revokeObjectURL(previewSrc.value)
    previewSrc.value = ''
  }
}

function emoKeys(voice: VoiceEntry): string[] {
  return Object.keys(voice.emo_refs || {})
}
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2 class="page-title">音色库</h2>
      <el-tag type="info" effect="plain">共 {{ voices.voices.length }} 个音色</el-tag>
      <span class="spacer" />
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新建音色
      </el-button>
      <el-button :loading="voices.loading" @click="voices.load()">刷新</el-button>
    </div>

    <el-table v-loading="voices.loading" :data="voices.voices" row-key="name" class="voice-table">
      <el-table-column prop="name" label="音色名" min-width="140">
        <template #default="{ row }">
          <span class="voice-name">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="prompt_text" label="参考文本" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.prompt_text || '—' }}</template>
      </el-table-column>
      <el-table-column label="情感参考集" min-width="200">
        <template #default="{ row }">
          <template v-if="emoKeys(row).length">
            <el-tag v-for="k in emoKeys(row)" :key="k" size="small" class="emo-tag" effect="plain">
              {{ k }}
            </el-tag>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <el-tag v-if="row.is_normalized" size="small" type="success" effect="plain">已标准化</el-tag>
          <el-tag v-else size="small" type="warning" effect="plain">未标准化</el-tag>
          <el-tooltip v-if="row.warnings?.length" :content="row.warnings.join(';')">
            <el-tag size="small" type="warning" effect="dark" class="warn-tag">提示</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="preview(row)">试听</el-button>
          <el-button size="small" text type="primary" @click="openEdit(row)">编辑/重命名</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建 -->
    <el-dialog v-model="createVisible" title="新建音色" width="520px">
      <el-form label-width="90px">
        <el-form-item label="音色名" required>
          <el-input v-model="createForm.name" placeholder="中文音色名,如:丁仪" />
        </el-form-item>
        <el-form-item label="参考文本">
          <el-input v-model="createForm.prompt_text" type="textarea" :rows="3" placeholder="参考音频对应的文本(可选)" />
        </el-form-item>
        <el-form-item label="参考音频" required>
          <el-upload
            :auto-upload="false"
            :limit="1"
            accept="audio/*"
            :on-change="handleCreateUploadChange"
            v-model:file-list="fileList"
          >
            <el-button>
              <el-icon><Upload /></el-icon>&nbsp;选择音频文件
            </el-button>
            <template #tip>
              <div class="muted">将入库转码为 mono + 只降不升(≤22050)+ 裁剪 ≤15s</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 / 重命名 -->
    <el-dialog v-model="editVisible" title="编辑音色" width="520px">
      <el-form label-width="90px">
        <el-form-item label="音色名" required>
          <el-input v-model="editForm.rename" />
        </el-form-item>
        <el-form-item label="参考文本">
          <el-input v-model="editForm.prompt_text" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="替换音频">
          <el-upload
            :auto-upload="false"
            :limit="1"
            accept="audio/*"
            :on-change="handleEditUploadChange"
            v-model:file-list="fileList"
          >
            <el-button>
              <el-icon><Upload /></el-icon>&nbsp;选择新音频(可选)
            </el-button>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 试听 -->
    <el-dialog v-model="previewVisible" :title="`试听:${previewName}`" width="480px" @closed="closePreview">
      <audio v-if="previewSrc" :src="previewSrc" controls autoplay style="width: 100%" />
    </el-dialog>
  </div>
</template>

<style scoped>
.voice-table {
  width: 100%;
}
.voice-name {
  font-weight: 600;
}
.emo-tag {
  margin: 0 4px 4px 0;
}
.warn-tag {
  margin-left: 6px;
}
</style>
