/**
 * 后端调用封装(fetch + SSE)。
 *
 * 严格对齐 doc/api/design.md 的 15 条路由。所有路径字符串集中在本文件,
 * store / 组件不直接拼 URL —— 契约只有一个来源。
 *
 * Mock:开发用 mock(src/api/mock.ts)通过 `window.__AUDIOBOOK_MOCK__` 注册一个桥接对象,
 * 本文件在每次调用时惰性读取它。client **不 import mock**,因此生产构建里 mock
 * 不会被打进产物(见 main.ts 的条件动态导入)。
 */

import type {
  BindingsFile,
  CreateProjectRequest,
  CreateVoiceRequest,
  DeleteProjectResult,
  FsBrowse,
  HealthResult,
  JobStatus,
  ProbeRequest,
  ProjectDetail,
  ProjectSummary,
  ProviderInfo,
  Settings,
  StartJobRequest,
  UpdateVoiceRequest,
  UploadProjectRequest,
  VoiceEntry,
} from './types'

// --------------------------------------------------------------------------- //
// 章节 JSON 上传的前端侧校验(服务端才是权威;前台先拦能少一次往返)
// api/design.md §8.9:只接 .json、单文件 8MB、单次 200 个、总 64MB
// --------------------------------------------------------------------------- //

export const CHAPTER_FILE_LIMITS = {
  maxFileBytes: 8 * 1024 * 1024,
  maxCount: 200,
  maxTotalBytes: 64 * 1024 * 1024,
} as const

function formatMB(bytes: number): string {
  return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`
}

/** 校验一批待上传的章节 JSON;通过返回 null,否则返回可直接展示的错误文案 */
export function validateChapterFiles(files: File[]): string | null {
  if (files.length === 0) return '请至少选择一个 .json 文件'
  if (files.length > CHAPTER_FILE_LIMITS.maxCount) {
    return `单次最多上传 ${CHAPTER_FILE_LIMITS.maxCount} 个文件(当前 ${files.length} 个)`
  }
  let total = 0
  for (const f of files) {
    const lower = f.name.toLowerCase()
    if (!lower.endsWith('.json')) return `只允许 .json 文件:${f.name}`
    if (f.size === 0) return `文件为空:${f.name}`
    if (f.size > CHAPTER_FILE_LIMITS.maxFileBytes) {
      return `单文件不得超过 ${formatMB(CHAPTER_FILE_LIMITS.maxFileBytes)}:${f.name}(${formatMB(f.size)})`
    }
    total += f.size
  }
  if (total > CHAPTER_FILE_LIMITS.maxTotalBytes) {
    return `总大小不得超过 ${formatMB(CHAPTER_FILE_LIMITS.maxTotalBytes)}(当前 ${formatMB(total)})`
  }
  return null
}

// --------------------------------------------------------------------------- //
// 错误
// --------------------------------------------------------------------------- //

export class ApiError extends Error {
  readonly kind: string
  readonly status: number
  readonly detail: unknown

  constructor(kind: string, message: string, status = 0, detail: unknown = undefined) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.detail = detail
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let kind = 'fatal'
  let message = `请求失败(HTTP ${res.status})`
  let detail: unknown = undefined
  try {
    const body = (await res.json()) as { error?: { kind?: string; message?: string; detail?: unknown } }
    if (body?.error) {
      kind = body.error.kind || kind
      message = body.error.message || message
      detail = body.error.detail
    }
  } catch {
    // 非 JSON 错误体,保留默认信息
    try {
      const text = await res.text()
      if (text) message = text
    } catch {
      /* ignore */
    }
  }
  return new ApiError(kind, message, res.status, detail)
}

// --------------------------------------------------------------------------- //
// Mock 桥接
// --------------------------------------------------------------------------- //

export interface SseHandlers {
  onEvent: (payload: unknown, eventName: string) => void
  onOpen?: () => void
  onError?: (err?: unknown) => void
}

export interface MockSubscription {
  close: () => void
}

export interface MockBridge {
  request: (path: string, init?: RequestInit) => Promise<Response>
  subscribe: (handlers: SseHandlers) => MockSubscription
}

function mockBridge(): MockBridge | undefined {
  return (window as unknown as { __AUDIOBOOK_MOCK__?: MockBridge }).__AUDIOBOOK_MOCK__
}

/** 当前是否运行在 mock 模式(用于界面角标提示) */
export function isMockEnabled(): boolean {
  return mockBridge() !== undefined
}

// --------------------------------------------------------------------------- //
// 核心请求
// --------------------------------------------------------------------------- //

async function doFetch(path: string, init?: RequestInit): Promise<Response> {
  const bridge = mockBridge()
  if (bridge) return bridge.request(path, init)

  const headers = new Headers(init?.headers || {})
  // FormData 由浏览器自动带 boundary,不要覆盖 Content-Type
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Accept', headers.get('Accept') || 'application/json, text/event-stream, audio/wav')
  return fetch(path, { ...init, headers })
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await doFetch(path, init)
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return (await res.json()) as T
  return (await res.text()) as unknown as T
}

/** 取一个音频接口的 Blob URL(供 <audio> 播放)。调用方负责 URL.revokeObjectURL。 */
async function fetchAudioObjectUrl(path: string): Promise<string> {
  const res = await doFetch(path)
  if (!res.ok) throw await toApiError(res)
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

function json(body: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(body) }
}

// --------------------------------------------------------------------------- //
// 路由封装(与 doc/api/design.md 一一对应)
// --------------------------------------------------------------------------- //

export const api = {
  // -- 路径浏览 ---------------------------------------------------------- //
  browseFs(path: string): Promise<FsBrowse> {
    return request<FsBrowse>(`/api/fs/browse?path=${encodeURIComponent(path)}`)
  },

  // -- 设置 -------------------------------------------------------------- //
  getSettings(): Promise<Settings> {
    return request<Settings>('/api/settings')
  },
  updateSettings(patch: Partial<Settings>): Promise<Settings> {
    return request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) })
  },
  getProviders(): Promise<ProviderInfo[]> {
    return request<ProviderInfo[]>('/api/settings/providers')
  },

  // -- 测试连接 ---------------------------------------------------------- //
  probe(req: ProbeRequest = {}): Promise<HealthResult> {
    return request<HealthResult>('/api/tts/probe', json(req))
  },

  // -- 工程 -------------------------------------------------------------- //
  listProjects(): Promise<ProjectSummary[]> {
    return request<ProjectSummary[]>('/api/projects')
  },
  createProject(req: CreateProjectRequest): Promise<ProjectDetail> {
    return request<ProjectDetail>('/api/projects', json(req))
  },
  /** 上传 JSON 建工程(multipart,字段名固定 files) */
  uploadProject(req: UploadProjectRequest): Promise<ProjectDetail> {
    const fd = new FormData()
    fd.append('name', req.name)
    for (const f of req.files) fd.append('files', f)
    return request<ProjectDetail>('/api/projects/upload', { method: 'POST', body: fd })
  },
  /** 追加/替换章节(multipart,字段名固定 files;同名覆盖) */
  appendChapters(name: string, files: File[]): Promise<ProjectDetail> {
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    return request<ProjectDetail>(`/api/projects/${encodeURIComponent(name)}/chapters`, {
      method: 'POST',
      body: fd,
    })
  },
  getProject(name: string): Promise<ProjectDetail> {
    return request<ProjectDetail>(`/api/projects/${encodeURIComponent(name)}`)
  },
  /** 删除工程:整个目录移入 <workspace>/.trash/(不真删);正在合成 → 409 */
  deleteProject(name: string): Promise<DeleteProjectResult> {
    return request<DeleteProjectResult>(`/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' })
  },
  /** 删除章节:成品移入 audio/.stale/(不真删);该章正在合成 → 409;返回更新后的详情 */
  deleteChapter(name: string, chapter: string): Promise<ProjectDetail> {
    return request<ProjectDetail>(
      `/api/projects/${encodeURIComponent(name)}/chapters/${encodeURIComponent(chapter)}`,
      { method: 'DELETE' },
    )
  },

  // -- 绑定 -------------------------------------------------------------- //
  getBindings(name: string): Promise<BindingsFile> {
    return request<BindingsFile>(`/api/projects/${encodeURIComponent(name)}/bindings`)
  },
  updateBindings(name: string, bindings: BindingsFile): Promise<BindingsFile> {
    return request<BindingsFile>(`/api/projects/${encodeURIComponent(name)}/bindings`, {
      method: 'PUT',
      body: JSON.stringify(bindings),
    })
  },

  // -- 章节成品试听 ------------------------------------------------------ //
  chapterAudioUrl(name: string, chapter: string): Promise<string> {
    return fetchAudioObjectUrl(
      `/api/projects/${encodeURIComponent(name)}/chapters/${encodeURIComponent(chapter)}/audio`,
    )
  },

  // -- 音色库 ------------------------------------------------------------ //
  listVoices(): Promise<VoiceEntry[]> {
    return request<VoiceEntry[]>('/api/voices')
  },
  createVoice(req: CreateVoiceRequest): Promise<VoiceEntry> {
    const fd = new FormData()
    fd.append('name', req.name)
    if (req.prompt_text != null) fd.append('prompt_text', req.prompt_text)
    fd.append('file', req.file)
    return request<VoiceEntry>('/api/voices', { method: 'POST', body: fd })
  },
  updateVoice(name: string, req: UpdateVoiceRequest): Promise<VoiceEntry> {
    const fd = new FormData()
    if (req.prompt_text != null) fd.append('prompt_text', req.prompt_text)
    if (req.rename) fd.append('rename', req.rename)
    if (req.file) fd.append('file', req.file)
    return request<VoiceEntry>(`/api/voices/${encodeURIComponent(name)}`, { method: 'PUT', body: fd })
  },
  deleteVoice(name: string): Promise<void> {
    return request<void>(`/api/voices/${encodeURIComponent(name)}`, { method: 'DELETE' })
  },
  voicePreviewUrl(name: string): Promise<string> {
    return fetchAudioObjectUrl(`/api/voices/${encodeURIComponent(name)}/preview`)
  },

  // -- 合成任务 ---------------------------------------------------------- //
  startJob(req: StartJobRequest): Promise<JobStatus> {
    return request<JobStatus>('/api/jobs', json(req))
  },
  getJobState(): Promise<JobStatus> {
    return request<JobStatus>('/api/jobs/state')
  },
  pauseJob(): Promise<JobStatus> {
    return request<JobStatus>('/api/jobs/pause', { method: 'POST' })
  },
  stopJob(): Promise<JobStatus> {
    return request<JobStatus>('/api/jobs/stop', { method: 'POST' })
  },

  /**
   * 订阅进度事件流(SSE)。
   * 断线重连由调用方负责:重连前先 getJobState() 补齐,再调本函数订阅增量。
   */
  subscribeJobEvents(handlers: SseHandlers): { close: () => void } {
    const bridge = mockBridge()
    if (bridge) return bridge.subscribe(handlers)

    const es = new EventSource('/api/jobs/events')
    const named = [
      'chapter_started',
      'fragment_done',
      'fragment_failed',
      'chapter_concatenated',
      'chapter_failed',
      'job_state',
      'log',
    ]
    const dispatch = (name: string) => (ev: MessageEvent) => {
      let payload: unknown = ev.data
      try {
        payload = JSON.parse(ev.data)
      } catch {
        /* 保留原始文本 */
      }
      handlers.onEvent(payload, name)
    }

    es.onopen = () => handlers.onOpen?.()
    es.onerror = (err) => handlers.onError?.(err)
    // 兼容两种后端:默认 message 事件(data 里带 type),或具名事件(event: <type>)
    es.onmessage = dispatch('message')
    for (const name of named) es.addEventListener(name, dispatch(name) as EventListener)

    return { close: () => es.close() }
  },
}
