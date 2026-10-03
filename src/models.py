"""领域模型 —— 跨层共享的数据结构。**这是全项目的接口冻结点。**

设计原则(见 doc/architecture.md §5、doc/tts/design.md §6):

    领域层**不得**出现任何 TTS 后端专有概念。

具体地:
    ✗ 没有 `emo_control_method`(那是 IndexTTS 的模式枚举)
    ✗ 没有硬编码的 9 个生成参数(那是 provider 自描述的 ParamSpec)
    ✗ 参考音色不是裸 `bytes`(用 VoiceRef,以便容纳 prompt_text / voice_id)
    ✗ 没有采样率常量以外的音频处理逻辑

只要守住这四条,新增 TTS 后端就只需加一个适配器文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

EMOTION_VECTOR_DIMS = 8
"""情感向量维度。源数据(章节 JSON)固定 8 维。"""

EMOTION_LABELS: tuple[str, ...] = ("喜", "怒", "哀", "惧", "厌恶", "低落", "惊喜", "平静")
"""情感向量各维语义。顺序与源数据一致,**不可调整**。"""

FRAGMENT_INDEX_WIDTH = 4
"""片段文件名序号宽度:0001.wav。"""


@dataclass(frozen=True)
class AudioFormat:
    """音频格式描述。用于让 provider 声明自己的输出格式(硬规则 R7)。"""

    sample_rate: int
    channels: int
    bits: int

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.sample_rate}Hz/{self.channels}ch/{self.bits}bit"


CANONICAL_FORMAT = AudioFormat(sample_rate=22050, channels=1, bits=16)
"""标准内部格式。

实测 IndexTTS2 的输出恒为此格式、与参考音频无关(见 doc/findings.md §5),
因此章节拼接是同格式 PCM 直连,零重采样。
其他后端若返回不同格式,由编排层归一化到本格式(硬规则 R7)。
"""


# --------------------------------------------------------------------------- #
# 合成输入
# --------------------------------------------------------------------------- #


@dataclass
class Sentence:
    """章节 JSON 中的一条,合成的最小单元。"""

    index: int  # 1-based,对应片段文件名序号
    speaker: str
    content: str
    emo_vector: list[float] | None = None
    delay_ms: int = 0  # **本条之后**的静音时长


@dataclass
class Chapter:
    """一个章节 = 一个 JSON 文件 = 一个成品音频。"""

    name: str  # 不含扩展名,如 "P01_扉页"
    json_relpath: str  # 相对工程根的路径 —— 只存相对路径(容器化约束 #9)
    sentences: list[Sentence] = field(default_factory=list)
    json_sha256: str = ""

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)


# --------------------------------------------------------------------------- #
# TTS 请求/响应(领域意图,不是后端报文)
# --------------------------------------------------------------------------- #


@dataclass
class VoiceRef:
    """参考音色。

    刻意**不是**裸 bytes —— 不同后端需要的参考信息不同:
    有的要参考音频,有的要参考文本(`pt.txt`),有的用它自己的音色 ID。
    各适配器按需取用。
    """

    audio: bytes | None = None
    prompt_text: str | None = None
    voice_id: str | None = None
    extras: dict[str, Any] | None = None

    @property
    def is_empty(self) -> bool:
        return self.audio is None and self.voice_id is None


@dataclass
class EmotionSpec:
    """情感**意图**(不是后端的模式枚举)。

    源数据目前只提供 `vectors`,其余两种是为了容纳不同后端的能力。
    `EmotionSpec` → 具体报文字段的映射只发生在各适配器内部。
    """

    vectors: list[float] | None = None
    ref_audio: bytes | None = None
    text: str | None = None
    weight: float = 1.0

    @property
    def is_empty(self) -> bool:
        return self.vectors is None and self.ref_audio is None and self.text is None


@dataclass
class TTSRequest:
    text: str
    voice_ref: VoiceRef
    emotion: EmotionSpec | None = None
    params: dict[str, Any] | None = None  # 键名来自本 provider 的 param_schema


@dataclass
class TTSResult:
    audio: bytes
    format: AudioFormat
    is_canonical: bool = False
    """provider 声明「我已符合标准内部格式」。

    为 True 时编排层跳过归一化(零开销快路径)。
    """


@dataclass
class HealthResult:
    """测试连接的探测结果。

    `sample_rate` / `duration_ms` 是给测试连接面板用的
    (doc/tts/design.md §5:展示「延迟 + 返回音频时长」,并可试听)。
    """

    ok: bool
    kind: str  # ok | unreachable | model_not_loaded | server_error | bad_request
    message: str = ""
    latency_ms: int | None = None
    sample_audio: bytes | None = None  # 成功时返回,供前端试听
    sample_rate: int | None = None
    duration_ms: int | None = None


class TTSProvider(Protocol):
    """换引擎的唯一接缝。

    加一个新后端 = 新增一个实现本 Protocol 的文件 + 在 registry.py 注册一行。
    `audio/`、`synthesis/`、`api/`、`web/` 零改动。
    """

    id: str

    label: str
    """展示名(如 "IndexTTS2"),供设置页的下拉与标题使用。

    必须由 provider 自己提供 —— 不能在 API 层硬编码一份 id→名称的映射,
    否则「加一个后端 = 加一个文件 + 注册一行」的承诺会被打破
    (仍要去 API 层改表)。
    """

    contract_version: int
    """该适配器实现的契约版本。

    会随 provider 一起写进 manifest 的 `provider_contract_version`:
    版本变化意味着「报文语义可能变了」,已生成的片段需整章重做
    (见 doc/project/design.md §5.4 第 0 步)。

    必须作为**实例可访问**的属性暴露 —— 编排层不应靠「模块常量叫什么名字」的
    命名约定去猜测。
    """

    @property
    def capabilities(self) -> Any:  # TTSCapabilities,定义在 tts/capabilities.py
        ...

    def synthesize(self, req: TTSRequest) -> TTSResult: ...

    def probe(self) -> HealthResult: ...


# --------------------------------------------------------------------------- #
# 工程 / 续传台账
# --------------------------------------------------------------------------- #


@dataclass
class Binding:
    """一个角色 → 一个音色的绑定。多角色可共用同一音色。"""

    voice: str
    emo_ref: str | None = None  # 可选:情感参考集中的一个


class FragmentStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"  # 绑定或后端变更后失效


@dataclass
class FragmentRecord:
    index: int
    status: FragmentStatus = FragmentStatus.PENDING
    bytes: int | None = None  # 用于轻量完整性校验
    speaker: str | None = None
    voice: str | None = None
    provider: str | None = None
    attempts: int = 0
    error: str | None = None


class ChapterStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"  # 有片段失败,未拼接(故意不产出半成品)
    DONE = "done"


@dataclass
class ChapterRecord:
    name: str
    json_sha256: str
    sentence_count: int
    status: ChapterStatus = ChapterStatus.PENDING
    output: str | None = None  # 相对工程根的路径
    provider: str | None = None
    provider_contract_version: int | None = None
    fragments: dict[str, FragmentRecord] = field(default_factory=dict)


class JobState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
