/**
 * 开发用 mock —— 覆盖 doc/api/design.md 的全部 15 条路由的假数据。
 *
 * 本文件**不得进入生产构建**:只用条件动态 import 装载(见 src/main.ts),
 * 生产构建里 `import.meta.env.VITE_USE_MOCK` 为假,该分支被 tree-shake 掉。
 * 本文件第一行的 MARKER 用于构建产物校验(见 README)。
 *
 * 启用方式见 README.md:`VITE_USE_MOCK=1`(或 `npm run dev:mock`)。
 *
 * 覆盖内容:
 *   - 工程列表 / 详情 / 新建 / 章节成品
 *   - 角色列表 + 绑定读写(含「未绑定」「失效」两种状态)
 *   - 音色列表(真实音色名)/ 新建 / 编辑 / 重命名 / 删除 / 试听
 *   - 服务端卷内路径浏览(容器内目录树)
 *   - 设置 / providers(两个 provider,各带 param_schema)
 *   - 测试连接(按 URL 关键字演示 4 种 kind)
 *   - 任务开始/暂停/停止 + 进度事件流(SSE 常驻模拟)
 */

import type {
  BindingsFile,
  ChapterProgress,
  ChapterSummary,
  FsBrowse,
  HealthResult,
  JobStatus,
  LogEntry,
  ParamSpec,
  ProgressEvent,
  ProjectDetail,
  ProjectSummary,
  ProviderInfo,
  RoleView,
  Settings,
  VoiceEntry,
} from './types'
import type { MockBridge, SseHandlers } from './client'

export const MOCK_MARKER = '__AUDIOBOOK_MOCK_MARKER__'

// --------------------------------------------------------------------------- //
// 小工具
// --------------------------------------------------------------------------- //

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  })
}

function errorResponse(kind: string, message: string, status: number): Response {
  return jsonResponse({ error: { kind, message } }, status)
}

/** 生成一段可播放的 WAV(单声道 22.05k),用于试听占位。 */
function makeWav(durationMs: number, freq = 330, sampleRate = 22050): Uint8Array {
  const n = Math.max(1, Math.floor((sampleRate * durationMs) / 1000))
  const bytes = new Uint8Array(44 + n * 2)
  const view = new DataView(bytes.buffer)
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i))
  }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + n * 2, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeStr(36, 'data')
  view.setUint32(40, n * 2, true)
  for (let i = 0; i < n; i++) {
    const s = Math.sin((2 * Math.PI * freq * i) / sampleRate) * 0.18
    view.setInt16(44 + i * 2, s * 32767, true)
  }
  return bytes
}

function wavResponse(durationMs: number, freq = 330): Response {
  return new Response(makeWav(durationMs, freq), {
    status: 200,
    headers: { 'Content-Type': 'audio/wav' },
  })
}

function bytesToBase64(bytes: Uint8Array): string {
  let bin = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(bin)
}

const nowIso = () => new Date().toISOString()

// --------------------------------------------------------------------------- //
// 假数据
// --------------------------------------------------------------------------- //

let settings: Settings = {
  workspace_root: '/workspace',
  voices_root: '/voices',
  config_dir: '/config',
  host: '0.0.0.0',
  port: 8000,
  log_level: 'INFO',
  active_provider: 'indextts2',
  providers: {
    indextts2: {
      base_url: 'http://hinas-v4.ninglang.top:8300',
      timeout: 300,
      retries: 3,
      auth: { kind: 'none' },
      params: {
        max_text_tokens_per_sentence: 120,
        do_sample: true,
        temperature: 0.8,
        top_p: 0.8,
        top_k: 30,
        num_beams: 3,
        repetition_penalty: 10.0,
        length_penalty: 0.0,
        max_mel_tokens: 1500,
      },
    },
    mock_tts: {
      base_url: 'http://localhost:9000',
      timeout: 120,
      retries: 2,
      auth: { kind: 'none' },
      params: { style: '叙述', speed: 1.0, expressiveness: 0.5, enable_ssml: false, format: 'wav' },
    },
  },
  concurrency: 1,
  reference_max_seconds: 15,
  reference_max_sample_rate: 22050,
}

// param_schema 照搬 src/tts/capabilities.py 的 9 项(IndexTTS2)。
// 第二个 provider 刻意用完全不同的参数集,证明前端是「动态渲染」而非硬编码。
const INDEXTTS_PARAM_SCHEMA: ParamSpec[] = [
  { name: 'max_text_tokens_per_sentence', label: '每句最大文本 Token 数', type: 'int', default: 120, minimum: 1, maximum: 600 },
  { name: 'do_sample', label: '采样生成', type: 'bool', default: true },
  { name: 'temperature', label: '温度', type: 'float', default: 0.8, minimum: 0, maximum: 2 },
  { name: 'top_p', label: 'Top-P', type: 'float', default: 0.8, minimum: 0, maximum: 1 },
  { name: 'top_k', label: 'Top-K', type: 'int', default: 30, minimum: 0, maximum: 200 },
  { name: 'num_beams', label: 'Beam 数', type: 'int', default: 3, minimum: 1, maximum: 10 },
  { name: 'repetition_penalty', label: '重复惩罚', type: 'float', default: 10, minimum: 0, maximum: 20 },
  { name: 'length_penalty', label: '长度惩罚', type: 'float', default: 0, minimum: -2, maximum: 2 },
  { name: 'max_mel_tokens', label: '最大 Mel Token 数', type: 'int', default: 1500, minimum: 100, maximum: 4000 },
]

const MOCK_TTS_PARAM_SCHEMA: ParamSpec[] = [
  { name: 'style', label: '朗读风格', type: 'enum', default: '叙述', choices: ['叙述', '激昂', '温柔', '冷静'] },
  { name: 'speed', label: '语速', type: 'float', default: 1.0, minimum: 0.5, maximum: 2.0 },
  { name: 'expressiveness', label: '表现力', type: 'float', default: 0.5, minimum: 0, maximum: 1 },
  { name: 'enable_ssml', label: '启用 SSML', type: 'bool', default: false },
  { name: 'format', label: '输出格式', type: 'enum', default: 'wav', choices: ['wav', 'flac'] },
]

const PROVIDERS: ProviderInfo[] = [
  {
    id: 'indextts2',
    label: 'IndexTTS2',
    capabilities: {
      emotion: ['vector', 'ref_audio', 'text'],
      emotion_vector_dims: 8,
      voice_modes: ['audio_bytes'],
      needs_prompt_text: false,
      supports_concurrent: false,
      returns_canonical: true,
    },
    param_schema: INDEXTTS_PARAM_SCHEMA,
  },
  {
    id: 'mock_tts',
    label: '演示后端(无情感向量)',
    capabilities: {
      emotion: ['text'],
      emotion_vector_dims: null,
      voice_modes: ['audio_bytes', 'voice_id'],
      needs_prompt_text: true,
      supports_concurrent: true,
      returns_canonical: false,
    },
    param_schema: MOCK_TTS_PARAM_SCHEMA,
  },
]

// 音色库(用真实音色名)
const VOICE_NAMES = [
  '丁仪', '三体军官1', '中年女人1', '中年男人1', '中年男人2', '中年男人3',
  '刘羡阳', '叶流云', '圣人阮邛', '大佬齐静春', '女剑客', '宁摇', '宋集薪',
  '小裴钱', '少女阮秀', '少年李宝瓶', '少年李槐', '常伟思', '年轻道人', '庄妍',
  '成熟温柔女', '成熟稚归', '标准女声', '正经阿良', '汪淼', '活泼稚归', '独白',
  '申玉菲', '男旁白2', '符南华', '老头1', '老年叶文洁', '范闲', '赵瑶', '阿良',
  '陈平安1', '顾灿',
]

let voices: VoiceEntry[] = VOICE_NAMES.map((name, i) => {
  const emo_refs: Record<string, string> = {}
  if (name === '汪淼') {
    for (const e of ['不解', '叙述', '愤怒', '恐惧', '悲伤', '惊喜']) emo_refs[e] = `/voices/${name}/${e}.wav`
  }
  return {
    name,
    path: `/voices/${name}`,
    main_audio: `/voices/${name}/1.wav`,
    prompt_text: `这是音色「${name}」的参考文本示例。`,
    emo_refs,
    is_normalized: true,
    warnings: name === '陈平安1' ? ['原文件为伪 WAV(实为 MP3),已入库转码'] : [],
    _seed: i,
  } as VoiceEntry & { _seed: number }
})

// 工程
interface MockProject {
  name: string
  path: string
  chapters: ChapterSummary[]
  roles: string[]
  bindings: BindingsFile
  manifestProvider: string
  /** 绑定变更后,片段基于旧音色的章节(api/design.md §8.1) */
  stale_chapters: { name: string; stale_fragments: number[] }[]
}

const projects: MockProject[] = [
  {
    name: '三体',
    path: '/workspace/三体',
    chapters: [
      { name: 'P01_扉页', sentence_count: 103, status: 'done', output: 'audio/P01_扉页.wav' },
      { name: 'P02_疯狂年代', sentence_count: 187, status: 'partial', output: null },
      { name: 'P03_红岸之六', sentence_count: 142, status: 'pending', output: null },
    ],
    roles: ['旁白', '汪淼', '常伟思', '申玉菲', '叶文洁', '史强'],
    bindings: {
      version: 1,
      bindings: {
        旁白: { voice: '男旁白2' },
        汪淼: { voice: '汪淼', emo_ref: '叙述' },
        申玉菲: { voice: '申玉菲' },
        叶文洁: { voice: '老年叶文洁' },
        史强: { voice: '已删除的音色' }, // 该音色不存在 -> 失效演示
        // 常伟思:未绑定
      },
    },
    manifestProvider: 'indextts2',
    // 演示「绑定变更导致片段过期」提示
    stale_chapters: [
      { name: 'P01_扉页', stale_fragments: [12, 13] },
      { name: 'P02_疯狂年代', stale_fragments: [4] },
    ],
  },
  {
    name: '聪明的投资者',
    path: '/workspace/聪明的投资者',
    chapters: [
      { name: 'P01_扉页', sentence_count: 103, status: 'pending', output: null },
    ],
    roles: ['旁白'],
    bindings: { version: 1, bindings: {} },
    manifestProvider: 'indextts2',
    stale_chapters: [],
  },
]

// 容器内目录树(只读浏览)
const FS_TREE: Record<string, { is_project: boolean; dirs: string[] }> = {
  '/workspace': { is_project: false, dirs: ['三体', '聪明的投资者', '未初始化书'] },
  '/workspace/三体': { is_project: true, dirs: ['json', 'process_audio', 'audio'] },
  '/workspace/三体/json': { is_project: false, dirs: [] },
  '/workspace/三体/process_audio': { is_project: false, dirs: ['P02_疯狂年代'] },
  '/workspace/三体/audio': { is_project: false, dirs: [] },
  '/workspace/聪明的投资者': { is_project: true, dirs: ['json', 'process_audio', 'audio'] },
  '/workspace/聪明的投资者/json': { is_project: false, dirs: [] },
  '/workspace/未初始化书': { is_project: false, dirs: [] },
  '/voices': { is_project: false, dirs: VOICE_NAMES.slice(0, 12) },
  '/imports': { is_project: false, dirs: ['三体-新稿', '聪明的投资者-新稿'] },
  '/imports/三体-新稿': { is_project: true, dirs: ['json'] },
  '/imports/聪明的投资者-新稿': { is_project: true, dirs: ['json'] },
}

// --------------------------------------------------------------------------- //
// 视图构造
// --------------------------------------------------------------------------- //

function voiceExists(name: string): boolean {
  return voices.some((v) => v.name === name)
}

function rolesOf(p: MockProject): RoleView[] {
  return p.roles.map((role) => {
    const b = p.bindings.bindings[role]
    if (!b || !b.voice) return { name: role, voice: null, emo_ref: null, status: 'unbound' }
    if (!voiceExists(b.voice)) return { name: role, voice: b.voice, emo_ref: b.emo_ref ?? null, status: 'invalid' }
    return { name: role, voice: b.voice, emo_ref: b.emo_ref ?? null, status: 'ok' }
  })
}

function toSummary(p: MockProject): ProjectSummary {
  const done = p.chapters.filter((c) => c.status === 'done').length
  const total = p.chapters.reduce((s, c) => s + c.sentence_count, 0)
  return {
    name: p.name,
    path: p.path,
    chapter_count: p.chapters.length,
    done_count: done,
    sentence_count: total,
    progress: p.chapters.length ? done / p.chapters.length : 0,
    has_manifest: true,
  }
}

function toDetail(p: MockProject): ProjectDetail {
  const manifestSummary = {
    done: p.chapters.filter((c) => c.status === 'done').length,
    partial: p.chapters.filter((c) => c.status === 'partial').length,
    pending: p.chapters.filter((c) => c.status === 'pending').length,
  }
  return {
    name: p.name,
    path: p.path,
    chapters: p.chapters.map((c) => ({
      ...c,
      done_fragments: c.status === 'done' ? c.sentence_count : c.status === 'partial' ? Math.floor(c.sentence_count / 2) : 0,
      failed_fragments: 0,
    })),
    roles: rolesOf(p),
    bindings: p.bindings,
    manifest_summary: manifestSummary,
    stale_chapters: p.stale_chapters,
    warnings: [],
  }
}

function findProject(name: string): MockProject | undefined {
  return projects.find((p) => p.name === name)
}

// --------------------------------------------------------------------------- //
// 任务模拟(SSE)
// --------------------------------------------------------------------------- //

/** 排队中的任务(尚未开始) */
interface SimTask {
  project: string
  chapters: string[]
  count: number
}

/** 正在执行的工程:remaining 是尚未开始的章节,current 是正在跑的章节 */
interface ActiveTask {
  project: string
  remaining: { chapter: string; total: number }[]
  current: { chapter: string; total: number; done: number; failed: number } | null
}

interface SimState {
  status: JobStatus
  timer: ReturnType<typeof setInterval> | null
  /** FIFO 队列(排队中,尚未开始) */
  queue: SimTask[]
  active: ActiveTask | null
}

const listeners = new Set<(payload: ProgressEvent) => void>()

const blankStatus = (state: JobStatus['state']): JobStatus => ({
  state,
  project: null,
  current_chapter: null,
  done_fragments: 0,
  total_fragments: 0,
  chapters: [],
  pending: [],
  recent_logs: [],
})

const sim: SimState = {
  status: blankStatus('idle'),
  timer: null,
  queue: [],
  active: null,
}

function emit(payload: ProgressEvent) {
  for (const l of listeners) l(payload)
}

function pushLog(level: LogEntry['level'], message: string, chapter?: string, index?: number) {
  const entry: LogEntry = { level, message, chapter: chapter ?? null, index: index ?? null, ts: nowIso() }
  const logs = sim.status.recent_logs || []
  logs.push(entry)
  if (logs.length > 200) logs.splice(0, logs.length - 200)
  sim.status.recent_logs = logs
  emit({ type: 'log', level, message, chapter, index, ts: entry.ts })
}

function stopTimer() {
  if (sim.timer) {
    clearInterval(sim.timer)
    sim.timer = null
  }
}

function updatePending() {
  sim.status.pending = sim.queue.map((t) => ({ project: t.project, chapters: t.count }))
}

/** 某工程已排入(运行中或排队中)的章节名,用于幂等去重 */
function scheduledSet(project: string): Set<string> {
  const s = new Set<string>()
  for (const t of sim.queue) if (t.project === project) for (const c of t.chapters) s.add(c)
  if (sim.active?.project === project) {
    for (const r of sim.active.remaining) s.add(r.chapter)
    if (sim.active.current) s.add(sim.active.current.chapter)
  }
  return s
}

function tick() {
  if (sim.status.state !== 'running') return

  // 无正在执行的工程 -> 从队列取下一个(FIFO)
  if (!sim.active) {
    const next = sim.queue.shift()
    updatePending()
    if (!next) {
      sim.status.state = 'idle'
      sim.status.project = null
      sim.status.current_chapter = null
      sim.status.done_fragments = 0
      sim.status.total_fragments = 0
      sim.status.chapters = []
      pushLog('info', '队列已清空,全部完成')
      emit({ type: 'job_state', state: 'idle' })
      stopTimer()
      return
    }
    const p = findProject(next.project)
    if (!p) return
    const targets = next.chapters.length ? p.chapters.filter((c) => next.chapters.includes(c.name)) : p.chapters
    const toRun = targets.filter((c) => c.status !== 'done')
    if (!toRun.length) return
    sim.active = {
      project: next.project,
      remaining: toRun.map((c) => ({ chapter: c.name, total: c.sentence_count })),
      current: null,
    }
    // 只有「正在执行」的工程才写进 status.project(修复:不再被最后入队的覆盖)
    sim.status.project = next.project
    sim.status.current_chapter = null
    sim.status.done_fragments = 0
    sim.status.total_fragments = toRun.reduce((s, c) => s + c.sentence_count, 0)
    sim.status.chapters = []
    pushLog('info', `开始合成「${next.project}」,待生成 ${sim.status.total_fragments} 条`)
    emit({ type: 'job_state', state: 'running' })
    return
  }

  const act = sim.active
  if (!act.current) {
    const next = act.remaining.shift()
    if (!next) {
      // 本工程跑完,下一 tick 取队列中的下一个工程
      sim.active = null
      sim.status.current_chapter = null
      return
    }
    act.current = { chapter: next.chapter, total: next.total, done: 0, failed: 0 }
    sim.status.current_chapter = next.chapter
    const cp: ChapterProgress = { name: next.chapter, total: next.total, done: 0, failed: 0, status: 'synthesizing' }
    sim.status.chapters = [...(sim.status.chapters?.filter((c) => c.name !== next.chapter) || []), cp]
    emit({ type: 'chapter_started', chapter: next.chapter, total: next.total, remaining: next.total })
    return
  }

  // 逐条推进(约 500ms 一条,模拟片段完成粒度)
  const cur = act.current
  cur.done += 1
  sim.status.done_fragments = (sim.status.done_fragments || 0) + 1
  const cp = sim.status.chapters?.find((c) => c.name === cur.chapter)
  if (cp) cp.done = cur.done
  emit({ type: 'fragment_done', chapter: cur.chapter, index: cur.done, elapsed_ms: 500, done: cur.done, total: cur.total })
  if (cur.done % 7 === 0) pushLog('debug', `已合成 ${cur.done}/${cur.total}`, cur.chapter, cur.done)

  if (cur.done >= cur.total) {
    if (cp) cp.status = 'concatenating'
    const out = `audio/${cur.chapter}.wav`
    emit({ type: 'chapter_concatenated', chapter: cur.chapter, output: out, duration_ms: cur.total * 900 })
    if (cp) cp.status = 'done'
    pushLog('info', `章节完成并拼接: ${cur.chapter}`, cur.chapter)
    const p = sim.status.project ? findProject(sim.status.project) : undefined
    const ch = p?.chapters.find((c) => c.name === cur.chapter)
    if (ch) {
      ch.status = 'done'
      ch.output = out
    }
    act.current = null
  }
}

/** 提交任务:运行中亦可入队(FIFO),不覆盖正在执行的工程 */
function startSim(projectName: string, chapters: string[]) {
  const p = findProject(projectName)
  if (!p) return
  const scheduled = scheduledSet(projectName)
  const targets = chapters.length ? p.chapters.filter((c) => chapters.includes(c.name)) : p.chapters
  const toRun = targets.filter((c) => c.status !== 'done' && !scheduled.has(c.name))
  if (!toRun.length) {
    pushLog('info', `「${projectName}」没有需要生成的章节(已完成或已在队列中)`)
    return
  }
  sim.queue.push({ project: projectName, chapters: toRun.map((c) => c.name), count: toRun.length })
  updatePending()
  const unbound = rolesOf(p).filter((r) => r.status !== 'ok')
  if (unbound.length) {
    pushLog(
      'warn',
      `「${projectName}」有 ${unbound.length} 个角色未绑定或绑定失效,后端将在开跑前阻塞: ${unbound.map((r) => r.name).join('、')}`,
    )
  }
  pushLog('info', `已加入队列:「${projectName}」${toRun.length} 章`)

  if (sim.status.state !== 'running') {
    sim.status.state = 'running'
    emit({ type: 'job_state', state: 'running' })
  }
  if (!sim.timer) sim.timer = setInterval(tick, 500)
}

function pauseSim() {
  if (sim.status.state !== 'running') return
  sim.status.state = 'paused'
  pushLog('warn', '已暂停(当前片段跑完后停)')
  emit({ type: 'job_state', state: 'paused' })
}

function stopSim() {
  stopTimer()
  sim.active = null
  sim.queue = []
  sim.status = blankStatus('stopped')
  pushLog('warn', '已停止,清空未开始任务')
  emit({ type: 'job_state', state: 'stopped' })
}

function resetSim() {
  stopTimer()
  sim.active = null
  sim.queue = []
  sim.status = blankStatus('idle')
}

// --------------------------------------------------------------------------- //
// 路由分发
// --------------------------------------------------------------------------- //

/** 取 multipart 里 `files`(复数)字段的原始文件 */
function uploadedFiles(init?: RequestInit): File[] {
  const fd = init?.body instanceof FormData ? init.body : undefined
  if (!fd) return []
  return fd.getAll('files').filter((x): x is File => typeof x !== 'string')
}

/** 章节名 = 文件 basename 去掉 .json(与后端一致) */
function chapterNameFromFile(name: string): string {
  const base = name.split(/[\\/]/).pop() || name
  return base.replace(/\.json$/i, '')
}

function parsePath(path: string): { pathname: string; query: URLSearchParams } {
  const qIndex = path.indexOf('?')
  const pathname = qIndex >= 0 ? path.slice(0, qIndex) : path
  const query = new URLSearchParams(qIndex >= 0 ? path.slice(qIndex + 1) : '')
  return { pathname, query }
}

function browse(path: string): Response {
  const target = path || settings.workspace_root
  if (!target.startsWith('/') || target.includes('..')) {
    return errorResponse('invalid', `非法路径: ${target}`, 400)
  }
  const node = FS_TREE[target]
  if (!node) return errorResponse('not_found', `目录不存在或不在允许的卷内: ${target}`, 404)
  const parent = target === '/' ? null : target.replace(/\/[^/]+\/?$/, '') || '/'
  const data: FsBrowse = {
    path: target,
    parent,
    dirs: node.dirs.map((name) => ({ name, has_children: Boolean(FS_TREE[`${target}/${name}`]?.dirs.length) })),
    is_project: node.is_project,
  }
  return jsonResponse(data)
}

function probe(body: Record<string, unknown>): Response {
  const url = String(body.base_url || settings.providers[settings.active_provider]?.base_url || '')
  const t0 = perHint()
  // 用 URL 关键字演示四种结果;默认成功。
  if (/unreachable|不可达|offline/i.test(url)) {
    return jsonResponse({ ok: false, kind: 'unreachable', message: '后端不可达,请检查地址与网络', latency_ms: t0 } satisfies HealthResult)
  }
  if (/noload|未加载|model-not-loaded/i.test(url)) {
    return jsonResponse({ ok: false, kind: 'model_not_loaded', message: '后端已启动但模型未加载,请检查后端', latency_ms: t0 } satisfies HealthResult)
  }
  if (/bad|参数|invalid/i.test(url)) {
    return jsonResponse({ ok: false, kind: 'bad_request', message: '请求参数有误: prompt_audio 字段缺失', latency_ms: t0 } satisfies HealthResult)
  }
  if (/error|500|server/i.test(url)) {
    return jsonResponse({ ok: false, kind: 'server_error', message: '处理请求时发生内部错误: CUDA out of memory', latency_ms: t0 } satisfies HealthResult)
  }
  const sample = makeWav(1200, 440)
  const result: HealthResult = {
    ok: true,
    kind: 'ok',
    message: '合成成功',
    latency_ms: 2400,
    duration_ms: 1200,
    sample_rate: 22050,
    sample_audio: bytesToBase64(sample),
  }
  return jsonResponse(result)
}

function perHint(): number {
  return 300 + Math.floor(Math.random() * 400)
}

export function mockRequest(path: string, init?: RequestInit): Promise<Response> {
  return Promise.resolve(handle(path, init))
}

function bodyOf(init?: RequestInit): Record<string, unknown> {
  if (!init?.body || typeof init.body !== 'string') return {}
  try {
    return JSON.parse(init.body) as Record<string, unknown>
  } catch {
    return {}
  }
}

function handle(path: string, init?: RequestInit): Response {
  const method = (init?.method || 'GET').toUpperCase()
  const { pathname, query } = parsePath(path)
  const seg = pathname.split('/').filter(Boolean) // e.g. ['api','projects','三体','bindings']

  // /api/fs/browse
  if (method === 'GET' && pathname === '/api/fs/browse') {
    return browse(query.get('path') || '')
  }

  // /api/settings/providers
  if (method === 'GET' && pathname === '/api/settings/providers') {
    return jsonResponse(PROVIDERS)
  }

  // /api/settings
  if (pathname === '/api/settings') {
    if (method === 'GET') return jsonResponse(settings)
    if (method === 'PUT') {
      const patch = bodyOf(init) as Partial<Settings>
      settings = { ...settings, ...patch, providers: { ...settings.providers, ...(patch.providers || {}) } }
      return jsonResponse(settings)
    }
  }

  // /api/tts/probe
  if (method === 'POST' && pathname === '/api/tts/probe') {
    return probe(bodyOf(init))
  }

  // /api/projects
  if (pathname === '/api/projects') {
    if (method === 'GET') return jsonResponse(projects.map(toSummary))
    if (method === 'POST') {
      const body = bodyOf(init)
      const name = String(body.name || '').trim()
      if (!name) return errorResponse('invalid', '工程名不能为空', 400)
      if (findProject(name)) return errorResponse('invalid', `工程已存在: ${name}`, 409)
      const np: MockProject = {
        name,
        path: `${settings.workspace_root}/${name}`,
        chapters: [{ name: 'P01_扉页', sentence_count: 60, status: 'pending', output: null }],
        roles: ['旁白'],
        bindings: { version: 1, bindings: {} },
        manifestProvider: 'indextts2',
        stale_chapters: [],
      }
      projects.push(np)
      FS_TREE[np.path] = { is_project: true, dirs: ['json', 'process_audio', 'audio'] }
      return jsonResponse(toDetail(np), 201)
    }
  }

  // /api/projects/upload —— 上传建工程(必须在 {name} 分支之前判定)
  if (method === 'POST' && pathname === '/api/projects/upload') {
    const fd = init?.body instanceof FormData ? init.body : undefined
    const name = String(fd?.get('name') || '').trim()
    if (!name) return errorResponse('invalid', '工程名不能为空', 400)
    if (findProject(name)) return errorResponse('invalid', `工程已存在: ${name}`, 409)
    const files = uploadedFiles(init)
    if (files.length === 0) return errorResponse('invalid', '未提供任何 .json 文件', 400)
    const bad = files.find((f) => !f.name.toLowerCase().endsWith('.json'))
    if (bad) return errorResponse('invalid', `只允许 .json 文件:${bad.name}`, 400)
    const np: MockProject = {
      name,
      path: `${settings.workspace_root}/${name}`,
      chapters: files.map((f) => ({
        name: chapterNameFromFile(f.name),
        sentence_count: 60,
        status: 'pending' as const,
        output: null,
      })),
      roles: ['旁白'],
      bindings: { version: 1, bindings: {} },
      manifestProvider: 'indextts2',
      stale_chapters: [],
    }
    projects.push(np)
    FS_TREE[np.path] = { is_project: true, dirs: ['json', 'process_audio', 'audio'] }
    return jsonResponse(toDetail(np))
  }

  if (seg[0] === 'api' && seg[1] === 'projects' && seg[2]) {
    const name = decodeURIComponent(seg[2])
    const p = findProject(name)
    // /api/projects/{name}
    if (seg.length === 3) {
      if (!p) return errorResponse('not_found', `工程不存在: ${name}`, 404)
      if (method === 'DELETE') {
        if (sim.status.state === 'running' && sim.status.project === name) {
          return errorResponse('invalid', `工程正在合成,无法删除: ${name}`, 409)
        }
        const idx = projects.findIndex((x) => x.name === name)
        if (idx >= 0) projects.splice(idx, 1)
        delete FS_TREE[p.path]
        const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15)
        return jsonResponse({ deleted: name, trashed_to: `.trash/${name}-${stamp}` })
      }
      return jsonResponse(toDetail(p))
    }
    // /api/projects/{name}/bindings
    if (seg.length === 4 && seg[3] === 'bindings') {
      if (!p) return errorResponse('not_found', `工程不存在: ${name}`, 404)
      if (method === 'GET') return jsonResponse(p.bindings)
      if (method === 'PUT') {
        const body = bodyOf(init) as unknown as BindingsFile
        p.bindings = { version: body.version ?? 1, bindings: body.bindings || {} }
        return jsonResponse(p.bindings)
      }
    }
    // /api/projects/{name}/chapters —— 追加/替换章节(同名覆盖)
    if (seg.length === 4 && seg[3] === 'chapters' && method === 'POST') {
      if (!p) return errorResponse('not_found', `工程不存在: ${name}`, 404)
      const files = uploadedFiles(init)
      if (files.length === 0) return errorResponse('invalid', '未提供任何 .json 文件', 400)
      const bad = files.find((f) => !f.name.toLowerCase().endsWith('.json'))
      if (bad) return errorResponse('invalid', `只允许 .json 文件:${bad.name}`, 400)
      const stale = [...p.stale_chapters]
      for (const f of files) {
        const cn = chapterNameFromFile(f.name)
        const existing = p.chapters.find((c) => c.name === cn)
        if (existing) {
          // 同名覆盖:重置状态;已生成的旧音频失效(后端靠 json_sha256 变化自动作废)
          existing.status = 'pending'
          existing.output = null
          if (!stale.some((s) => s.name === cn)) stale.push({ name: cn, stale_fragments: [1] })
        } else {
          p.chapters.push({ name: cn, sentence_count: 60, status: 'pending', output: null })
        }
      }
      p.stale_chapters = stale
      return jsonResponse(toDetail(p))
    }

    // /api/projects/{name}/chapters/{chapter} —— 删除章节(成品移入 audio/.stale/)
    if (seg.length === 5 && seg[3] === 'chapters' && method === 'DELETE') {
      if (!p) return errorResponse('not_found', `工程不存在: ${name}`, 404)
      const chapter = decodeURIComponent(seg[4])
      const ci = p.chapters.findIndex((c) => c.name === chapter)
      if (ci < 0) return errorResponse('not_found', `章节不存在: ${chapter}`, 404)
      if (sim.status.state === 'running' && sim.status.current_chapter === chapter) {
        return errorResponse('invalid', `章节正在合成,无法删除: ${chapter}`, 409)
      }
      p.chapters.splice(ci, 1)
      p.stale_chapters = p.stale_chapters.filter((s) => s.name !== chapter)
      return jsonResponse(toDetail(p))
    }

    // /api/projects/{name}/chapters/{ch}/audio
    if (seg.length === 6 && seg[3] === 'chapters' && seg[5] === 'audio') {
      if (!p) return errorResponse('not_found', `工程不存在: ${name}`, 404)
      return wavResponse(1500, 392)
    }
  }

  // /api/voices
  if (pathname === '/api/voices') {
    if (method === 'GET') return jsonResponse(voices)
    if (method === 'POST') {
      // multipart 在 mock 下由 client 组 FormData;这里只读 name/prompt_text
      const fd = init?.body instanceof FormData ? init.body : undefined
      const name = String(fd?.get('name') || '').trim()
      if (!name) return errorResponse('invalid', '音色名不能为空', 400)
      if (voices.some((v) => v.name === name)) return errorResponse('invalid', `音色已存在: ${name}`, 409)
      const entry: VoiceEntry = {
        name,
        path: `/voices/${name}`,
        main_audio: `/voices/${name}/1.wav`,
        prompt_text: (fd?.get('prompt_text') as string) || null,
        emo_refs: {},
        is_normalized: true,
        warnings: [],
      }
      voices.push(entry)
      return jsonResponse(entry, 201)
    }
  }

  if (seg[0] === 'api' && seg[1] === 'voices' && seg[2]) {
    const name = decodeURIComponent(seg[2])
    const idx = voices.findIndex((v) => v.name === name)
    // /api/voices/{name}
    if (seg.length === 3) {
      if (idx < 0) return errorResponse('not_found', `音色不存在: ${name}`, 404)
      if (method === 'PUT') {
        const fd = init?.body instanceof FormData ? init.body : undefined
        const newName = (fd?.get('rename') as string) || name
        if (newName !== name && voices.some((v) => v.name === newName)) {
          return errorResponse('invalid', `音色名已存在: ${newName}`, 409)
        }
        const entry = voices[idx]
        if (fd?.get('prompt_text') != null) entry.prompt_text = String(fd.get('prompt_text'))
        if (newName !== name) {
          entry.name = newName
          entry.path = `/voices/${newName}`
          entry.main_audio = `/voices/${newName}/1.wav`
        }
        return jsonResponse(entry)
      }
      if (method === 'DELETE') {
        voices.splice(idx, 1)
        return new Response(null, { status: 204 })
      }
    }
    // /api/voices/{name}/preview
    if (seg.length === 4 && seg[3] === 'preview') {
      if (idx < 0) return errorResponse('not_found', `音色不存在: ${name}`, 404)
      return wavResponse(1800, 523)
    }
  }

  // /api/jobs
  if (pathname === '/api/jobs' && method === 'POST') {
    const body = bodyOf(init)
    const project = String(body.project || '')
    const chapters = Array.isArray(body.chapters) ? (body.chapters as string[]) : []
    if (!findProject(project)) return errorResponse('not_found', `工程不存在: ${project}`, 404)
    startSim(project, chapters)
    return jsonResponse(sim.status)
  }
  if (pathname === '/api/jobs/state' && method === 'GET') {
    return jsonResponse(sim.status)
  }
  if (pathname === '/api/jobs/pause' && method === 'POST') {
    pauseSim()
    return jsonResponse(sim.status)
  }
  if (pathname === '/api/jobs/stop' && method === 'POST') {
    stopSim()
    return jsonResponse(sim.status)
  }

  return errorResponse('not_found', `mock 未覆盖的接口: ${method} ${pathname}`, 404)
}

// --------------------------------------------------------------------------- //
// 事件订阅(模拟 SSE)
// --------------------------------------------------------------------------- //

function mockSubscribe(handlers: SseHandlers): { close: () => void } {
  const listener = (payload: ProgressEvent) => handlers.onEvent(payload, payload.type)
  listeners.add(listener)
  handlers.onOpen?.()
  // 立即补一帧当前状态,模拟「先 state 后增量」的重连语义
  queueMicrotask(() => handlers.onEvent({ type: 'job_state', state: sim.status.state }, 'job_state'))
  return {
    close: () => {
      listeners.delete(listener)
    },
  }
}

// --------------------------------------------------------------------------- //
// 安装
// --------------------------------------------------------------------------- //

/** 把 mock 桥接挂到 window,client.ts 会惰性读取它。 */
export function installMock(): void {
  const bridge: MockBridge = {
    request: mockRequest,
    subscribe: mockSubscribe,
  }
  ;(window as unknown as { __AUDIOBOOK_MOCK__?: MockBridge }).__AUDIOBOOK_MOCK__ = bridge
  // 方便手动回归:控制台可调用 window.__AUDIOBOOK_MOCK_RESET__()
  ;(window as unknown as { __AUDIOBOOK_MOCK_RESET__?: () => void }).__AUDIOBOOK_MOCK_RESET__ = resetSim
  // eslint-disable-next-line no-console
  console.info('[mock] 已启用内置 mock。所有 /api 请求由 src/api/mock.ts 应答。')
}
