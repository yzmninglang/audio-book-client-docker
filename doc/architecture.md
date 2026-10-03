# 架构与模块划分

状态:【已确认】(2026-10-03)

## 1. 第一原则:解耦 TTS 与音频

需求方明确指出后端 TTS 引擎后期可能变动甚至更换,并把「TTS 连接模块与音频处理模块解耦」列为设计要求。
本架构用**依赖方向**把这条锁死:

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

## 2. 依赖硬规则(R1~R6)

| 规则 | 内容 | 违反后果 |
|---|---|---|
| **R1** | `audio/` 与 `tts/` **互相禁止 import** | 二者是兄弟模块,不是上下级 |
| **R2** | `audio/` 只做「字节 → 字节」,不认识「音色」「工程」「后端」等业务概念 | 音频层被业务污染后无法复用/测试 |
| **R3** | `tts/` 只接收/返回**字节**,不读任何文件路径、不碰文件系统 | 换引擎时被文件 IO 牵连 |
| **R4** | `synthesis/` 是**唯一交叉点**,也是唯一知道「一条片段如何诞生」的模块 | 交叉逻辑散落各处会失控 |
| **R5** | `api/` **不含业务逻辑**,只做 Pydantic 校验 + 调用 + 序列化 | 接口层变厚后无法改协议 |
| **R6** | `web/` 与后端**零代码共享**,只通过 HTTP/SSE 通信 | 前后端耦合导致部署复杂 |
| **R7** | 异构输出格式的**归一化归属 `synthesis/`**,不得放进 `tts/` | 放进 `tts/` 会迫使它 import `audio/`,破坏 R1 |

> R1 + R3 + R7 的收益:**新增一个 TTS 后端 = 加一个文件 + 注册一行**,
> 音频层、编排层、接口层、前端**一行不改**。详见 [tts/design.md §1.1](tts/design.md#11-可扩展性目标本模块的核心承诺)。

## 3. 模块职责表

| 模块 | 职责 | 明确**不**负责 |
|---|---|---|
| `audio/` | 真实格式探测(识破伪 wav)、转码、只降不升重采样、裁 15s、静音插入、拼接、base64 编解码 | 不知道「音色」「工程」「后端」 |
| `tts/` | 后端契约实现、字段映射、错误码分层、退避重试、测试连接探测、**能力与参数自描述**、**声明输出格式** | 不读文件、不知道音频从哪来、**不做格式归一化** |
| `voice/` | 音色库 CRUD、目录解析(主参考音频 / `pt.txt` / 情感参考集 / 分离产物识别)、入库转码 | 不关心哪个工程在用 |
| `project/` | 工作区扫描、工程与章节模型、角色聚合、角色→音色绑定持久化、**manifest 续传台账** | 不合成、不调后端 |
| `synthesis/` | 串行队列、逐条流水线、进度事件、暂停/停止、章节拼接与清理 | 不知道 HTTP,不知道 FFmpeg 细节 |
| `api/` | FastAPI 路由、出入参校验、SSE 推送、服务端路径浏览 | 无业务逻辑 |
| `web/` | Vue3 界面 | 不与后端共享代码 |

## 4. 目录树

```
audiobook/
├── CLAUDE.md
├── Dockerfile                     # 多阶段构建(amd64)
├── docker-compose.yml             # PUID/PGID + 三卷 + UTF-8
├── pyproject.toml                 # uv 管理
├── src/
│   ├── config.py                  # 设置模型 + 持久化
│   ├── errors.py                  # 异常体系:RetryableError / FatalError
│   ├── models.py                  # 领域模型:Sentence/Chapter/Voice/Binding/JobProgress
│   │
│   ├── audio/                     # 【音频处理模块】
│   │   ├── probe.py               #   真实格式探测
│   │   ├── convert.py             #   转码 → mono,只降不升,裁 15s
│   │   ├── segment.py             #   静音插入 + 片段拼接
│   │   └── codec.py               #   base64 编解码 + 体积估算
│   │
│   ├── tts/                       # 【TTS 接入模块】
│   │   ├── base.py                #   TTSProvider 抽象(Protocol)
│   │   ├── registry.py            #   provider 注册表 ← 新增后端只改这里 +1 行
│   │   ├── capabilities.py        #   能力自描述(TTSCapabilities / ParamSpec)
│   │   ├── indextts_client.py     #   /api/tts 实现 + 字段映射
│   │   ├── retry.py               #   分层重试策略
│   │   ├── probe.py               #   测试连接(极短真实合成)
│   │   └── <new>_client.py        #   ← 未来新增后端:加一个文件即可
│   │
│   ├── voice/                     # 【音色库模块】
│   │   ├── library.py             #   扫描/新建/重命名/删除
│   │   └── entry.py               #   单个音色:主音频、pt.txt、情感参考集、忽略分离产物
│   │
│   ├── project/                   # 【工程模块】
│   │   ├── workspace.py           #   工作区扫描 + 工程发现
│   │   ├── project.py             #   工程模型 + 章节清单
│   │   ├── chapter.py             #   章节 JSON 解析 + 角色聚合
│   │   ├── binding.py             #   角色→音色 绑定持久化
│   │   └── manifest.py            #   续传台账
│   │
│   ├── synthesis/                 # 【编排层】
│   │   ├── pipeline.py            #   单条:向量校验 → 查绑定 → 取音色 → TTS → 原子落盘
│   │   ├── chapter_runner.py      #   单章:续传扫描 → 逐条 → 拼接 → 清理
│   │   ├── queue.py               #   全局串行队列 + 暂停/停止
│   │   └── events.py              #   进度事件总线(供 SSE 消费)
│   │
│   ├── api/                       # 【HTTP 层】
│   │   ├── app.py                 #   FastAPI 装配 + 静态托管构建产物
│   │   ├── schemas.py             #   Pydantic 出入参
│   │   └── routes/
│   │       ├── projects.py
│   │       ├── voices.py
│   │       ├── bindings.py
│   │       ├── settings.py        #   全局设置读写
│   │       ├── fs.py              #   服务端卷内路径浏览
│   │       ├── tts.py             #   测试连接
│   │       └── jobs.py            #   开始/暂停/停止 + SSE
│   │
│   └── web/                       # 【Vue3 前端】
│       ├── src/views/             #   工程列表 / 角色绑定 / 音色库 / 进度
│       ├── src/api/               #   后端调用封装
│       └── ...
│
├── doc/                           # 与 src/ 严格镜像(本目录)
├── server/                        # 后端参考实现(只读,不修改)
│   ├── ui-api.py                  #   ← 线上部署的就是它
│   └── webui.py
├── reference/audio/               # 音色库(默认内容)
└── book-json/                     # 章节 JSON 样例
```

## 5. 三个核心抽象

接口先对齐,再写实现。

### 5.1 `TTSProvider` — 换引擎的唯一接缝

```python
# src/tts/base.py
class TTSProvider(Protocol):
    id: str
    capabilities: TTSCapabilities        # 能力 + 参数自描述

    def synthesize(self, req: TTSRequest) -> TTSResult: ...
    def probe(self) -> HealthResult: ...

@dataclass
class TTSRequest:
    text: str
    voice_ref: VoiceRef                  # 音频字节 / 参考文本 / voice_id
    emotion: EmotionSpec | None = None   # 领域意图,不是后端的模式枚举
    params: dict | None = None           # 键名来自本 provider 的 param_schema

@dataclass
class TTSResult:
    audio: bytes
    format: AudioFormat                  # 声明自己的输出格式
    is_canonical: bool                   # 是否已符合标准内部格式(见 R7)
```

**设计要点**:领域模型里**不出现任何后端专有概念**。
没有 `emo_control_method`(IndexTTS 的枚举)、没有 9 个硬编码参数、参考音色不是裸 `bytes`。
详见 [tts/design.md §6](tts/design.md#6-领域抽象修正-indextts-细节泄漏)。

### 5.2 单条片段诞生流程

```python
# src/synthesis/pipeline.py —— 唯一交叉点
def synthesize_one(sentence, ctx) -> Fragment:
    voice  = ctx.binding_for(sentence.speaker)          # project/ + voice/
    result = tts_provider.synthesize(...)               # tts/  ← 只给字节
    if not result.is_canonical:                         # R7:归一化在此
        result.audio, _ = audio.convert.to_canonical(   # audio/
            result.audio, result.format)
    write_atomic(fragment_path, result.audio)           # 原子写,防半截文件
```

> 情感向量的校验(如 IndexTTS2 的 0.8 卡口)在 provider 内部完成 —— 那是后端专有约束,
> 领域层不硬编码任何阈值。

### 5.3 三个核心抽象

| 抽象 | 位置 | 作用 |
|---|---|---|
| `VoiceRef` | `models.py` | 参考音色:`audio` / `prompt_text` / `voice_id` |
| `EmotionSpec` | `models.py` | 情感**意图**:`vectors` / `ref_audio` / `text` / `weight` |
| `TTSCapabilities` | `tts/capabilities.py` | 后端**能力自描述** + `ParamSpec[]` 参数表 |

UI 的参数表单由 `ParamSpec` 动态渲染 → **新增 provider 不改前端一行**。

### 5.3 续传台账

见 [project/design.md](project/design.md#manifest-续传协议),协议状态为【待评审】。

## 6. 运行时数据落点(三个卷)

| 容器内路径 | 内容 | 缺失后果 |
|---|---|---|
| `/workspace` | 书籍工程:`<书名>/{json/, process_audio/, audio/}` | 无工程可跑 |
| `/voices` | 音色库(默认提供 `reference/audio` 的内容) | 角色无法绑定音色 |
| `/config` | `config.json`(后端地址、超时、重试、并发等) | 配置丢失,回落到默认值 |

## 7. 三条从部署形态推导的硬约束

| 约束 | 原因 |
|---|---|
| 路径选择器必须是**服务端卷内路径浏览器** | Docker 容器内没有系统文件对话框;卷外路径不可达 |
| 任务队列是**进程级全局单例** | NAS 上多设备/多人同时访问,而后端无并发保护,必须全局串行 |
| manifest 必须**及时落盘** | 容器重启/升级后内存进度全丢,恢复完全依赖它 |

## 8. 实现顺序

自底向上,每层可独立测试:

```
models/config/errors  →  audio/  →  tts/  →  voice/ + project/  →  synthesis/  →  api/  →  web/
```
