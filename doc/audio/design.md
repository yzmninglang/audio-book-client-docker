# 音频处理模块设计

对应源码:`src/audio/` · 状态:【已确认】

## 1. 职责与边界

**职责**:真实的音频字节处理 —— 格式探测、解码、转码、重采样、裁剪、静音插入、拼接、base64 编解码。

**边界(硬规则 R2)**:

- ❌ 不认识「音色」「工程」「后端」等业务概念
- ❌ 不发起任何网络请求
- ❌ **禁止 import `tts/`**(架构硬规则 R1)
- ✅ 纯函数式,输入字节/路径,输出字节/元信息 —— 可单独单测

## 2. 子模块

| 文件 | 职责 |
|---|---|
| `probe.py` | 真实格式探测(magic bytes + 解码器试探),返回 `{format, sample_rate, channels, bits, duration}` |
| `convert.py` | 转码到标准参考音频:**mono + 只降不升(≤22050)+ 裁剪 ≤15s** |
| `segment.py` | 按 `delay` 插入静音;把有序片段拼成章节成品 |
| `codec.py` | base64 编解码 + 体积估算(供流量预警) |

## 3. 关键规则

### 3.1 扩展名不可信

实测:37 个参考音频里有 **3 个 `.wav` 实为 MP3**(见 [findings.md](../findings.md#11-音色库-referenceaudio))。

→ `probe.py` 必须以**解码库的真实判定**为准,扩展名只作参考:

```
1. soundfile.info() 能报出真实格式(实测可识破伪 wav),作为主路径
2. magic bytes 作快速路径与二次确认
3. 全部失败 → 抛 FatalError(不可重试)
```

> 实测:`soundfile` 自带 `libsndfile 1.2.2`,**支持 MP3**,`sf.info("陈平安1/1.wav")`
> 正确报出 `MP3/MPEG_LAYER_III`。因此不需要手写完整的格式嗅探。

**⚠️ 半截文件(实测存在,必须处理)**

`标准女声/1.wav` 的 WAV 头声明 486400 字节,实际文件只有 187198 字节。
stdlib `wave` 照头返回 **15.2s**,而 `soundfile` 按物理字节得到 **5.85s(真实)**。

→ 走 stdlib 路径时**必须按物理可用字节夹紧帧数**;任何按头部长度分配缓冲的实现都会踩坑。
详见 [findings.md §1.1](../findings.md#11-音色库-referenceaudio)。

### 3.2 只降不升(≤22050)

后端内部会把参考音频重采样到 22050/16000,**参考音频的采样率不影响输出**(见 [findings.md](../findings.md#4-三条判断更正))。
故重采样只为**减小传输体积**:

| 源采样率 | 处理 |
|---|---|
| > 22050(如 44100) | **降**到 22050 ✅ 体积减半 |
| ≤ 22050(如 16000) | **保持原样**,❌ 不升采样(升采样会让体积变大且无收益) |
| 多声道 | 一律降为 **mono**(体积减半) |

### 3.3 裁剪 ≤15s

`infer_v2.py` 中 `_load_and_cut_audio(prompt, 15, ...)` —— 后端**只读前 15 秒**。
故超出部分纯属白传流量,入库时裁掉,零质量损失。

### 3.4 拼接:零重采样

实测后端输出**恒为 22050Hz/16bit/mono**,因此章节拼接是**同格式 PCM 直接拼接**:

```python
# 无需 FFmpeg,纯 stdlib
frames = b"".join(fragment_frames[i] + silence(delay_i) for i in order)
```

> 该结论已实测验证:两段不同音色的真实合成结果直拼 + 插 600ms 静音,stdlib `wave` 完成。

**拼接时禁止追加淡入淡出**:后端已对每个片段做过 30ms 淡出(见 [findings.md](../findings.md#5-关键机制发现)),
客户端再叠加会产生可听见的段间凹陷。

### 3.5 无 FFmpeg 依赖

| 操作 | 方案 | 需要 FFmpeg? |
|---|---|---|
| WAV 读写 | stdlib `wave` | ❌ |
| 静音插入 | `b"\x00" * n` | ❌ |
| 拼接 | `bytes` 连接 | ❌ |
| 格式探测 | magic bytes | ❌ |
| MP3 解码 | `soundfile`(pip wheel,自带 libsndfile≥1.1) | ❌ |
| 重采样 | `soxr` 或 `numpy`(pip wheel) | ❌ |
| MP3 **编码** | —— | 仅在需要 MP3 成品时才要 |

→ 核心路径**完全不需要 FFmpeg**,镜像可保持 slim。详见 [deploy/deployment.md](../deploy/deployment.md)。

## 4. 接口草图

```python
# probe.py
@dataclass
class AudioInfo:
    format: str          # "wav" | "mp3" | ...
    sample_rate: int
    channels: int
    bits: int
    duration: float
    is_actually_what_ext_says: bool   # 扩展名是否可信

# filename 仅用于判定 is_actually_what_ext_says;不传时不臆断(默认 True)
def probe(data: bytes, *, filename: str | None = None) -> AudioInfo: ...

# convert.py
def to_reference_audio(data: bytes, *, max_seconds: float = 15.0,
                       max_sample_rate: int = 22050) -> bytes:
    """【入库用】mono + 只降不升 + 裁剪 ≤15s。返回标准 PCM WAV 字节。"""

def to_canonical(data: bytes, fmt: AudioFormat | None = None
                 ) -> tuple[bytes, AudioFormat]:
    """【归一化用,硬规则 R7 的入口】任意后端输出 → CANONICAL_FORMAT。

    - 采样率:**可升可降**,一律到 22050(与 to_reference_audio 刻意相反)
    - 声道/位深:一律 mono / 16bit
    - **不裁剪时长**(片段必须完整)
    - 已符合 CANONICAL_FORMAT 时原样返回(零开销快路径)

    调用方是 synthesis/,不是 tts/ —— 见 architecture.md §5.2 与 R7。
    """

# segment.py
def silence(ms: int, sample_rate: int, channels: int, sampwidth: int) -> bytes: ...
def concat(fragments: list[bytes], delays: list[int], *, template: AudioInfo) -> bytes: ...

# codec.py
def b64_and_estimate(data: bytes) -> tuple[str, int]: ...   # (base64, 预估上行字节数)
```

## 5. 测试策略

纯函数,极易单测,且**不需要网络**:

- 用手头真实素材当 fixture:44.1k stereo WAV、16k mono WAV、伪 wav(实为 MP3)
- 断言 `probe()` 能识破伪 wav
- 断言 `to_reference_audio()` 对 16k 输入**不升采样**
- 断言拼接结果的采样率/声道/位深与输入一致,且时长 = Σ片段 + Σdelay
