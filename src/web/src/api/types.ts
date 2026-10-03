/**
 * 与后端 schemas 对齐的 TS 类型(手写,零代码共享 —— 架构硬规则 R6)。
 *
 * 来源:
 *   - doc/api/design.md     路由清单与出入参要点
 *   - src/models.py         领域模型
 *   - src/tts/capabilities.py  ParamSpec / TTSCapabilities
 *   - src/errors.py         错误分类
 *   - doc/project/design.md / doc/voice/design.md / doc/synthesis/design.md
 *
 * ⚠️ 后端 `src/api/schemas.py` 尚未实现。凡文档未明确钉死形状、由本文件自行
 *    约定的字段,均以 `// [约定]` 标注,并在实现报告中列为契约歧义项。
 */

// --------------------------------------------------------------------------- //
// 通用
// --------------------------------------------------------------------------- //

/** 统一错误响应体:`{"error": {"kind": "...", "message": "..."}}` */
export interface ApiErrorBody {
  error: {
    kind: ApiErrorKind
    message: string
    detail?: unknown
  }
}

/** 复用 errors.py 的分类;kinds 并集见 doc/api/design.md §5 与 errors.py */
export type ApiErrorKind =
  | 'retryable'
  | 'fatal'
  | 'invalid'
  | 'config'
  | 'not_found'
  | 'binding'
  | 'manifest'
  | string

// --------------------------------------------------------------------------- //
// /api/fs/browse —— 服务端卷内路径浏览
// --------------------------------------------------------------------------- //

export interface FsEntry {
  name: string
  has_children: boolean
}

export interface FsBrowse {
  path: string
  parent: string | null
  dirs: FsEntry[]
  /** 该目录是否像一个工程(含 json/) */
  is_project: boolean
}

// --------------------------------------------------------------------------- //
// /api/settings/providers —— 能力与参数自描述(前端动态渲染的依据)
// --------------------------------------------------------------------------- //

export type ParamType = 'bool' | 'int' | 'float' | 'str' | 'enum'

export interface ParamSpec {
  name: string
  label: string
  type: ParamType
  default: unknown
  minimum?: number | null
  maximum?: number | null
  choices?: string[] | null
}

export interface TTSCapabilities {
  /** 支持的情感意图:{"vector","ref_audio","text"} */
  emotion: string[]
  /** 情感向量维度;不支持向量时为 null */
  emotion_vector_dims: number | null
  /** 支持的音色指定方式:{"audio_bytes","voice_id"} */
  voice_modes: string[]
  needs_prompt_text: boolean
  supports_concurrent: boolean
  returns_canonical: boolean
}

export interface ProviderInfo {
  id: string
  label: string
  capabilities: TTSCapabilities
  param_schema: ParamSpec[]
}

// --------------------------------------------------------------------------- //
// /api/settings
// --------------------------------------------------------------------------- //

export interface AuthConfig {
  kind: 'none' | 'bearer' | 'header' | string
  token?: string | null
  header_name?: string | null
}

export interface ProviderConfig {
  base_url: string
  timeout: number
  retries: number
  auth?: AuthConfig
  /** 键名来自本 provider 的 param_schema;每个 provider 独立存储 */
  params: Record<string, unknown>
}

export interface Settings {
  workspace_root: string
  voices_root: string
  config_dir: string
  host: string
  port: number
  log_level: string
  active_provider: string
  providers: Record<string, ProviderConfig>
  concurrency: number
  reference_max_seconds: number
  reference_max_sample_rate: number
}

// --------------------------------------------------------------------------- //
// /api/tts/probe —— 测试连接
// --------------------------------------------------------------------------- //

export type ProbeKind =
  | 'ok'
  | 'unreachable'
  | 'model_not_loaded'
  | 'server_error'
  | 'bad_request'

export interface HealthResult {
  ok: boolean
  kind: ProbeKind
  message: string
  latency_ms?: number | null
  duration_ms?: number | null
  sample_rate?: number | null
  /**
   * [约定] 成功时的探测音频。后端 models.HealthResult.sample_audio 是 bytes,
   * 经 JSON 序列化后应为 base64 字符串(字段名沿用 sample_audio)。
   * 前端据此拼成 data:audio/wav;base64,... 供 <audio> 试听。
   */
  sample_audio?: string | null
}

export interface ProbeRequest {
  provider?: string
  base_url?: string
  /** 探测用参考音色名(后端从音色库取字节) */
  voice?: string
  timeout?: number
}

// --------------------------------------------------------------------------- //
// 工程 / 章节 / 角色
// --------------------------------------------------------------------------- //

export type ChapterStatus = 'pending' | 'partial' | 'done'

export interface ChapterSummary {
  name: string
  sentence_count: number
  status: ChapterStatus
  /** 相对工程根的成品路径(如 audio/P01_扉页.wav) */
  output?: string | null
  provider?: string | null
  /** 详情接口(api/design.md §8.1)带回的片段计数 */
  done_fragments?: number
  failed_fragments?: number
}

/** [约定] 角色一行:后端聚合 + 绑定解析后的视图 */
export type RoleStatus = 'ok' | 'unbound' | 'invalid'

export interface RoleView {
  name: string
  /** 当前绑定音色名;未绑定为 null */
  voice: string | null
  /** 可选:情感参考集条目名 */
  emo_ref?: string | null
  /** ok=已绑定且音色存在;unbound=未绑定;invalid=绑定音色已被删除 */
  status: RoleStatus
}

export interface ProjectSummary {
  name: string
  path: string
  chapter_count: number
  /** 已完成章节数 */
  done_count?: number
  /** 总句数(可选概览) */
  sentence_count?: number
  /** 0~1 进度(可选概览) */
  progress?: number
  has_manifest?: boolean
}

/** 绑定被改动后,已生成片段所用的音色与当前绑定不符的章节(api/design.md §8.1) */
export interface StaleChapter {
  name: string
  /** 受影响的片段序号 */
  stale_fragments: number[]
}

/** 详情接口的 manifest 摘要计数 */
export interface ManifestSummary {
  done: number
  partial: number
  pending: number
}

export interface ProjectDetail {
  name: string
  path?: string
  chapters: ChapterSummary[]
  roles: RoleView[]
  bindings: BindingsFile
  manifest_summary?: ManifestSummary | null
  /**
   * 绑定变更导致「片段基于旧音色」的章节。语义:只提示,不自动重做。
   * 旧版本后端可能不返回 → 前端按空数组容错(见 useStaleChapters)。
   */
  stale_chapters?: StaleChapter[] | null
  warnings?: string[]
}

export interface CreateProjectRequest {
  /** 书名(工程名) */
  name: string
  /** 从哪个容器内目录导入 JSON(经 /api/fs/browse 选定) */
  source_path: string
}

/** 上传建工程(api/design.md §8.9):multipart,字段名固定为 files(复数) */
export interface UploadProjectRequest {
  name: string
  files: File[]
}

/** 删除工程(api/design.md §8.10):目录移入回收站,不真删 */
export interface DeleteProjectResult {
  deleted: string
  trashed_to: string
}

// --------------------------------------------------------------------------- //
// 绑定
// --------------------------------------------------------------------------- //

export interface Binding {
  voice: string
  emo_ref?: string | null
}

export interface BindingsFile {
  version: number
  bindings: Record<string, Binding>
}

// --------------------------------------------------------------------------- //
// 音色库
// --------------------------------------------------------------------------- //

export interface VoiceEntry {
  name: string
  path: string
  main_audio: string
  prompt_text: string | null
  /** 情感名 -> 音频路径 */
  emo_refs: Record<string, string>
  is_normalized: boolean
  /** 非标准结构提示 */
  warnings: string[]
}

export interface CreateVoiceRequest {
  name: string
  prompt_text?: string | null
  file: File
}

export interface UpdateVoiceRequest {
  prompt_text?: string | null
  file?: File | null
  /** 重命名(目录名 = 音色名);为空则不改名 */
  rename?: string | null
}

// --------------------------------------------------------------------------- //
// 合成任务 / 进度事件
// --------------------------------------------------------------------------- //

export type JobState = 'idle' | 'running' | 'paused' | 'stopped'

export interface ChapterProgress {
  name: string
  total: number
  /** 已完成条数 */
  done: number
  failed: number
  status: ChapterStatus | 'synthesizing' | 'concatenating'
}

export interface LogEntry {
  level: 'debug' | 'info' | 'warn' | 'error'
  message: string
  /** 失败条目可定位:章节 + 序号 */
  chapter?: string | null
  index?: number | null
  ts?: string
}

export interface JobStatus {
  state: JobState
  project?: string | null
  current_chapter?: string | null
  /** 全局:已完成 / 总条数(用于总进度条) */
  done_fragments?: number
  total_fragments?: number
  chapters?: ChapterProgress[]
  /** [约定] ETA 秒(后端粗估);缺失时前端按实测均值本地估算 */
  eta_seconds?: number | null
  recent_logs?: LogEntry[]
}

export interface StartJobRequest {
  project: string
  /** 为空表示全部章节 */
  chapters?: string[]
  /** 显式重做(作废旧成品与片段) */
  redo?: boolean
}

export type ProgressEventType =
  | 'chapter_started'
  | 'fragment_done'
  | 'fragment_failed'
  | 'chapter_concatenated'
  | 'chapter_failed'
  | 'job_state'
  | 'log'

export interface ProgressEvent {
  type: ProgressEventType
  chapter?: string
  index?: number
  /** chapter_started:总数 / 需生成数 */
  total?: number
  remaining?: number
  /** fragment_done:耗时 ms / 累计 */
  elapsed_ms?: number
  done?: number
  failed?: number
  /** fragment_failed:错误分类与重试次数 */
  error_kind?: string
  error?: string
  attempts?: number
  /** chapter_concatenated:成品路径、时长 */
  output?: string
  duration_ms?: number
  /** job_state */
  state?: JobState
  /** log */
  level?: LogEntry['level']
  message?: string
  ts?: string
}
