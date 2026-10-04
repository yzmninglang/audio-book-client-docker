<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import ProgressPanel from '@/components/ProgressPanel.vue'
import { ApiError } from '@/api/client'
import type { ChapterProgress, StaleChapter } from '@/api/types'
import { useJobsStore } from '@/stores/jobs'
import { useProjectsStore } from '@/stores/projects'

const props = defineProps<{ name: string }>()

const router = useRouter()
const jobs = useJobsStore()
const projects = useProjectsStore()

// 全局队列状态(仅用于「全局队列」那一块,不用于本工程进度)
const { status, logs, connected, reconnecting } = storeToRefs(jobs)

const stateLabel = computed(
  () =>
    ({ idle: '空闲', running: '运行中', paused: '已暂停', stopped: '已停止' })[status.value.state] ||
    status.value.state,
)

const unboundCount = computed(() => projects.unboundRoles.length)
const invalidCount = computed(() => projects.invalidRoles.length)

// ---- 本工程进度:一律来自工程自身数据(修复「看到的是别的工程的进度」) ----
const thisProjectRunning = computed(() => jobs.status.project === props.name)

const displayedChapters = computed<ChapterProgress[]>(() => {
  const base: ChapterProgress[] = (projects.current?.chapters || []).map((c) => ({
    name: c.name,
    total: c.sentence_count,
    done: c.done_fragments ?? 0,
    failed: c.failed_fragments ?? 0,
    status: c.status,
  }))
  // 仅当「正在执行的正是本工程」时叠加实时进度。
  // 契约(§8.6)保证全局队列里的 chapters 属于正在执行的那个工程,所以此叠加是安全的。
  if (thisProjectRunning.value) {
    return base.map((b) => {
      const live = jobs.chapters.find((x) => x.name === b.name)
      return live ? { ...b, done: live.done, failed: live.failed, status: live.status } : b
    })
  }
  return base
})

const projectTotal = computed(() => displayedChapters.value.reduce((s, c) => s + c.total, 0))
const projectDone = computed(() => displayedChapters.value.reduce((s, c) => s + c.done, 0))
const liveCurrentChapter = computed(() => (thisProjectRunning.value ? jobs.runningChapter : null))
const liveEta = computed(() => (thisProjectRunning.value ? jobs.etaLabel : ''))

// ---- 全局队列 ----
const queueRunning = computed(() => jobs.status.project)
const queueChapter = computed(() => jobs.runningChapter)
const queuePending = computed(() => jobs.pending)

/** 绑定变更后「片段基于旧音色」的章节;旧版后端不返回该字段时按空数组容错 */
const staleChapters = computed<StaleChapter[]>(() => projects.current?.stale_chapters || [])
const staleDescription = computed(() =>
  staleChapters.value.map((c) => `${c.name}(${c.stale_fragments.length} 条)`).join('、'),
)

function refreshProject() {
  return projects.open(props.name).catch(() => undefined)
}

onMounted(async () => {
  await refreshProject()
  await jobs.connect()
})

onUnmounted(() => {
  jobs.disconnect()
})

// 任务离开运行时刷新一次本工程详情(补上最终章节状态)
watch(
  () => jobs.state,
  (nv) => {
    if (nv !== 'running') refreshProject()
  },
)

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
  const wasRunning = jobs.state === 'running'
  try {
    await jobs.start({ project: props.name })
    ElMessage.success(wasRunning ? '已加入队列(排在当前任务之后)' : '已开始合成(只补未完成部分)')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function startChapter(chapter: string) {
  const wasRunning = jobs.state === 'running'
  try {
    await jobs.start({ project: props.name, chapters: [chapter] })
    ElMessage.success(wasRunning ? `已加入队列:${chapter}` : `已开始章节:${chapter}`)
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
          <el-button size="small" type="warning" plain @click="redoStale">重做受影响章节</el-button>
          <span class="muted">仅提示,不会自动重做</span>
        </div>
      </template>
    </el-alert>

    <!-- 全局队列(所有工程共享);提交按钮不再因运行中而禁用 —— 支持排队 -->
    <el-card shadow="never" class="mb">
      <template #header>全局队列(所有工程共享,串行执行)</template>
      <div class="queue-row">
        <span class="queue-label">正在运行</span>
        <template v-if="queueRunning">
          <el-tag type="primary" effect="dark">{{ queueRunning }}</el-tag>
          <span v-if="queueChapter" class="muted">· {{ queueChapter }}</span>
          <el-tag v-if="thisProjectRunning" size="small" type="success" effect="plain">本工程</el-tag>
        </template>
        <span v-else class="muted">空闲</span>
      </div>
      <div class="queue-row">
        <span class="queue-label">排队中</span>
        <template v-if="queuePending.length">
          <el-tag v-for="(p, i) in queuePending" :key="p.project + i" type="info" effect="plain" class="queue-chip">
            {{ i + 1 }}. {{ p.project }}({{ p.chapters }} 章)
          </el-tag>
        </template>
        <span v-else class="muted">无</span>
      </div>
      <div class="queue-controls">
        <el-button type="primary" @click="startAll">
          <el-icon><VideoPlay /></el-icon>&nbsp;{{ status.state === 'running' ? '加入队列' : '全部开始' }}
        </el-button>
        <el-button :disabled="status.state !== 'running'" @click="pause">
          <el-icon><VideoPause /></el-icon>&nbsp;暂停
        </el-button>
        <el-button :disabled="status.state === 'idle' || status.state === 'stopped'" @click="stop">
          <el-icon><SwitchButton /></el-icon>&nbsp;停止
        </el-button>
      </div>
    </el-card>

    <!-- 本工程进度:来自工程自身数据,与全局队列无关 -->
    <el-card shadow="never">
      <template #header>
        本工程进度 <span class="muted">— 来自本工程数据({{ projectDone }} / {{ projectTotal }} 条)</span>
      </template>
      <ProgressPanel
        :chapters="displayedChapters"
        :logs="logs"
        :done-fragments="projectDone"
        :total-fragments="projectTotal"
        :current-chapter="liveCurrentChapter"
        :eta-label="liveEta"
        :state-label="stateLabel"
      >
        <template #row-actions="{ row }">
          <el-button
            size="small"
            text
            type="primary"
            :disabled="jobs.runningChapter === row.name"
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
      </ProgressPanel>
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
.queue-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.queue-label {
  color: var(--el-text-color-secondary);
  width: 72px;
  flex: none;
}
.queue-chip {
  margin-right: 4px;
}
.queue-controls {
  margin-top: 6px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
