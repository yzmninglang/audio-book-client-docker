<script setup lang="ts">
import { computed } from 'vue'

import type { ChapterProgress, LogEntry } from '@/api/types'

const props = defineProps<{
  chapters: ChapterProgress[]
  logs: LogEntry[]
  doneFragments: number
  totalFragments: number
  currentChapter?: string | null
  etaLabel?: string
  stateLabel?: string
}>()

const percent = computed(() => {
  if (!props.totalFragments) return 0
  return Math.min(100, Math.round((props.doneFragments / props.totalFragments) * 100))
})

const statusText: Record<string, string> = {
  pending: '待生成',
  synthesizing: '合成中',
  concatenating: '拼接中',
  partial: '部分完成',
  done: '已完成',
}

const statusType: Record<string, 'info' | 'warning' | 'primary' | 'danger' | 'success'> = {
  pending: 'info',
  synthesizing: 'primary',
  concatenating: 'warning',
  partial: 'danger',
  done: 'success',
}

const levelType: Record<string, 'info' | 'warning' | 'danger' | 'primary'> = {
  debug: 'info',
  info: 'primary',
  warn: 'warning',
  error: 'danger',
}
</script>

<template>
  <div class="progress-panel">
    <div class="pp-summary">
      <div class="pp-stat">
        <span class="pp-label">状态</span>
        <el-tag type="primary" effect="dark" size="small">{{ stateLabel || '空闲' }}</el-tag>
      </div>
      <div class="pp-stat">
        <span class="pp-label">进度</span>
        <span>{{ doneFragments }} / {{ totalFragments }} 条</span>
      </div>
      <div v-if="currentChapter" class="pp-stat">
        <span class="pp-label">当前章节</span>
        <span>{{ currentChapter }}</span>
      </div>
      <div v-if="etaLabel" class="pp-stat">
        <span class="pp-label">预计剩余</span>
        <span>{{ etaLabel }}</span>
      </div>
    </div>

    <el-progress :percentage="percent" :stroke-width="14" :text-inside="true" />

    <el-table :data="chapters" size="small" class="pp-table" empty-text="暂无章节进度">
      <el-table-column prop="name" label="章节" min-width="160" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType[row.status] || 'info'" size="small">{{ statusText[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="片段进度" width="200">
        <template #default="{ row }">
          <el-progress
            :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
            :stroke-width="10"
            :format="() => `${row.done}/${row.total}`"
          />
        </template>
      </el-table-column>
      <el-table-column label="失败" width="80">
        <template #default="{ row }">
          <span :class="{ 'pp-failed': row.failed > 0 }">{{ row.failed }}</span>
        </template>
      </el-table-column>
    </el-table>

    <div class="pp-logs">
      <div class="pp-logs-title">日志</div>
      <div class="pp-log-body">
        <el-empty v-if="logs.length === 0" description="暂无日志" :image-size="50" />
        <div v-for="(log, i) in logs" :key="i" class="pp-log-line">
          <el-tag :type="levelType[log.level] || 'info'" size="small" effect="plain" class="pp-log-level">
            {{ log.level }}
          </el-tag>
          <span class="pp-log-loc">
            <template v-if="log.chapter">[{{ log.chapter }}<template v-if="log.index"> #{{ log.index }}</template>] </template>
          </span>
          <span class="pp-log-msg">{{ log.message }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.progress-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pp-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.pp-stat {
  display: flex;
  align-items: center;
  gap: 6px;
}
.pp-label {
  color: var(--el-text-color-secondary);
}
.pp-table {
  width: 100%;
}
.pp-failed {
  color: var(--el-color-danger);
  font-weight: 600;
}
.pp-logs-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.pp-log-body {
  max-height: 220px;
  overflow-y: auto;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  padding: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.pp-log-line {
  display: flex;
  gap: 6px;
  padding: 2px 0;
  align-items: baseline;
}
.pp-log-level {
  flex: none;
}
.pp-log-loc {
  color: var(--el-color-primary);
  flex: none;
}
.pp-log-msg {
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
