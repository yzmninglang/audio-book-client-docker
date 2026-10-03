# 开发工作流

状态:【已确认】 · 适用对象:实现阶段

> 前置:[requirements.md](requirements.md) 边界已确认、[architecture.md](architecture.md) 模块架构已确认、
> 各模块设计文档已落盘。**当前状态:未写任何代码。**

## 0. 硬性开工规则

| 规则 | 出处 |
|---|---|
| 严禁擅自扩展边界;一旦发现需求超界,**立即停下重新对齐** | 项目 CLAUDE.md |
| 新增/重构模块时,**同步更新 `doc/` 对应镜像层级**的文档 | 项目 CLAUDE.md |
| 代码一律放 `src/`,文档一律放 `doc/`,不散落 | 项目 CLAUDE.md |
| Python 环境/依赖一律用 **uv** 管理 | 用户全局 CLAUDE.md |
| `server/ui-api.py` **只读,不修改** | [requirements.md](requirements.md) Out-of-Scope |
| 不读、不改 `prompt.md` | 用户全局 CLAUDE.md |
| 测试用真实后端时**必须节制**(公网、无鉴权、GPU 资源) | [findings.md §7](findings.md#7-安全发现) |

## 1. 环境准备

### 1.1 后端依赖

```bash
uv init --name audiobook-client
uv add fastapi "uvicorn[standard]" pydantic httpx \
       soundfile numpy soxr python-multipart
uv add --dev pytest ruff mypy
```

> `soundfile`(自带 libsndfile)+ `numpy` + `soxr` 都是 pip wheel,**无系统依赖** ——
> 这是「核心路径不需要 FFmpeg」的前提,别引入需要系统 FFmpeg 的库(如裸 `pydub`)。

### 1.2 前端

```bash
npm create vite@latest src/web -- --template vue-ts
cd src/web && npm i pinia element-plus
```

### 1.3 本地数据布局(模拟容器三卷)

**不要在代码里硬编码本地路径。** 本地开发时用环境变量指到 `dev-data/`:

```
dev-data/
├── workspace/          # ↔ 容器 /workspace
├── voices/             # ↔ 容器 /voices   (放 reference/audio 的内容)
└── config/             # ↔ 容器 /config
```

```bash
export AUDIOBOOK_WORKSPACE=./dev-data/workspace
export AUDIOBOOK_VOICES=./dev-data/voices
export AUDIOBOOK_CONFIG=./dev-data/config
uv run uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000
```

### 1.4 配置三层覆盖(必须实现)

```
默认值(代码内)  <  config.json  <  环境变量
```

开发用本地路径,部署用容器路径,**同一套代码零改动**。

## 2. 容器化约束(第一天就遵守,别拖到最后)

> 这些 bug 的共同特征:**开发机上不会暴露,进容器才炸**。完整说明见 [deploy/deployment.md](deploy/deployment.md)。

| # | 约束 | 违反后果 |
|---|---|---|
| 1 | **路径全部来自配置,零硬编码** | 容器里 `/mnt/i/...` 不存在 |
| 2 | **配置支持环境变量覆盖** | 本地与容器无法共用代码 |
| 3 | **所有文件 IO 显式 `encoding="utf-8"`** | 容器 locale 缺失 → 37 个中文音色名**直接炸** |
| 4 | **监听 `0.0.0.0`,端口可配** | 容器外访问不到 |
| 5 | **日志写 stdout**(可选外加落盘) | `docker logs` 看不到任何东西 |
| 6 | **不依赖系统 FFmpeg** | 镜像得塞 FFmpeg 或直接跑不起来 |
| 7 | **不依赖本地时区/系统时间格式** | 容器默认 UTC,时间戳错乱 |
| 8 | **路径选择器是服务端风格**(不用系统文件对话框) | 上容器要重写前端交互 |
| 9 | **manifest/配置/前端只存相对路径** | 换挂载点后全部失效(断点续传直接残废) |

> 第 9 条最易漏:manifest 里存 `/workspace/三体/process_audio/...` 就废了。**存相对工程根的路径**。

## 3. 实施顺序与里程碑

自底向上,每层可独立验证。**不要跳层**——下层不稳定,上层的 bug 无法归因。

```
M0 地基 → M1 音频 → M2 TTS ─┐
                            ├→ M3 领域 → M4 编排 → M5 接口 → M6 前端 → M7 容器化
              ▣ M1.5 提前容器冒烟(插在 M2 之后)
```

### M0 — 地基

| 项 | 内容 |
|---|---|
| 交付 | `pyproject.toml`、`src/{config.py, errors.py, models.py}` |
| 完成判据 | ① 配置三层覆盖可用且有单测<br>② `RetryableError` / `FatalError` 定义完整<br>③ `models.py` 含 `Sentence`/`Chapter`/`VoiceRef`/`EmotionSpec`/`TTSRequest`/`TTSResult`/`AudioFormat`<br>④ 所有文件 IO 显式 utf-8 |
| 验证 | `uv run pytest` |

### M1 — 音频层

| 项 | 内容 |
|---|---|
| 交付 | `src/audio/{probe,convert,segment,codec}.py` |
| 完成判据 | ① `probe()` 能识破伪 wav(实为 MP3)<br>② `to_reference_audio()` 对 16k 输入**不升采样**<br>③ 拼接结果时长 = Σ片段 + Σdelay<br>④ 全程零 FFmpeg 依赖 |
| 验证 | **离线单测**,fixture 直接取 `reference/audio/` 的真实素材(44.1k stereo / 16k mono / 伪 MP3) |

> M1 是唯一可以**完全离线**完成的层,而且能 100% 覆盖。把它做扎实,后面省很多事。

### M2 — TTS 层

| 项 | 内容 |
|---|---|
| 交付 | `src/tts/{base,capabilities,registry,indextts_client,retry,probe}.py` |
| 完成判据 | ① 对真实后端 `probe()` 返回 `ok` + 延迟<br>② 能合成一条并写出可播放的 wav<br>③ 400 不重试 / 500 退避重试,有 mock 单测<br>④ `param_schema` 覆盖 9 个参数,键名用 `max_text_tokens_per_sentence`<br>⑤ `capabilities.returns_canonical=True` |
| 验证 | **集成测试**,标记 `@pytest.mark.integration`,默认跳过 |

> ⚠️ **联调节制**:后端在公网、无鉴权、GPU 推理。集成测试用**极短文本**(如 `"测试。"`),
> 严格串行,不并发,不刷量。

### ▣ M1.5 — 提前容器冒烟(别跳过)

| 项 | 内容 |
|---|---|
| 时机 | M2 完成后,已有「一条端到端通路」时 |
| 交付 | 初步 `Dockerfile` + `docker-compose.yml` |
| **目的** | **尽早撞出容器侧的问题**:locale、`soundfile` wheel、卷权限、SSE 反代 |
| 理由 | 此时代码量小,返工便宜;拖到最后再撞,可能要回头改一堆文件读写 |

### M3 — 领域层

| 项 | 内容 |
|---|---|
| 交付 | `src/voice/{library,entry}.py`、`src/project/{workspace,project,chapter,binding,manifest}.py` |
| 完成判据 | ① 扫描 `reference/audio` 得到 **37 个音色**,正确识别 `汪淼` 的情感参考集、**忽略** `output_spleeter`<br>② 解析 `P01_扉页.json` 得 103 条 + 角色聚合<br>③ 绑定读写正确<br>④ manifest 原子写 + 对账(含 provider 检查)可用 |
| 验证 | 用真实素材的离线单测 |

### M4 — 编排层

| 项 | 内容 |
|---|---|
| 交付 | `src/synthesis/{pipeline,chapter_runner,queue,events}.py` |
| 完成判据 | ① 跑完 `P01_扉页` 一章 → `audio/` 出成品 → `process_audio/` 被清理<br>② **中途 `kill -9` → 重开 → 只补未完成部分**(必做)<br>③ 单条失败不中断整章<br>④ 未绑定角色在**开跑前**就报错,而非跑到一半 |
| 验证 | 端到端 + 破坏性测试 |

### M5 — 接口层

| 项 | 内容 |
|---|---|
| 交付 | `src/api/{app,schemas}.py`、`src/api/routes/*.py` |
| 完成判据 | ① 路由齐全(含 `/api/fs/browse`、`/api/settings/providers`)<br>② SSE 能实时推事件<br>③ **`fs/browse` 能挡住 `../` 路径穿越与符号链接逃逸**<br>④ 音频试听接口返回字节流而非 base64 |
| 验证 | HTTP 手测 + **路径穿越攻击测试** |

### M6 — 前端

| 项 | 内容 |
|---|---|
| 交付 | `src/web/`(5 个页面 + `PathBrowser`/`VoiceSelect`/`ProgressPanel`) |
| 完成判据 | ① 五个页面可用<br>② 角色绑定表**可搜索**<br>③ 参数表单由 `param_schema` **动态渲染**(不硬编码 temperature 等)<br>④ 页面刷新不影响后端任务 |
| 验证 | 手动走一遍 [requirements.md §8](requirements.md#8-验收要点从需求反推) 的 5 条验收要点 |

### M7 — 容器化与部署

| 项 | 内容 |
|---|---|
| 交付 | 正式 `Dockerfile`(多阶段)+ `docker-compose.yml` + 入口脚本(PUID/PGID) |
| 完成判据 | [deploy/deployment.md §7 验收清单](deploy/deployment.md#7-部署验收清单) **全部通过** |

## 4. 测试策略

### 4.1 分层

| 层 | 方式 | 需要网络? | 需要后端? |
|---|---|---|---|
| M0/M1 | 离线单测 | ❌ | ❌ |
| M3 | 离线单测(真实素材 fixture) | ❌ | ❌ |
| M2/M4 | 集成测试(标记 `integration`,默认跳过) | ✅ | ✅ |
| M5/M6 | HTTP 手测 + 手动走查 | ✅ | ✅ |

```
pytest                      # 默认:只跑离线测试
pytest -m integration       # 显式:才碰真实后端
```

### 4.2 离线 fixture 来源(不用造假数据)

直接用仓库里的真实素材,它们天然覆盖了所有异常:

| fixture | 覆盖的场景 |
|---|---|
| `reference/audio/丁仪/1.wav` | 44.1kHz 立体声(需降采样) |
| `reference/audio/标准女声/1.wav` | 16kHz 单声道(**不得升采样**) |
| `reference/audio/陈平安1/1.wav` | **伪 wav(实为 MP3)** |
| `reference/audio/汪淼/` | 情感参考集(6 组) |
| `reference/audio/男旁白2/output_spleeter/` | 分离产物(必须忽略) |
| `book-json/P01_扉页.json` | 103 条、全零 emo_vector |

### 4.3 必做的负面/破坏性测试

正面路径谁都会过,这些才是会翻车的地方:

| # | 测试 | 期望 |
|---|---|---|
| 1 | 合成中途 `kill -9`,重开点「全部开始」 | 只补未完成部分,**不重做已完成章节** |
| 2 | 中途修改章节 JSON 后重跑 | 该章片段作废重生成(hash 不匹配) |
| 3 | `/api/fs/browse?path=../../etc` | 被拒绝 |
| 4 | 音色名为 `../evil` 或含 `/` | 被拒绝 |
| 5 | `emo_vector` 之和 = 1.2 | 缩放到 0.8 并告警,不报 400 |
| 6 | 音频文件只有半个(模拟半截写入) | 被判为未完成,重新生成 |
| 7 | 角色未绑定音色 | **开跑前**报错,不跑到一半 |
| 8 | 中文音色名全流程 | 无乱码 |
| 9 | 8 个片段格式不同(模拟新后端) | 归一化后拼接成功 |

## 5. 文档同步规则

| 时机 | 动作 |
|---|---|
| 发现设计与实现不符 | **先改 `doc/`,再改代码** —— 文档是决策记录,滞后就失去价值 |
| 新增模块 | 在 `doc/` 镜像层级下同步创建设计文档 |
| 发现需求超界 | **停下**,向用户重新对齐,不得擅自实现 |
| 推翻某个既往判断 | 在 [findings.md](findings.md) 的「更正」小节追加记录(**像之前那三条一样**) |

## 6. 里程碑检查点

每到一个里程碑,停下自检:

- [ ] 本层完成判据是否**逐条**达成(不是「大概能用」)
- [ ] 离线测试是否全绿
- [ ] 是否有硬编码路径 / 缺 `encoding="utf-8"`(**§2 的 9 条**)
- [ ] `doc/` 是否已同步
- [ ] 是否有超界实现需要重新对齐

## 7. 当前起点

```
已完成:需求边界 · 模块架构 · 全部设计文档 · 后端连通性实测
未开始:任何代码

下一步:M0(地基)
```
