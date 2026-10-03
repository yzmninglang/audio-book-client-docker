# TTS 接入模块设计

对应源码:`src/tts/` · 状态:【已确认】

## 1. 职责与边界

**职责**:把**领域意图**翻译成某个具体 TTS 后端的调用,并把后端的错误翻译回领域语义。

**边界(硬规则 R3)**:

- ❌ 只吃字节、只吐字节 —— 不读文件路径,不碰文件系统
- ❌ **禁止 import `audio/`**(架构硬规则 R1)
- ✅ 后端契约的全部细节(字段名、模式枚举、错误码、默认参数)**只允许出现在本模块内**

### 1.1 可扩展性目标(本模块的核心承诺)

> **加一个新的 TTS 后端 = 加一个文件 + 注册一行。**
> `audio/`、`synthesis/`、`api/`、`web/` **零改动**。

```
src/tts/
├── base.py                  # 抽象与契约(新增后端时不动)
├── registry.py              # 注册表(+1 行)
├── capabilities.py          # 能力与参数自描述
├── indextts_client.py       # 现有
└── <new_provider>_client.py # 新增 ← 只需这一个文件
```

> 这个承诺能成立的前提是 §6 的 5 处抽象修正 —— **若把 IndexTTS 的细节留在领域模型里,「加文件」就会退化成「改一堆地方」**。

## 2. 后端契约(IndexTTS2,实测)

地址默认 `http://hinas-v4.ninglang.top:8300`,端点 `POST /api/tts`。
完整实测细节见 [findings.md](../findings.md#2-后端契约实测确认)。

> ⚠️ **本节全部内容属于 `indextts_client.py` 的内部实现细节,严禁外泄到领域模型、编排层或前端。**

### 2.1 字段映射

| 领域概念 | 后端字段 | 映射规则 |
|---|---|---|
| 句子文本 | `tts_text` | 直接 |
| 参考音色 | `prompt_audio` | base64(参考音频字节) |
| 情感向量 | `emo_vector` + `emo_control_method=2` | 8 维,顺序 喜/怒/哀/惧/厌恶/低落/惊喜/平静 —— **顺序不可改** |
| 情感参考音频 | `emo_audio` + `emo_control_method=1` + `emo_weight` | 来自音色目录的情感参考集 |
| 情感文本 | `emo_text` + `emo_control_method=3` | —— |
| 生成参数 | 9 个高级参数 | 见 §6.4 的 `ParamSpec` |

⚠️ **命名陷阱**:请求体键名是 `max_text_tokens_per_sentence`(**sentence**),
而模型内部参数叫 `max_text_tokens_per_segment`(**segment**)。映射时必须用前者。

⚠️ `emo_control_method` 是 **IndexTTS 独有的模式枚举**(0/1/2/3),它只能存在于本模块内部。

### 2.2 响应解析

| 响应 | 处理 |
|---|---|
| `status="success"` | 取 `audio`(base64 wav)与 `sample_rate`(实测恒 22050) |
| `status="error"` + 400 | → `FatalError`,**不重试** |
| `status="error"` + 500 | → `RetryableError`,退避重试 |
| 网络异常/超时 | → `RetryableError` |

## 3. 情感向量校验(重要)

实测发现一个**后端宽松、模型严格**的错位:

| 校验点 | 阈值 |
|---|---|
| `ui-api.py` 实际拒绝 | `sum > 1.5` |
| 模型 `normalize_emo_vec` 意图上限 | **0.8** |

且 `ui-api.py` **不调用** `normalize_emo_vec`(只有 `webui.py` 调),向量原样透传。
故 **0.8 ~ 1.5 是危险区**:后端照单全收,但超出模型预期,可能产生过激/崩坏的情感表现。

**规则**:

```
if sum(emo_vector) > 0.8:
    write_log(WARN, "向量之和 {sum} 超出模型预期 0.8,已按 0.8/sum 缩放")
    emo_vector = [x * 0.8 / sum for x in emo_vector]
```

> 这是 **IndexTTS2 特有的阈值**,属于 `indextts_client.py`。
> 其他后端若有自己的约束,在各自适配器内实现;**领域层不硬编码任何阈值**。

> 全零向量仍走 `emo_control_method=2` 透传(用户决策)。实测零向量 ≈ 不施加情感引导,是安全的。
> 建议首次跑通后抽 1~2 条做 method 0/2 的 A/B 试听确认无差别。

## 4. 重试策略(分层)

需求要求「可设置重试次数」。**不能一刀切** —— 400 类错误重试是纯粹的浪费:

| 错误类型 | 重试? | 退避 |
|---|---|---|
| 连接失败 / 超时 | ✅ | 指数退避 1s→2s→4s,上限 30s |
| HTTP 500 | ✅ | 同上(**后端无并发保护,必须退避,不可立即重试**) |
| HTTP 400 | ❌ | 立即失败,错误信息定位到具体句子 |
| 模型未加载(500 + 固定文案「模型未成功加载」) | ❌ | 立即失败并提示用户检查后端 |
| 音频解码失败 | ❌ | 数据问题,重试无意义 |

**重试次数可配置**(需求要求),默认建议 3 次。
分类结果统一收敛为 `errors.py` 的 `RetryableError` / `FatalError` —— **编排层只认这两个类型,不认 HTTP 码**。

## 5. 测试连接(需求要求)

后端**没有任何健康检查端点**,只有 `/api/tts`。因此:

> 测试连接 = **发一次极短文本的真实合成**(如 `"测试连接。"`,配一个已入库音色)。

这样能真实反映可用性,并能区分四种失败:

| 现象 | `kind` | 用户提示 |
|---|---|---|
| 连接被拒 / DNS 失败 / 超时 | `unreachable` | 「后端不可达,请检查地址与网络」 |
| 500 + 「模型未成功加载」 | `model_not_loaded` | 「后端已启动但模型未加载」 |
| 500 其他 | `server_error` | 展示原始 message |
| 400 | `bad_request` | 展示原始 message(通常是客户端 bug) |
| 200 | `ok` | ✅ 展示耗时 + 返回音频时长,可试听 |

> 测速价值:实测单条 1.8~4.3s,测试连接顺带给出真实延迟基准,供进度预估。

## 6. 领域抽象(修正 IndexTTS 细节泄漏)

> 本节是「增量添加新后端」能否成立的关键。下列 5 处若按 IndexTTS 的形状定义,新后端就会被迫兼容它。

### 6.1 ❌ 泄漏:`emo_control_method: int`

`0/1/2/3` 是 IndexTTS 独有的模式枚举。新模型可能只有「风格字符串」,或根本没有情感控制。

**✅ 修正:领域层只表达意图,不表达实现**

```python
# src/models.py
@dataclass
class EmotionSpec:
    vectors: list[float] | None = None    # 源数据(book JSON)固定 8 维
    ref_audio: bytes | None = None        # 情感参考音频
    text: str | None = None               # 情感描述文本
    weight: float = 1.0
```

`EmotionSpec` → `emo_control_method=2` 的映射**只发生在 `indextts_client.py` 内部**。

### 6.2 ❌ 泄漏:`voice_ref: bytes`(太窄)

新引擎可能需要:参考音频 **+ 参考文本**(`pt.txt` 正是为此保留的)、多段参考、或它自己注册表里的 `voice_id`。

**✅ 修正:`VoiceRef` 携带足够信息,由适配器各取所需**

```python
@dataclass
class VoiceRef:
    audio: bytes | None = None        # 参考音频字节
    prompt_text: str | None = None    # 参考文本 ← 新引擎可能需要
    voice_id: str | None = None       # 后端自带音色库时
    extras: dict | None = None
```

> **流量前瞻**:当前 IndexTTS 契约要求每请求重传参考音频(实测 ~573KB/请求,一本书 ~3.4GB)。
> 若未来后端支持「一次上传、`voice_id` 引用」,只需实现本类型的 `voice_id` 分支,
> **编排层与音频层零改动**。本次不改后端(用户决策),仅做类型预留。

### 6.3 ❌ 泄漏:`advanced: dict`(9 个参数是 IndexTTS 的)

若设置页硬编码 `temperature/top_p/top_k/num_beams...`,换个没有这些参数的后端就要改前端。

**✅ 修正:provider 自描述参数表,前端动态渲染(见 [web/design.md §3.4](../web/design.md))**

### 6.4 ✅ `ParamSpec` — 参数自描述

```python
# src/tts/capabilities.py
@dataclass
class ParamSpec:
    name: str
    label: str
    type: Literal["bool", "int", "float", "str", "enum"]
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
```

IndexTTS2 的 9 个参数各自写成一个 `ParamSpec`(键名用请求体的 `max_text_tokens_per_sentence`)。
领域层只传递 `params: dict`,**键名来自本 provider 的 `param_schema`**。

### 6.5 ✅ `TTSCapabilities` — 能力自描述

```python
@dataclass
class TTSCapabilities:
    emotion: set[str]                  # {"vector", "ref_audio", "text"}
    emotion_vector_dims: int | None    # IndexTTS2 = 8
    voice_modes: set[str]              # {"audio_bytes", "voice_id"}
    needs_prompt_text: bool
    supports_concurrent: bool
    returns_canonical: bool            # 见 §8
    param_schema: list[ParamSpec]
```

### 6.6 ✅ `TTSResult` — 声明自己的输出格式

```python
@dataclass
class TTSResult:
    audio: bytes
    format: AudioFormat        # {sample_rate, channels, bits}
    is_canonical: bool         # provider 声明「我已符合标准内部格式」
```

## 7. 注册与配置

```python
# src/tts/registry.py
PROVIDERS: dict[str, type[TTSProvider]] = {
    "indextts2": IndexttsClient,
    # 新增后端: 在此加一行
}

def create(provider_id: str, cfg: dict) -> TTSProvider: ...
```

配置形态(存 `/config/config.json`):

```json
{
  "active_provider": "indextts2",
  "providers": {
    "indextts2": {
      "base_url": "http://hinas-v4.ninglang.top:8300",
      "timeout": 300,
      "retries": 3,
      "auth": null,
      "params": { "temperature": 0.8, "top_p": 0.8 }
    }
  }
}
```

- 每个 provider 一份独立配置 → **切后端不改动其他 provider 的配置**
- `auth` 为**预留字段**,本次不使用(见 [findings.md §7](../findings.md#7-安全发现))

## 8. 输出格式与归一化归属(最严重的一处泄漏)

`audio/segment.py` 的拼接是「同格式 PCM 字节直接连接」——
它**假设了所有片段格式一致**。实测 IndexTTS2 恒返回 22050Hz/16bit/mono 才成立。

> ⚠️ 若新后端返回 24kHz 立体声,同一章内会拼出**采样率错乱的成品**,而且是**静默损坏**。

**✅ 修正:归一化归属编排层**

```
tts/ 声明格式  →  synthesis/ 决定是否归一化  →  audio/ 提供转换能力
```

```python
# synthesis/pipeline.py
if not result.is_canonical:
    normalized, _ = audio.convert.to_canonical(result.audio, result.format)
    result.audio = normalized
write_atomic(fragment_path, result.audio)
```

**为什么不放在 `tts/` 里**:那需要 `tts/` import `audio/`,直接违反硬规则 R1。
放在 `synthesis/` 既保住了 R1,又把「让异构输出变统一」这件事放在它本该在的位置 ——
`synthesis/` 本来就是唯一同时认识 `tts/` 与 `audio/` 的地方(硬规则 R4)。

**性能**:IndexTTS2 设 `returns_canonical=True` → 走零开销快路径,不做任何转码。

## 9. 能力协商与降级策略

若新后端**不支持情感向量**,而 chapter JSON 里有 `emo_vector` —— 静默忽略 = **静默降质**。

**规则**:开始合成前做一次能力检查,不支持的意图按**显式策略**处理:

| 策略 | 行为 | 建议 |
|---|---|---|
| `warn` | 忽略该意图,记录警告并在 UI 明示 | ✅ **默认** —— 不因个别能力缺失中断整章 |
| `error` | 拒绝启动,提示用户换后端或改配置 | 对音质要求严格的场景 |
| `map` | 尝试近似映射(如把向量降维/取主导维度) | 需 provider 显式实现,不做隐式猜测 |

> 当前**没有任何 provider 实现 `map`**,故该策略在实现中等价于 `warn`(丢弃 + 显式告警)。
> 这是刻意的:宁可告警,也不做隐式猜测。

> 原则:**宁可显式告警,也不静默降质。**

## 10. 并发约束

后端 `ui-api.py` 使用 Flask 且 **无锁、无并发保护**,GPU 推理并发会导致 OOM/崩溃。

→ 本模块**不做内部并发**;串行由 [synthesis/queue.py](../synthesis/design.md) 全局保证。
`TTSCapabilities.supports_concurrent` 供队列未来放开并发度时参考。

## 11. 接口草图

```python
# base.py
class TTSProvider(Protocol):
    id: str
    capabilities: TTSCapabilities

    def synthesize(self, req: TTSRequest) -> TTSResult: ...
    def probe(self) -> HealthResult: ...

@dataclass
class TTSRequest:
    text: str
    voice_ref: VoiceRef
    emotion: EmotionSpec | None = None
    params: dict | None = None            # 键名来自本 provider 的 param_schema

@dataclass
class HealthResult:
    ok: bool
    kind: Literal["ok", "unreachable", "model_not_loaded", "server_error", "bad_request"]
    latency_ms: int | None
    message: str
    sample_audio: bytes | None            # 成功时返回,供前端试听
```

## 12. 刻意不做的两件事

| 不做 | 理由 |
|---|---|
| 不支持「整章批量提交」的后端优化 | 本项目的续传粒度是**片段**,按章提交会让断点续传退化。这类后端也接受单条,适配器按单条调用即可 |
| 接口不做异步/流式 | `synthesize()` 保持**同步**;后端若是「提交任务→轮询」或流式,适配器内部阻塞消化。**接缝越窄越好** |
