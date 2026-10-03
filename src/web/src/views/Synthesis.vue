<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import ProgressPanel from '@/components/ProgressPanel.vue'
import { ApiError } from '@/api/client'
import type { StaleChapter } from '@/api/types'
import { useJobsStore } from '@/stores/jobs'
import { useProjectsStore } from '@/stores/projects'

const props = defineProps<{ name: string }>()

const router = useRouter()
const jobs = useJobsStore()
const projects = useProjectsStore()

const { status, logs, connected, reconnecting, chapters, doneFragments, totalFragments, etaLabel } =
  storeToRefs(jobs)

const stateLabel = computed(
  () =>
    ({ idle: '空闲', running: '运行中', paused: '已暂停', stopped: '已停止' })[status.value.state] ||
    status.value.state,
)

const unboundCount = computed(() => projects.unboundRoles.length)
const invalidCount = computed(() => projects.invalidRoles.length)

/** 绑定变更后「片段基于旧音色」的章节;旧版后端不返回该字段时按空数组容错 */
const staleChapters = computed<StaleChapter[]>(() => projects.current?.stale_chapters || [])
const staleDescription = computed(() =>
  staleChapters.value.map((c) => `${c.name}(${c.stale_fragments.length} 条)`).join('、'),
)

const chapterStatusText: Record<string, string> = {
  done: '已完成',
  partial: '部分完成',
  pending: '待生成',
}
function chapterStatusLabel(status: string): string {
  return chapterStatusText[status] || status
}

onMounted(async () => {
  try {
    await projects.open(props.name)
  } catch {
    /* 详情失败不阻塞进度 */
  }
  await jobs.connect()
})

onUnmounted(() => {
  jobs.disconnect()
})

async function startAll() {
  const problems: string[] = []
  if (unboundCount.value) problems.push(`未绑定角色 ${unboundCount.value} 个`)
  if (invalidCount.value) problems.push(`绑定失效 ${invalidCount.value} 个`)
  if (problems.length) {
    try {
      await ElMessageBox.confirm(
        `${problems.join('、')}。后端会在开跑前报错并阻塞,建议先回绑定页处理。仍要开始?`,
        '存在未完成的绑定',
        { type: 'warning', confirmButtonText: '仍然开始', cancelButtonText: '去绑定' },
      )
    } catch {
      router.push(`/projects/${encodeURIComponent(props.name)}`)
      return
    }
  }
  try {
    await jobs.start({ project: props.name })
    ElMessage.success('已开始合成(只补未完成部分)')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function startChapter(chapter: string) {
  try {
    await jobs.start({ project: props.name, chapters: [chapter] })
    ElMessage.success(`已开始章节:${chapter}`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function redoStale() {
  if (staleChapters.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `将作废旧成品与片段并重新生成:${staleDescription.value}。确定重做?`,
      '重做受影响章节',
      { type: 'warning', confirmButtonText: '重做', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await jobs.start({ project: props.name, chapters: staleChapters.value.map((c) => c.name), redo: true })
    ElMessage.success('已提交重做受影响章节')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function removeChapter(chapter: string) {
  try {
    await ElMessageBox.confirm(
      `将删除章节《${chapter}》的 JSON 与中间片段。该章已生成的音频会移入 audio/.stale/,不会立即丢失。`,
      '删除章节',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  try {
    await projects.deleteChapter(props.name, chapter)
    ElMessage.success(`已删除章节:${chapter}`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function pause() {
  try {
    await jobs.pause()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function stop() {
  try {
    await jobs.stop()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <el-button text @click="router.push(`/projects/${encodeURIComponent(props.name)}`)">
        <el-icon><ArrowLeft /></el-icon>&nbsp;角色绑定
      </el-button>
      <h2 class="page-title">{{ props.name }} · 合成进度</h2>
      <span class="spacer" />
      <el-tag v-if="connected" type="success" effect="plain" size="small">事件流已连接</el-tag>
      <el-tag v-else-if="reconnecting" type="warning" effect="plain" size="small">重连中…</el-tag>
      <el-tag v-else type="info" effect="plain" size="small">未连接</el-tag>
      <el-button size="small" @click="jobs.refreshState()">刷新状态</el-button>
    </div>

    <el-alert
      v-if="projects.current && (unboundCount || invalidCount)"
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      :title="`绑定未完成:未绑定 ${unboundCount} 个,失效 ${invalidCount} 个`"
      description="后端会在开跑前阻塞;请先到角色绑定页完成绑定。"
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
            :disabled="status.state === 'running'"
            @click="redoStale"
          >
            重做受影响章节
          </el-button>
          <span class="muted">仅提示,不会自动重做</span>
        </div>
      </template>
    </el-alert>

    <div class="toolbar">
      <el-button
        type="primary"
        :disabled="status.state === 'running'"
        @click="startAll"
      >
        <el-icon><VideoPlay /></el-icon>&nbsp;全部开始
      </el-button>
      <el-button :disabled="status.state !== 'running'" @click="pause">
        <el-icon><VideoPause /></el-icon>&nbsp;暂停
      </el-button>
      <el-button :disabled="status.state === 'idle' || status.state === 'stopped'" @click="stop">
        <el-icon><SwitchButton /></el-icon>&nbsp;停止
      </el-button>
    </div>

    <el-card shadow="never" class="mb">
      <ProgressPanel
        :chapters="chapters"
        :logs="logs"
        :done-fragments="doneFragments"
        :total-fragments="totalFragments"
        :current-chapter="status.current_chapter"
        :eta-label="etaLabel"
        :state-label="stateLabel"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>章节清单</template>
      <el-table :data="projects.current?.chapters || []" size="small" empty-text="无章节">
        <el-table-column prop="name" label="章节" min-width="160" />
        <el-table-column prop="sentence_count" label="句数" width="90" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'done' ? 'success' : row.status === 'partial' ? 'danger' : 'info'"
            >
              {{ chapterStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="output" label="成品路径" min-width="180">
          <template #default="{ row }">{{ row.output || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button
              size="small"
              text
              type="primary"
              :disabled="status.state === 'running'"
              @click="startChapter(row.name)"
            >
              {{ row.status === 'done' ? '重做' : '开始此章' }}
            </el-button>
            <el-tooltip :disabled="jobs.runningChapter !== row.name" content="该章正在合成,无法删除">
              <span>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :disabled="jobs.runningChapter === row.name"
                  @click="removeChapter(row.name)"
                >
                  删除
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.stale-body {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
