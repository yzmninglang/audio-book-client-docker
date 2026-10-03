import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { ChapterProgress, JobState, JobStatus, LogEntry, ProgressEvent, StartJobRequest } from '@/api/types'

function emptyStatus(): JobStatus {
  return {
    state: 'idle',
    project: null,
    current_chapter: null,
    done_fragments: 0,
    total_fragments: 0,
    chapters: [],
    recent_logs: [],
  }
}

export const useJobsStore = defineStore('jobs', () => {
  const status = ref<JobStatus>(emptyStatus())
  const logs = ref<LogEntry[]>([])
  const connected = ref(false)
  const reconnecting = ref(false)

  let subscription: { close: () => void } | null = null

  const state = computed<JobState>(() => status.value.state)
  const isActive = computed(() => state.value === 'running' || state.value === 'paused')
  const chapters = computed<ChapterProgress[]>(() => status.value.chapters || [])

  const totalFragments = computed(() => status.value.total_fragments || 0)
  const doneFragments = computed(() => status.value.done_fragments || 0)

  /** 正在运行的任务所属工程(用于禁用删除入口);无运行任务时为 null */
  const runningProject = computed(() =>
    state.value === 'running' ? status.value.project ?? null : null,
  )
  /** 正在合成的章节名;无则为 null */
  const runningChapter = computed(() =>
    state.value === 'running' ? status.value.current_chapter ?? null : null,
  )

  /** 粗略 ETA:优先用后端值,否则按「剩余条数 × 实测均值(默认 3s)」 */
  const etaSeconds = computed<number | null>(() => {
    if (status.value.eta_seconds != null) return status.value.eta_seconds
    if (!isActive.value || totalFragments.value === 0) return null
    const remaining = Math.max(0, totalFragments.value - doneFragments.value)
    if (remaining === 0) return 0
    return Math.round(remaining * 3)
  })

  const etaLabel = computed(() => {
    const s = etaSeconds.value
    if (s == null) return ''
    if (s === 0) return '即将完成'
    const m = Math.floor(s / 60)
    const sec = s % 60
    return m > 0 ? `约 ${m} 分 ${sec} 秒` : `约 ${sec} 秒`
  })

  function pushLog(entry: LogEntry) {
    logs.value.push(entry)
    if (logs.value.length > 500) logs.value.splice(0, logs.value.length - 500)
  }

  function applyEvent(ev: ProgressEvent) {
    switch (ev.type) {
      case 'job_state': {
        if (ev.state) status.value.state = ev.state
        break
      }
      case 'chapter_started': {
        status.value.current_chapter = ev.chapter || null
        const cp: ChapterProgress = {
          name: ev.chapter || '',
          total: ev.total ?? 0,
          done: 0,
          failed: 0,
          status: 'synthesizing',
        }
        const rest = (status.value.chapters || []).filter((c) => c.name !== cp.name)
        status.value.chapters = [...rest, cp]
        break
      }
      case 'fragment_done': {
        const cp = status.value.chapters?.find((c) => c.name === ev.chapter)
        if (cp && ev.done != null) cp.done = ev.done
        else if (cp) cp.done = Math.min(cp.total, cp.done + 1)
        status.value.done_fragments = (status.value.done_fragments || 0) + 1
        break
      }
      case 'fragment_failed': {
        const cp = status.value.chapters?.find((c) => c.name === ev.chapter)
        if (cp) cp.failed += 1
        pushLog({
          level: 'error',
          message: `片段失败(章节 ${ev.chapter} #${ev.index}):${ev.error || ev.error_kind || '未知错误'}${
            ev.attempts ? `(重试 ${ev.attempts} 次)` : ''
          }`,
          chapter: ev.chapter,
          index: ev.index,
          ts: ev.ts,
        })
        break
      }
      case 'chapter_concatenated': {
        const cp = status.value.chapters?.find((c) => c.name === ev.chapter)
        if (cp) cp.status = 'done'
        pushLog({ level: 'info', message: `章节完成:${ev.chapter} → ${ev.output || ''}`, chapter: ev.chapter, ts: ev.ts })
        break
      }
      case 'chapter_failed': {
        const cp = status.value.chapters?.find((c) => c.name === ev.chapter)
        if (cp) cp.status = 'partial'
        pushLog({ level: 'error', message: `章节失败:${ev.chapter}${ev.message ? ' - ' + ev.message : ''}`, chapter: ev.chapter, ts: ev.ts })
        break
      }
      case 'log': {
        pushLog({
          level: ev.level || 'info',
          message: ev.message || '',
          chapter: ev.chapter,
          index: ev.index,
          ts: ev.ts,
        })
        break
      }
      default:
        break
    }
  }

  /** 断线重连:先拉 state 补齐,再订阅增量 */
  async function connect() {
    disconnect()
    try {
      status.value = await api.getJobState()
      logs.value = [...(status.value.recent_logs || [])]
    } catch {
      /* state 拉取失败不阻塞订阅 */
    }
    subscription = api.subscribeJobEvents({
      onOpen: () => {
        connected.value = true
        reconnecting.value = false
      },
      onEvent: (payload, name) => {
        const ev = payload as ProgressEvent
        if (!ev || typeof ev !== 'object') return
        if (!ev.type) ev.type = name as ProgressEvent['type']
        applyEvent(ev)
      },
      onError: () => {
        connected.value = false
        reconnecting.value = true
        // EventSource 自带重连;这里延迟后补一次 state,避免增量期间丢帧
        window.setTimeout(() => {
          api
            .getJobState()
            .then((s) => {
              status.value = s
            })
            .catch(() => undefined)
        }, 2000)
      },
    })
  }

  function disconnect() {
    if (subscription) {
      subscription.close()
      subscription = null
    }
    connected.value = false
  }

  async function refreshState() {
    status.value = await api.getJobState()
  }

  async function start(req: StartJobRequest) {
    logs.value = []
    status.value = await api.startJob(req)
  }

  async function pause() {
    status.value = await api.pauseJob()
  }

  async function stop() {
    status.value = await api.stopJob()
  }

  return {
    status,
    logs,
    connected,
    reconnecting,
    state,
    isActive,
    chapters,
    totalFragments,
    doneFragments,
    runningProject,
    runningChapter,
    etaSeconds,
    etaLabel,
    connect,
    disconnect,
    refreshState,
    applyEvent,
    start,
    pause,
    stop,
    pushLog,
  }
})
