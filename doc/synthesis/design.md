# 编排层设计

对应源码:`src/synthesis/` · 状态:【已确认】

## 1. 职责与边界

**职责**:串起「一条片段如何诞生」的全过程,并管理队列、进度与中断。

**边界(硬规则 R4)**:这是**唯一同时 import `tts/` 与 `audio/` 的地方**。
它知道 HTTP 的成败,也知道文件怎么写,但**不知道 HTTP 报文长什么样**,也**不知道 FFmpeg/编码细节** —— 那些分别属于 `tts/` 和 `audio/`。

## 2. 单条片段流水线

```
1. 取句子(speaker, content, emo_vector, delay)
2. 能力检查:provider 是否支持该情感意图?不支持 → 按策略告警/报错   ← 启动前已做
3. 查绑定 → 取音色 → 组装 VoiceRef(音频字节 + pt.txt)              ← project/ + voice/
4. tts_provider.synthesize(text, voice_ref, emotion, params)        ← tts/ 只吃字节吐字节
5. 若 not result.is_canonical:归一化为标准格式                       ← R7,audio/
6. write_atomic(process_audio/<章>/<0001>.wav)                       ← 原子写
7. manifest 记 done(含 provider)并落盘                              ← project/
```

**第 5 步是硬规则 R7 的落点**:异构后端的输出格式在此被统一成 22050 mono,
使 [audio/segment.py](../audio/design.md#34-拼接零重采样) 的同格式拼接始终成立。
放在这里而非 `tts/` 内,是为了不破坏 R1(`tts/` 与 `audio/` 互不 import)。
provider 若声明 `returns_canonical=True`(IndexTTS2),本步整个跳过,零开销。

**参考音频缓存**:同一音色在一章内会被反复使用(每请求重传 base64)。
进程内按音色名缓存标准化后的字节与其 base64,避免重复读盘与编码。

## 3. 章节执行器

单章状态机:

```
pending → scanning(续传对账) → synthesizing(逐条) → concatenating(拼接) → done
                                        ↓ 失败
                                    partial(记录失败条目,可重跑)
```

关键行为:

| 行为 | 说明 |
|---|---|
| 来源检查 | 若本章 `provider` 与当前激活后端不一致 → 整章重做,不做部分混合(见 [project/design.md §5.5](../project/design.md#55-绑定变更与后端变更的处理)) |
| 能力检查 | 开跑前校验 provider 是否支持本章需要的情感意图,不支持则按策略告警/报错 |
| 续传对账 | 进入 `synthesizing` 前,按 [project/design.md §5.4](../project/design.md#54-启动对账算法) 确定哪些条目要跑 |
| 逐条合成 | 严格按 JSON 顺序(成品音频顺序必须与文本一致) |
| 失败隔离 | 单条失败**不中断整章**,记录后继续;章末汇总失败清单 |
| 拼接前提 | 仅当**全部条目成功**才拼接成章;有失败则停在 `partial`,不产出半成品 |
| 清理时机 | **拼接成功并写入 `audio/` 之后**,才删 `process_audio/<章>/` |
| 幂等 | 对 `done` 的章节重跑 = 直接跳过(除非用户显式选「重做」) |

> **故意不做的**:不做「跳过失败条目先拼一版」的降级策略。理由:成品一旦带上「已知缺陷」的标记,
> 后续续传会对账困难,容易产生静默损坏。宁可停在 `partial` 让用户处理。

## 4. 全局串行队列

**硬约束**:进程级**全局单例**,不是每会话一个。

理由(见 [findings.md](../findings.md#5-关键机制发现)):NAS 上可能多设备/多人同时开页面,而后端 `ui-api.py`
**无锁、无并发保护**,并发请求会导致 GPU OOM 或崩溃。

| 特性 | 设计 |
|---|---|
| 并发度 | 默认 **1**;配置项预留(未来后端支持并发时放开) |
| 任务粒度 | 「章节」为调度单元,「片段」为执行单元 |
| 队列持久性 | **不做持久化队列** —— 状态本来就落在 manifest 里,重启后重新扫描即可 |
| 多用户 | 队列全局共享,UI 显示「正在运行其他任务」 |

## 5. 进度事件

`events.py` 提供事件总线,`api/` 侧以 **SSE** 推送给浏览器。

| 事件 | 载荷 |
|---|---|
| `chapter_started` | 章节名、总条数、其中需生成条数 |
| `fragment_done` | 章节名、索引、耗时、累计进度 |
| `fragment_failed` | 章节名、索引、错误分类、重试次数 |
| `chapter_concatenated` | 章节名、成品路径、时长 |
| `chapter_failed` | 章节名、失败清单 |
| `job_state` | `running` / `paused` / `stopped` / `idle` |
| `log` | 分级日志(供 UI 的日志面板) |

**耗时预估**:实测单条 1.8~4.3s(见 [findings.md](../findings.md#3-连通性与功能验证结果)),
可按「剩余条数 × 3s」给出粗略 ETA,并随实测均值动态修正。

## 6. 开始 / 暂停 / 停止

语义定义(需求要求「重试次数可设置」,但暂停/停止语义需明确):

| 操作 | 语义 |
|---|---|
| 全部开始 | 扫描所有章节 → 对未完成部分入队 → 串行执行 |
| 单章开始 | 只入队指定章节 |
| 暂停 | 当前片段**跑完**后停;已完成片段与 manifest 保留;重启可续 |
| 停止 | 同暂停,但清空队列中未开始的任务 |
| 重做章节 | 显式作废旧成品与片段,重新生成 |

> **暂停不中断进行中的 HTTP 请求**,避免留下后端已生成但客户端未接收的无效工作。

## 7. 接口草图

```python
# pipeline.py
def synthesize_one(sentence: Sentence, idx: int, ctx: ChapterContext) -> FragmentResult: ...

# chapter_runner.py
class ChapterRunner:
    def run(self, chapter: Chapter, ctx: ChapterContext,
            on_event: Callable[[Event], None]) -> ChapterResult: ...

# queue.py
class SynthesisQueue:                 # 进程级单例
    def enqueue_chapters(self, project: Project, chapters: list[str]) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> JobState: ...
```

## 8. 异常与重试的归属

| 异常来源 | 分类 | 由谁处理 |
|---|---|---|
| `tts/` 抛 `RetryableError` | 可重试 | `tts/retry.py` 内部退避重试,耗尽后抛给流水线 |
| `tts/` 抛 `FatalError` | 不可重试 | 流水线记为 `failed`,继续下一条 |
| `voice/` 绑定失效 | 配置问题 | **章开始前**就检查,避免跑一半才失败 |
| `audio/` 解码/拼接失败 | 不可重试 | 全章失败,保留片段供排查 |
| manifest 写失败 | 严重 | 停止任务并告警(否则续传不可靠) |

> 第 3 条尤其重要:**开跑前一次性校验该章所有绑定的有效性**,比跑几百条后才发现某个角色没绑音色要好得多。
