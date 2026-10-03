<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import type { UploadUserFile } from 'element-plus'

import VoiceSelect from '@/components/VoiceSelect.vue'
import { ApiError, CHAPTER_FILE_LIMITS, validateChapterFiles } from '@/api/client'
import type { BindingsFile, RoleStatus, StaleChapter } from '@/api/types'
import { useProjectsStore } from '@/stores/projects'
import { useSettingsStore } from '@/stores/settings'
import { useVoicesStore } from '@/stores/voices'

const props = defineProps<{ name: string }>()

const router = useRouter()
const projects = useProjectsStore()
const voices = useVoicesStore()
const settings = useSettingsStore()

interface Row {
  name: string
  voice: string | null
  emo_ref: string | null
  /** 服务端判定的状态(保存前),用于失效高亮 */
  serverStatus: RoleStatus
  serverVoice: string | null
}

const rows = ref<Row[]>([])
const saving = ref(false)
const loading = ref(false)

// 追加章节(需求扩展 §8.9)
const appendVisible = ref(false)
const appending = ref(false)
const appendFileList = ref<UploadUserFile[]>([])

const unboundCount = computed(() => rows.value.filter((r) => !r.voice).length)
/** 绑定指向的音色已被删除(且尚未改绑) */
const invalidRows = computed(() =>
  rows.value.filter((r) => r.voice && !voices.exists(r.voice)),
)
const danglingNames = computed(() => invalidRows.value.map((r) => r.voice as string))
const okCount = computed(() => rows.value.filter((r) => r.voice && voices.exists(r.voice)).length)

/** 绑定变更后「片段基于旧音色」的章节;旧版后端不返回该字段时按空数组容错 */
const staleChapters = computed<StaleChapter[]>(() => projects.current?.stale_chapters || [])
const staleDescription = computed(() =>
  staleChapters.value.map((c) => `${c.name}(${c.stale_fragments.length} 条)`).join('、'),
)

const dirty = computed(() => {
  const cur = projects.current
  if (!cur) return false
  const saved = cur.bindings.bindings || {}
  return rows.value.some((r) => (saved[r.name]?.voice || null) !== (r.voice || null))
})

/** provider 不支持情感向量时显式提示(不静默忽略 chapter JSON 的 emo_vector) */
const vectorUnsupported = computed(() => {
  const caps = settings.activeProviderInfo?.capabilities
  if (!caps) return false
  return Array.isArray(caps.emotion) && !caps.emotion.includes('vector')
})

async function reload() {
  loading.value = true
  try {
    await Promise.all([projects.open(props.name), voices.load(), settings.load().catch(() => undefined)])
    rebuildRows()
  } finally {
    loading.value = false
  }
}

function rebuildRows() {
  const cur = projects.current
  if (!cur) {
    rows.value = []
    return
  }
  rows.value = cur.roles.map((role) => ({
    name: role.name,
    voice: role.voice,
    emo_ref: role.emo_ref ?? null,
    serverStatus: role.status,
    serverVoice: role.voice,
  }))
}

watch(() => props.name, reload)
watch(() => projects.current, rebuildRows)

onMounted(reload)

/** 按同名推荐:角色名与音色名完全相同则自动绑定 */
function applySameNameRecommendation() {
  let n = 0
  for (const r of rows.value) {
    if (!r.voice && voices.exists(r.name)) {
      r.voice = r.name
      n += 1
    }
  }
  ElMessage[n ? 'success' : 'info'](n ? `已按同名推荐绑定 ${n} 个角色` : '没有可同名匹配的角色')
}

function buildBindings(): BindingsFile {
  const bindings: BindingsFile['bindings'] = {}
  for (const r of rows.value) {
    if (r.voice) bindings[r.name] = { voice: r.voice, emo_ref: r.emo_ref }
  }
  return { version: 1, bindings }
}

async function save(): Promise<boolean> {
  saving.value = true
  try {
    await projects.saveBindings(buildBindings())
    rebuildRows()
    ElMessage.success('绑定已保存')
    return true
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
    return false
  } finally {
    saving.value = false
  }
}

async function startSynthesis() {
  if (dirty.value) {
    const ok = await save()
    if (!ok) return
  }
  const problems: string[] = []
  if (unboundCount.value > 0) problems.push(`未绑定角色 ${unboundCount.value} 个`)
  if (invalidRows.value.length > 0) problems.push(`绑定失效 ${invalidRows.value.length} 个`)

  if (problems.length > 0) {
    try {
      await ElMessageBox.confirm(
        `${problems.join('、')}。后端会在开跑前阻塞,请先完成绑定。仍要前往合成页?`,
        '绑定未完成',
        { type: 'warning', confirmButtonText: '仍然前往', cancelButtonText: '返回绑定' },
      )
    } catch {
      return
    }
  }
  router.push(`/projects/${encodeURIComponent(props.name)}/synthesis`)
}

function openAppend() {
  appendFileList.value = []
  appendVisible.value = true
}

function appendRawFiles(): File[] {
  const out: File[] = []
  for (const f of appendFileList.value) if (f.raw) out.push(f.raw)
  return out
}

async function submitAppend() {
  const files = appendRawFiles()
  const invalid = validateChapterFiles(files)
  if (invalid) {
    ElMessage.warning(invalid)
    return
  }
  appending.value = true
  try {
    await projects.appendChapters(props.name, files)
    appendVisible.value = false
    ElMessage.success(`已追加/替换 ${files.length} 个章节,章节列表已刷新`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    appending.value = false
  }
}

function mb(bytes: number) {
  return Math.round((bytes / (1024 * 1024)) * 10) / 10
}

function statusTag(row: Row) {
  if (!row.voice) return { type: 'warning' as const, text: '需绑定' }
  if (!voices.exists(row.voice)) return { type: 'danger' as const, text: '已失效' }
  return { type: 'success' as const, text: '已绑定' }
}
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="toolbar">
      <el-button text @click="router.push('/projects')">
        <el-icon><ArrowLeft /></el-icon>&nbsp;工程列表
      </el-button>
      <h2 class="page-title">{{ props.name }} · 角色绑定</h2>
      <span class="spacer" />
      <el-button @click="openAppend">
        <el-icon><Upload /></el-icon>&nbsp;追加章节
      </el-button>
      <el-button @click="applySameNameRecommendation">同名推荐</el-button>
      <el-button :loading="saving" @click="save">保存绑定</el-button>
      <el-button type="primary" @click="startSynthesis">开始合成</el-button>
    </div>

    <el-alert
      v-if="unboundCount > 0"
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      :title="`有 ${unboundCount} 个角色未绑定音色,开始合成前必须全部绑定`"
    />
    <el-alert
      v-if="invalidRows.length > 0"
      type="error"
      show-icon
      :closable="false"
      class="mb"
      title="存在失效绑定"
      :description="`以下角色绑定的音色已被删除,请重新选择:${invalidRows.map((r) => r.name).join('、')}`"
    />
    <el-alert
      v-if="vectorUnsupported"
      type="info"
      show-icon
      :closable="false"
      class="mb"
      title="当前后端不支持情感向量"
      description="章节 JSON 中的 emo_vector 将被忽略(显式告警,不静默降质)。"
    />

    <el-alert
      v-if="staleChapters.length > 0"
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      :title="`${staleChapters.length} 个章节的片段基于旧绑定生成,建议重做`"
    >
      <template #default>
        <div class="stale-body">
          <span>受影响章节:{{ staleDescription }}</span>
          <el-button
            size="small"
            type="warning"
            plain
            @click="router.push(`/projects/${encodeURIComponent(props.name)}/synthesis`)"
          >
            前往合成页重做
          </el-button>
          <span class="muted">仅提示,不会自动重做</span>
        </div>
      </template>
    </el-alert>

    <div class="summary muted">
      共 {{ rows.length }} 个角色 · 已绑定 {{ okCount }} · 未绑定 {{ unboundCount }} ·
      失效 {{ invalidRows.length }}
    </div>

    <el-table :data="rows" class="bind-table" row-key="name">
      <el-table-column prop="name" label="角色(来自 JSON 聚合)" min-width="180">
        <template #default="{ row }">
          <span class="role-name">{{ row.name }}</span>
        </template>
      </el-table-column>

      <el-table-column label="绑定音色(可搜索)" min-width="280">
        <template #default="{ row }">
          <VoiceSelect
            v-model="row.voice"
            :voices="voices.voices"
            :invalid-names="danglingNames"
            :placeholder="'搜索音色,如 汪淼 / 男旁白2'"
          />
        </template>
      </el-table-column>

      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusTag(row).type" effect="light">{{ statusTag(row).text }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="情感参考(可选)" min-width="160">
        <template #default="{ row }">
          <el-select
            v-model="row.emo_ref"
            clearable
            placeholder="默认"
            :disabled="!row.voice || !voices.byName(row.voice)?.emo_refs || Object.keys(voices.byName(row.voice)?.emo_refs || {}).length === 0"
            size="default"
          >
            <el-option
              v-for="(_, key) in (row.voice ? voices.byName(row.voice)?.emo_refs : {}) || {}"
              :key="key"
              :label="key"
              :value="key"
            />
          </el-select>
        </template>
      </el-table-column>
    </el-table>

    <!-- 追加章节 -->
    <el-dialog v-model="appendVisible" title="追加章节 JSON" width="620px">
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        class="mb"
        title="同名章节将被替换"
        description="若文件名已存在,该章 JSON 会被覆盖,其已生成的音频将失效并需要重做(后端按 json_sha256 变化自动作废旧片段)。"
      />
      <el-upload
        v-model:file-list="appendFileList"
        :auto-upload="false"
        multiple
        accept=".json,application/json"
        drag
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">把章节 JSON 拖到这里,或<em>点击选择</em></div>
        <template #tip>
          <div class="muted">
            只接受 .json。单文件 ≤ {{ mb(CHAPTER_FILE_LIMITS.maxFileBytes) }} MB、
            单次 ≤ {{ CHAPTER_FILE_LIMITS.maxCount }} 个、总计 ≤ {{ mb(CHAPTER_FILE_LIMITS.maxTotalBytes) }} MB。
          </div>
        </template>
      </el-upload>
      <template #footer>
        <el-button @click="appendVisible = false">取消</el-button>
        <el-button type="primary" :loading="appending" @click="submitAppend">上传</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.summary {
  margin-bottom: 10px;
}
.stale-body {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.bind-table {
  width: 100%;
}
.role-name {
  font-weight: 600;
}
</style>
