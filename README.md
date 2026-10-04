# 有声书合成客户端

一个**零算力**的有声书合成客户端:跑在 NAS 的 Docker 容器里,局域网浏览器访问,
把章节 JSON 按**逐条**方式发给远端 TTS 后端(IndexTTS)合成,
用 **FFmpeg-free** 的纯 Python 拼接成章节成品,靠 **manifest** 支持任意中断续传。

> 本仓库是**纯客户端** —— 不含推理算力,不部署模型。TTS 引擎在远端,通过 HTTP 调用。

---

## 特性

| # | 能力 | 说明 |
|---|---|---|
| 1 | 工程管理 | 新建(路径导入 **或** 浏览器上传 JSON)/ 扫描既有工作区 / 打开 |
| 2 | 章节导入 | 给已有工程**追加/替换章节**(同名覆盖) |
| 3 | 删除 | 删除工程 / 删除章节 —— **均可恢复**,不真删(工程入 `.trash/`,成品入 `audio/.stale/`) |
| 4 | 章节解析 | 读取章节 JSON,跨章聚合**角色列表**(去重) |
| 5 | 音色库 | 列表 / 新建 / 重命名 / 编辑 / 删除;解析既有音色目录结构 |
| 6 | 角色绑定 | 每个角色一个**可搜索下拉框**绑定音色;多角色可共用同一音色 |
| 7 | TTS 连接 | URL 配置 + 测试连接 + 分层重试 + 超时 + 参考音频 base64 缓存 |
| 8 | 音频预处理 | 真实格式探测(识破伪 wav)/ 转码 / 只降不升重采样 / 裁剪 ≤15s |
| 9 | 合成引擎 | 进程级**串行队列**、逐条合成、SSE 实时进度、开始/暂停/停止 |
| 10 | 断点续传 | 扫描 `audio/` 与 `process_audio/`,**只补未完成部分** |
| 11 | 章节拼接 | 按 `delay` 插静音 → 拼接 → 章节成品 → 成功后清理 `process_audio/` |

**明确不做**:TTS 引擎部署/训练、文本预处理(分句/角色识别/情感标注)、波形编辑、多用户与鉴权、
全书合并为单文件。详见 [`doc/requirements.md`](doc/requirements.md)。

---

## 快速开始

### 方式 A:纯前端 mock(零依赖,看界面)

```bash
cd src/web
npm install
npm run dev:mock          # → http://localhost:5173
```

mock 覆盖全部 15 条路由,顶栏显示「MOCK 模式」角标,进度事件由定时器模拟。
适合先看交互,不需要后端、不需要 Python 环境。

### 方式 B:本地跑后端 + 前端

**1) 准备本地数据目录**(模拟容器三卷):

```
dev-data/
├── workspace/     # ↔ 容器 /workspace   书籍工程
├── voices/        # ↔ 容器 /voices      音色库(把 reference/audio/ 的内容放进来)
└── config/        # ↔ 容器 /config      config.json
```

**2) 起后端**:

```bash
uv sync
uv run uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000
```

**3) 起前端**(另开一个终端):

```bash
cd src/web
npm install
npm run dev               # dev server 把 /api 代理到 VITE_API_PROXY(默认 http://localhost:8000)
```

**4) 或让 FastAPI 直接托管前端产物**:

```bash
cd src/web && npm run build     # 产出 src/web/dist/
# 之后只跑后端,访问 http://localhost:8000 即可(SPA 回落已处理)
```

### 方式 C:Docker(推荐,NAS 形态)

```bash
docker compose up -d --build
# → http://<NAS_IP>:8301
```

---

## 配置

**三层覆盖**:`代码内默认值` < `config.json` < `环境变量`。
本地用相对路径、容器用绝对路径,**同一套代码零改动**。

### 环境变量(前缀 `AUDIOBOOK_`)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIOBOOK_WORKSPACE` | `/workspace` | 工作区根(存放多个书籍工程) |
| `AUDIOBOOK_VOICES` | `/voices` | 音色库根 |
| `AUDIOBOOK_CONFIG` | `/config` | 配置目录(内含 `config.json`) |
| `AUDIOBOOK_HOST` | `0.0.0.0` | 监听地址 |
| `AUDIOBOOK_PORT` | `8000` | 监听端口 |
| `AUDIOBOOK_LOG_LEVEL` | `INFO` | 日志级别 |
| `AUDIOBOOK_PROVIDER` | `indextts2` | 激活的 TTS provider |
| `AUDIOBOOK_CONCURRENCY` | `1` | 并发数(**后端无并发保护,保持 1**) |

本地开发示例:

```bash
AUDIOBOOK_WORKSPACE=./dev-data/workspace \
AUDIOBOOK_VOICES=./dev-data/voices \
AUDIOBOOK_CONFIG=./dev-data/config \
uv run uvicorn src.api.app:app --reload --port 8000
```

### 目录规范

| 容器路径 | 内容 |
|---|---|
| `/workspace` | `<书名>/{json/, process_audio/, audio/}` |
| `/voices` | 音色库(每个音色一个目录,目录名即音色名) |
| `/config` | `config.json` |

> 工程内固定三目录:`json/` 原始章节、`process_audio/` 片段中间产物(章节完成后自动清理)、
> `audio/` 章节成品。

---

## 数据准备

以下内容**不在版本控制内**(见 `.gitignore`),需自行提供:

| 项 | 说明 |
|---|---|
| `reference/audio/` | 音色库素材(~59MB 二进制 wav),部署时放到 `/voices` 卷 |
| `book-json/` | 章节 JSON 样例;其中 `P01_扉页.json` 是测试基准(103 条) |
| `deploy/` | 部署包(镜像 tar + NAS 用 compose + 速查 README) |

---

## 架构

**第一原则:解耦 TTS 与音频。** 后端引擎后期可能更换,因此用**依赖方向**把这条锁死:

```
                      ┌──────────────┐
  浏览器 ──HTTP/SSE──▶ │    api/      │  HTTP 层,只做协议转换
                      └──────┬───────┘
                             ▼
                      ┌──────────────┐
                      │ synthesis/   │  编排层:唯一同时知道 tts 与 audio
                      └──┬────┬────┬─┘
            ┌────────────┘    │    └────────────┐
            ▼                 ▼                 ▼
      ┌──────────┐      ┌──────────┐      ┌──────────────┐
      │  tts/    │  ✗   │ audio/   │      │ project/     │
      │ 纯协议   │◀────▶│ 纯音频   │      │ voice/       │
      └──────────┘ 禁止  └──────────┘      └──────────────┘
                   互相 import
```

### 模块职责

| 模块 | 职责 | 明确**不**负责 |
|---|---|---|
| `src/audio/` | 格式探测、转码、重采样、裁剪、静音插入、拼接、base64 编解码 | 不知道「音色」「工程」「后端」 |
| `src/tts/` | 后端契约实现、字段映射、错误码分层、退避重试、能力自描述 | 不读文件、不做格式归一化 |
| `src/voice/` | 音色库 CRUD、目录解析、入库转码 | 不关心哪个工程在用 |
| `src/project/` | 工作区扫描、工程/章节模型、角色聚合、绑定持久化、manifest 台账 | 不合成、不调后端 |
| `src/synthesis/` | 串行队列、逐条流水线、进度事件、暂停/停止、拼接与清理 | 不知道 HTTP、不知道 FFmpeg |
| `src/api/` | FastAPI 路由、出入参校验、SSE 推送、卷内路径浏览 | 无业务逻辑 |
| `src/web/` | Vue3 界面 | 与后端**零代码共享**(R6) |

### 扩展性承诺

> **加一个新 TTS 后端 = 加一个 `src/tts/<name>_client.py` + 在 `registry.py` 注册一行。**
> `audio/`、`synthesis/`、`api/`、`web/` **零改动**。

由三条硬规则保障(`R1` audio/tts 互不 import、`R3` tts/ 只收字节不碰文件系统、
`R7` 格式归一化归属 synthesis/)。前端参数表单由后端 `param_schema` **动态渲染**,
代码中不出现任何硬编码参数名。

---

## 开发

### 技术栈

- **后端**:Python ≥3.11 + FastAPI + uvicorn,依赖用 [uv](https://docs.astral.sh/uv/) 管理
- **前端**:Vue3 + TypeScript + Vite + Pinia + Element Plus
- **运行时依赖**:`soundfile`(自带 libsndfile)/ `numpy` / `soxr` —— 全为 pip wheel,
  **核心路径不需要系统 FFmpeg**

### 测试

```bash
uv run pytest                 # 默认:只跑离线测试(不需要网络/后端)
uv run pytest -m integration  # 显式:才碰真实后端(公网、GPU,请节制)
uv run ruff check .
```

离线测试直接用仓库内的真实素材作 fixture(44.1kHz 立体声 / 16kHz 单声道 / 伪 MP3 / 情感参考集 /
分离产物),天然覆盖各类异常输入。

> ⚠️ **联调节制**:测试后端在公网、无鉴权、GPU 推理。集成测试用**极短文本**、严格串行、不刷量。

### 容器化硬约束(开发第一天就遵守)

这些 bug 的共同特征:**开发机上不会暴露,进容器才炸**。

1. 路径全部来自配置,**零硬编码**
2. 配置支持环境变量覆盖
3. 所有文件 IO 显式 `encoding="utf-8"`(音色名**全中文**,locale 不对直接炸)
4. 监听 `0.0.0.0`,端口可配
5. 日志写 stdout
6. 不依赖系统 FFmpeg
7. 不依赖本地时区
8. 路径选择器是**服务端卷内路径浏览器**(容器内没有系统文件对话框)
9. manifest / 配置 / 前端**只存相对路径**(换挂载点后断点续传才不会残废)

---

## API 概览

前端与后端只通过 HTTP/SSE 通信(架构硬规则 R6:零代码共享)。

| 分组 | 路由 |
|---|---|
| 工程 | `GET/POST /api/projects`、`POST /api/projects/upload`、`GET /api/projects/{name}`、`POST /api/projects/{name}/chapters`、`DELETE /api/projects/{name}`、`DELETE /api/projects/{name}/chapters/{chapter}`、`GET /api/projects/{name}/chapters/{chapter}/audio` |
| 绑定 | `GET/PUT /api/projects/{name}/bindings` |
| 音色 | `GET/POST /api/voices`、`PUT/DELETE /api/voices/{name}`、`GET /api/voices/{name}/preview` |
| 设置 | `GET/PUT /api/settings`、`GET /api/settings/providers` |
| TTS | `POST /api/tts/probe` |
| 任务 | `POST /api/jobs`、`GET /api/jobs/state`、`POST /api/jobs/pause`、`POST /api/jobs/stop`、`GET /api/jobs/events`(SSE) |
| 文件 | `GET /api/fs/browse`(卷内路径浏览,挡住 `../` 穿越与符号链接逃逸) |

完整契约见 [`doc/api/design.md`](doc/api/design.md)。

---

## 安全须知

实测发现(见 [`doc/deploy/deployment.md`](doc/deploy/deployment.md) §5):

| 问题 | 说明 |
|---|---|
| 默认 TTS 后端在**公网** | 明文 HTTP,**无鉴权、无 TLS** —— 任何知道地址的人都能白嫖 GPU |

**本次决策**:不改后端,仅记录风险;客户端配置中**预留鉴权字段**(`AuthConfig`),以后加不返工。

部署建议:

1. 客户端**不要直接暴露公网**,仅局域网访问,或走 NAS 自带的 VPN / 反代 + 鉴权
2. 参考音频含版权内容,注意 NAS 共享权限
3. 若用 Nginx 反代,**SSE 必须 `proxy_buffering off`**,否则进度事件被攒着不发

---

## 文档索引

文档集中在 [`doc/`](doc/README.md),与 `src/` 严格镜像。

| 文档 | 内容 |
|---|---|
| [`requirements.md`](doc/requirements.md) | 需求与边界清单(定稿) |
| [`findings.md`](doc/findings.md) | 实测结论:后端契约、素材现状、流量测算 |
| [`architecture.md`](doc/architecture.md) | 分层、依赖硬规则、模块划分、目录树 |
| [`dev-workflow.md`](doc/dev-workflow.md) | 开发工作流:环境、容器化约束、M0~M7 里程碑 |
| [`audio/`](doc/audio/design.md) · [`tts/`](doc/tts/design.md) · [`voice/`](doc/voice/design.md) | 各模块设计 |
| [`project/`](doc/project/design.md) | 工程模块 + **续传 manifest 协议** |
| [`synthesis/`](doc/synthesis/design.md) · [`api/`](doc/api/design.md) · [`web/`](doc/web/design.md) | 编排层 / HTTP 层 / 前端 |
| [`deploy/deployment.md`](doc/deploy/deployment.md) | Docker / NAS 部署与验收清单 |

前端另有独立说明:[`src/web/README.md`](src/web/README.md)。

---

## 部署(NAS)

```bash
docker compose up -d --build      # 仓库根的 compose 是开发用的(带 build:)
```

**NAS 部署第一坑 —— PUID / PGID**:必须与 `/workspace`、`/voices`、`/config` 三个共享文件夹的
**所有者**一致,否则容器写 `process_audio/`、`audio/` 会 `Permission denied`。

排障提示:`docker exec` 默认以 **root** 运行(镜像未设 `USER`),验证卷权限必须带 `-u 1000`,
否则测的是 root 的可写性,证明不了应用能不能写。

完整步骤与 11 项验收清单见 [`doc/deploy/deployment.md`](doc/deploy/deployment.md)。