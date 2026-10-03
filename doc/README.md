# 有声书合成客户端 — 文档索引

> 本目录与 `src/` 严格镜像。所有需求、设计、部署文档集中于此。

## 文档状态约定

| 标记 | 含义 |
|---|---|
| 【已确认】 | 用户已明确拍板,可直接据此实现 |
| 【默认采纳】 | 提出后用户未提出异议,按此执行 |
| 【待评审】 | 需用户过目后才能定稿,尤其是续传协议 |
| 【建议】 | 实现建议,非强制 |

## 索引

| 文档 | 内容 | 状态 |
|---|---|---|
| [requirements.md](requirements.md) | 需求与边界清单(定稿) | 【已确认】 |
| [findings.md](findings.md) | 实测结论:后端契约、素材现状、数据更正、流量测算 | 【已确认】 |
| [architecture.md](architecture.md) | 分层、依赖硬规则、模块划分、目录树、数据落点 | 【已确认】 |
| [dev-workflow.md](dev-workflow.md) | **开发工作流**:环境、容器化约束、M0~M7 里程碑、测试策略 | 【已确认】 |
| [audio/design.md](audio/design.md) | 音频处理模块 | 【已确认】 |
| [tts/design.md](tts/design.md) | TTS 接入模块(契约映射、重试分层) | 【已确认】 |
| [voice/design.md](voice/design.md) | 音色库模块 | 【已确认】 |
| [project/design.md](project/design.md) | 工程模块 + **续传 manifest 协议** | 协议【待评审】 |
| [synthesis/design.md](synthesis/design.md) | 编排层:流水线、队列、进度 | 【已确认】 |
| [api/design.md](api/design.md) | HTTP 层与 SSE | 【已确认】 |
| [web/design.md](web/design.md) | Vue3 前端 | 【已确认】 |
| [deploy/deployment.md](deploy/deployment.md) | Docker / NAS 部署 | 【已确认】 |

## 一句话概括

一个**零算力**的有声书合成客户端:跑在 NAS 容器里,浏览器访问,把章节 JSON 按**逐条**方式发给远端 IndexTTS 后端合成,
用 **FFmpeg-free** 的纯 Python 拼接成章节成品,靠 **manifest** 支持任意中断续传。

## 扩展性(一条硬要求)

需求方明确要求:**新增 TTS 后端必须能增量接入,而不是修改现有适配器去兼容它。**

架构承诺:

> **加一个新后端 = 加一个 `src/tts/<name>_client.py` + 在 `registry.py` 注册一行。**
> `audio/`、`synthesis/`、`api/`、`web/` **零改动**。

该承诺由三条硬规则保障(R1 / R3 / R7),并由 5 处领域抽象修正落实
—— 详见 [tts/design.md §6](tts/design.md#6-领域抽象修正-indextts-细节泄漏)。

## 关键外部依赖

| 项 | 值 |
|---|---|
| TTS 后端 | `http://hinas-v4.ninglang.top:8300/api/tts`(公网,无鉴权) |
| 后端实现 | 仓库 `server/ui-api.py`(线上部署与之一致) |
| 音色库 | `reference/audio/`(37 个音色,目录名即音色名) |
| 章节 JSON 样例 | `book-json/P01_扉页.json` |
