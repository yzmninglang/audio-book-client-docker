# 音色库模块设计

对应源码:`src/voice/` · 状态:【已确认】

## 1. 职责与边界

**职责**:音色库的解析与增删改查,以及入库时的音频标准化。

**边界**:

- ✅ 可以 import `audio/`(用其探测与转码能力)
- ❌ 不关心哪个工程在用某个音色
- ❌ 不发起网络请求

## 2. 数据结构:目录即音色

沿用既有 `reference/audio/` 的组织方式 —— **一个目录 = 一个音色,目录名即音色名**(全中文)。

```
/voices/
├── 丁仪/
│   ├── 1.wav              # 主参考音频
│   └── pt.txt             # 参考文本
├── 汪淼/
│   ├── 1.wav              # 主参考音频
│   ├── pt.txt
│   ├── 不解.wav + 不解.txt          # 情感参考集(→ EmotionSpec.ref_audio)
│   ├── 叙述.wav + 叙述.txt
│   └── ...
├── 男旁白2/
│   ├── 1.wav
│   ├── pt.txt
│   └── output_spleeter/1/          # 分离产物 → 忽略
│       ├── vocals.wav
│       └── accompaniment.wav
└── ...
```

## 3. 解析规则(处理实测发现的三种异常)

实测发现音色目录里混着非参考音频的资产,解析器必须分类处理
(详见 [findings.md](../findings.md#11-音色库-referenceaudio)):

| 文件/目录 | 判定 | 处理 |
|---|---|---|
| `1.wav` | **主参考音频** | 使用 |
| `pt.txt` | 参考文本 | 保留为元数据(当前 API 用不上,换引擎时可能需要) |
| `<名称>.wav` + 同名 `.txt`(非 `1`) | **情感参考音频** | 识别为情感参考集,可在 UI 中选用(交给 `EmotionSpec.ref_audio`,由适配器决定怎么用) |
| `output_spleeter/` | 人声分离产物 | ❌ **忽略** |
| 仅含 `vocals.wav`/`accompaniment.wav` 的子目录 | 分离产物 | ❌ **忽略** |
| `1 - 副本.wav` | 副本 | ❌ 忽略(按 ` - 副本` / `(1)` 等模式识别) |
| 无 `1.wav` 但有其他 `*.wav` | 兜底 | 取排序第一个并标记为「非标准结构」,UI 提示用户确认 |

> 若整个目录**没有任何可用音频**,该目录不作为音色列出,并在 UI 上以「无效音色」提示。

## 4. 入库标准化(用户决策)

新建/导入音色时,调用 [audio/convert.py](../audio/design.md):

```
原始音频 → probe() 探测真实格式 → mono + 只降不升(≤22050) + 裁剪 ≤15s → 覆写为标准 PCM WAV
```

**收益**(实测):

| 指标 | 值 |
|---|---|
| 37 个音色总量 | 59.0MB → **15.9MB**(3.7×) |
| 单请求上行 | ~2.8MB → **~573KB**(含 base64) |
| 每本书上行 | ~3.4GB(公网传输,见 [findings.md](../findings.md#6-流量测算公网传输故必须算清)) |

> 实测确认:伪 wav(实为 MP3)**直接转发也能合成成功** —— 所以转码不是正确性要求,
> 而是**流量与健壮性优化**。选择在**入库时**做一次,而非每次请求时做。

**注意**:入库转码是**破坏性写回**(覆写原文件)。设计上:

- 首次导入时执行一次
- 在音色目录写一个 `.normalized` 标记文件,避免重复转码
- 处理前若原文件将被覆盖,UI 需明确告知

## 5. 功能清单

| 功能 | 说明 | 需求来源 |
|---|---|---|
| 列出音色 | 扫描 `/voices`,按 §3 规则解析,返回结构化列表 | 「给出音色库」 |
| 新建音色 | 上传音频 + 填参考文本 → 入库标准化 → 建目录 | 「音色新建」 |
| 重命名音色 | 重命名目录 | 「音色重命名」 |
| 编辑音色 | 替换参考音频 / 修改 `pt.txt` / 管理情感参考集 | 「音色修改」 |
| 删除音色 | 删除目录(**需二次确认,并提示哪些工程正在引用**)| ——(建议补充) |
| 试听 | 返回参考音频供浏览器播放 | 便于人工挑选音色 |

> 删除音色时提示引用情况:需要 `project/` 提供「反查哪些工程绑定了该音色」的能力 —— 建议保留该接口。

## 6. 接口草图

```python
# entry.py
@dataclass
class VoiceEntry:
    name: str                      # 目录名 = 音色名
    path: str
    main_audio: str                # 主参考音频路径
    prompt_text: str | None        # pt.txt 内容
    emo_refs: dict[str, str]       # 情感名 -> 音频路径
    is_normalized: bool
    warnings: list[str]            # 非标准结构提示

# library.py
class VoiceLibrary:
    def __init__(self, root: str): ...
    def list(self) -> list[VoiceEntry]: ...
    def create(self, name: str, audio: bytes, prompt_text: str | None) -> VoiceEntry: ...
    def rename(self, old: str, new: str) -> None: ...
    def update(self, name: str, *, audio: bytes | None, prompt_text: str | None) -> VoiceEntry: ...
    def delete(self, name: str) -> None: ...
    def reference_audio(self, name: str) -> bytes: ...   # 供 tts/ 使用(已在入库时标准化)
```

## 7. 命名与安全

- 音色名用于**目录名** → 必须校验并拒绝路径穿越(`..`、`/`、`\`)、空名、超长名
- 允许中文(既有 37 个音色全中文),但需确保容器 locale 为 UTF-8(见 [deploy/deployment.md](../deploy/deployment.md))
- 重命名冲突 → 明确报错,不静默覆盖
