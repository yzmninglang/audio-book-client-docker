"""单条片段流水线 —— **唯一同时认识 `tts/` 与 `audio/` 的地方**(硬规则 R4)。

对应设计:`doc/synthesis/design.md` §2,以及 `doc/architecture.md` §5.2。

一条片段的诞生(严格按序):

    1. 能力检查(章级,启动前;见 `chapter_runner.check_capabilities`)
    2. 查绑定 → 取音色 → 组装 `VoiceRef`(音频字节 + pt.txt)
    3. provider.synthesize(text, voice_ref, emotion, params)   ← tts/ 只吃字节吐字节
    4. 若 `not result.is_canonical` → `audio.to_canonical()`     ← R7 归一化落点
    5. 原子写 `process_audio/<章>/<0001>.wav`
    6. manifest 记 done(含 provider)并**逐条落盘**

**参考音频进程内缓存**:同一音色在一章内会被反复使用(每请求重传 base64),
`ReferenceCache` 按音色名缓存字节与 `pt.txt`,避免重复读盘。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..audio import to_canonical
from ..errors import (
    BindingError,
    ConfigError,
    FatalError,
    NotFoundError,
    RetryableError,
)
from ..models import (
    Chapter,
    EmotionSpec,
    FragmentStatus,
    Sentence,
    TTSRequest,
    VoiceRef,
)
from ..project import Project
from ..voice import VoiceLibrary
from .events import EventBus

logger = logging.getLogger(__name__)

# 情感**意图**类型(`TTSCapabilities.emotion` 的取值,见 doc/tts/design.md §6.5)
EMOTION_KIND_VECTOR = "vector"
EMOTION_KIND_REF_AUDIO = "ref_audio"
EMOTION_KIND_TEXT = "text"

# 能力降级策略(见 doc/tts/design.md §9)
POLICY_WARN = "warn"  # 默认:忽略不支持的意图,显式告警,不中断
POLICY_ERROR = "error"  # 拒绝启动
POLICY_MAP = "map"  # 需 provider 显式实现;本项目不做隐式猜测,等价于 warn


# --------------------------------------------------------------------------- #
# 参考音频缓存
# --------------------------------------------------------------------------- #


class ReferenceCache:
    """按音色名缓存参考音频字节与参考文本(进程内,避免重复读盘)。

    入库时 `voice/` 已把主参考音频标准化为 mono + 只降不升 + ≤15s
    (见 `doc/voice/design.md` §4),故此处直接缓存磁盘字节。
    """

    def __init__(self, voices: VoiceLibrary) -> None:
        self._voices = voices
        self._refs: dict[str, VoiceRef] = {}
        self._emotion: dict[tuple[str, str], bytes] = {}
        self.reads = 0  # 读盘次数(供测试断言缓存生效)

    def voice_ref(self, name: str) -> VoiceRef:
        """取音色 → `VoiceRef`(音频字节 + `pt.txt`)。缓存命中则不读盘。

        Raises:
            NotFoundError: 音色不存在或无可用音频。
        """
        ref = self._refs.get(name)
        if ref is None:
            entry = self._voices.get(name)  # 不存在时抛 NotFoundError
            data = Path(entry.main_audio).read_bytes()
            self.reads += 1
            ref = VoiceRef(audio=data, prompt_text=entry.prompt_text, voice_id=name)
            self._refs[name] = ref
        return ref

    def emotion_audio(self, voice_name: str, emo_name: str) -> bytes:
        """取音色的情感参考音频(缓存)。

        Raises:
            NotFoundError: 音色不存在,或该情感参考不存在。
        """
        key = (voice_name, emo_name)
        cached = self._emotion.get(key)
        if cached is None:
            cached = self._voices.emotion_reference_audio(voice_name, emo_name)
            self._emotion[key] = cached
        return cached


# --------------------------------------------------------------------------- #
# 运行控制(暂停 / 停止)
# --------------------------------------------------------------------------- #


class RunControl:
    """暂停 / 停止信号。

    语义(见 `doc/synthesis/design.md` §6):

        * 暂停 —— 当前片段**跑完**后停,**不中断进行中的 HTTP**(避免后端白干)。
        * 停止 —— 同暂停,但由队列额外清空未开始的任务。
    """

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._stop = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        self._stop.clear()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()


# --------------------------------------------------------------------------- #
# 章节上下文
# --------------------------------------------------------------------------- #


@dataclass
class ChapterContext:
    """单次运行所需的全部依赖(编排层内部携带,不跨层泄漏后端细节)。"""

    project: Project
    provider: Any  # TTSProvider
    voices: VoiceLibrary
    voice_names: set[str]
    provider_contract_version: int = 1
    params: dict[str, Any] | None = None
    policy: str = POLICY_WARN
    retries: int = 3
    control: RunControl = field(default_factory=RunControl)
    bus: EventBus = field(default_factory=EventBus)
    report: Any = None  # ReconcileReport | None(由队列一次性对账后注入)
    cache: ReferenceCache | None = None
    # 运行期由 chapter_runner 逐章设置:
    chapter: Chapter | None = None
    supported_emotion_kinds: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = ReferenceCache(self.voices)

    # -- 便捷访问 --------------------------------------------------------- #

    def bindings_map(self) -> dict[str, str]:
        """角色 → 音色名(供对账与能力检查)。"""
        if self.project.bindings is None:
            return {}
        return {role: b.voice for role, b in self.project.bindings.all().items()}

    def binding_for(self, speaker: str) -> Any:
        """取某角色的绑定。

        Raises:
            BindingError: 角色未绑定音色。
        """
        binding = self.project.bindings.get(speaker) if self.project.bindings else None
        if binding is None:
            raise BindingError(f"角色未绑定音色: {speaker}")
        return binding

    def emit(self, event_type: str, **payload: Any) -> None:
        self.bus.emit(event_type, **payload)


# --------------------------------------------------------------------------- #
# provider 契约版本
# --------------------------------------------------------------------------- #


def provider_contract_version(provider: Any) -> int:
    """取 provider 的契约版本。

    `TTSProvider` Protocol **未声明**该字段(frozen 的 `models.py`/`tts/` 不含),
    故按约定读取:实例属性 `contract_version` → 适配器模块常量
    `PROVIDER_CONTRACT_VERSION` → 默认 `1`。新增后端在模块里定义同名常量即可。
    """
    value = getattr(provider, "contract_version", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        module = importlib.import_module(type(provider).__module__)
    except Exception:  # pragma: no cover - 动态导入失败不应影响合成
        return 1
    value = getattr(module, "PROVIDER_CONTRACT_VERSION", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 1


# --------------------------------------------------------------------------- #
# 能力检查(章级,启动前)
# --------------------------------------------------------------------------- #


def required_emotion_kinds(chapter: Chapter, bindings: Any = None) -> set[str]:
    """本章**需要**的情感意图种类。

    全零 `emo_vector` 视为「无情感意图」—— 实测零向量 ≈ 不施加情感引导、安全
    (见 `doc/findings.md` §4),故不构成能力要求。绑定了 `emo_ref` 的角色
    额外要求 `ref_audio`。

    Args:
        bindings: 该工程的 `BindingTable`(角色 → `Binding`;可选)。
    """
    kinds: set[str] = set()
    for sentence in chapter.sentences:
        vector = sentence.emo_vector
        if vector and any(abs(x) > 0 for x in vector):
            kinds.add(EMOTION_KIND_VECTOR)
            break

    if bindings is not None:
        for sentence in chapter.sentences:
            binding = bindings.get(sentence.speaker)
            if binding is not None and getattr(binding, "emo_ref", None):
                kinds.add(EMOTION_KIND_REF_AUDIO)
                break
    return kinds


def check_capabilities(chapter: Chapter, ctx: ChapterContext) -> frozenset[str]:
    """开跑前校验 provider 是否支持本章需要的情感意图。

    按 `ctx.policy` 处理不支持的意图(见 `doc/tts/design.md` §9):

        * `warn`(默认)/`map` —— 记录警告并**丢弃**该意图(不静默降质:显式告警)。
        * `error` —— 拒绝启动,抛 `ConfigError`。

    Returns:
        本次**后端可用的情感方式**集合(`frozenset(supported)`)。`synthesize_one`
        据此决定如何组装 `EmotionSpec` —— 注意返回的是「后端支持的方式」而非
        「本章需要的方式」:全零向量需要 vector 能力才能按 method=2 透传,却不构成
        能力**要求**(requirements.md §5),故不能只返回 required 的交集。
    """
    capabilities = ctx.provider.capabilities
    supported = set(capabilities.emotion)

    # required 只用于「是否需要告警/拒绝」的判定。
    required = required_emotion_kinds(chapter, ctx.project.bindings)
    missing = required - supported
    if missing:
        if ctx.policy == POLICY_ERROR:
            raise ConfigError(
                "当前后端不支持本章需要的情感意图: "
                + ", ".join(sorted(missing))
                + f"(后端支持: {sorted(supported) or '无'})"
            )
        for kind in sorted(missing):
            message = (
                f"后端不支持情感意图 {kind!r},已按策略 {ctx.policy!r} 忽略"
                "(显式告警,不静默降质)"
            )
            logger.warning("%s: %s", chapter.name, message)
            ctx.bus.log(message, level="warn", chapter=chapter.name)

    return frozenset(supported)


# --------------------------------------------------------------------------- #
# 组装情感意图
# --------------------------------------------------------------------------- #


def _build_emotion(
    sentence: Sentence, binding: Any, ctx: ChapterContext
) -> EmotionSpec | None:
    """领域情感意图 —— 四分支,后者依次回落。

    | 顺序 | 条件                                   | 采用                          | 后端模式 |
    |------|----------------------------------------|-------------------------------|----------|
    | 1    | 句子 `emo_vector` **非零**             | `EmotionSpec(vectors=...)`    | method 2 |
    | 2    | 否则,绑定配了 `emo_ref`                | `EmotionSpec(ref_audio=...)`  | method 1 |
    | 3    | 否则,句子**带** `emo_vector`(全零)     | `EmotionSpec(vectors=[0.0]*8)`| method 2 |
    | 4    | 否则                                   | `None`                        | 后端默认 |

    设计要点:

    * **逐句信号比角色级配置更具体,更具体者优先** —— 给角色配了情感参考音频,
      不会静默丢弃该角色各句的逐句情感数据。
    * **`emo_ref`(第 2)优先于全零向量(第 3)**:全零向量不含信息,而 `emo_ref`
      是用户的显式配置。
    * **第 3 分支保留「全零向量仍走 method=2 透传」的用户决策**
      (见 `doc/requirements.md` §5 与 `doc/tts/design.md` §3),不是 `None`。

    前三个分支都要求当前 provider 的 `capabilities.emotion` 支持该方式;不支持则
    继续往下落。能力检查(§9)已把不支持的意图从 `supported_emotion_kinds` 剔除;
    注意**全零向量不构成能力要求**,故 provider 不支持 vector 时第 1、3 分支直接跳过
    (不告警),最终落到第 4 分支 `None`。
    """
    supported = ctx.supported_emotion_kinds
    vector = sentence.emo_vector

    # 1) 逐句非零向量(最具体)。
    if (
        vector
        and any(abs(x) > 0 for x in vector)
        and EMOTION_KIND_VECTOR in supported
    ):
        return EmotionSpec(vectors=list(vector))

    # 2) 角色级情感参考(用户显式配置,优先于无信息的全零向量)。
    emo_ref = getattr(binding, "emo_ref", None)
    if emo_ref and EMOTION_KIND_REF_AUDIO in supported:
        audio = ctx.cache.emotion_audio(binding.voice, emo_ref)  # type: ignore[union-attr]
        return EmotionSpec(ref_audio=audio, weight=1.0)

    # 3) 全零向量仍**显式**走 method=2 透传(用户决策,勿改为 None)。
    if vector is not None and EMOTION_KIND_VECTOR in supported:
        return EmotionSpec(vectors=list(vector))

    # 4) 无可用情感意图。
    return None


# --------------------------------------------------------------------------- #
# 单条片段
# --------------------------------------------------------------------------- #


@dataclass
class FragmentResult:
    """一条片段的合成结果(成功或失败)。"""

    index: int
    speaker: str
    voice: str | None
    provider: str
    status: FragmentStatus
    relpath: str | None = None
    bytes: int | None = None
    attempts: int = 0
    error: str | None = None
    error_kind: str | None = None
    duration_s: float = 0.0
    normalized: bool = False

    @property
    def ok(self) -> bool:
        return self.status is FragmentStatus.DONE


def synthesize_one(sentence: Sentence, idx: int, ctx: ChapterContext) -> FragmentResult:
    """合成一条片段:绑定 → VoiceRef → TTS → (R7 归一化) → 原子写 → 记账。

    单条失败**不抛出** `RetryableError`/`FatalError` —— 记入 manifest 为 `failed`
    并返回 `FragmentResult`(失败隔离,见 `doc/synthesis/design.md` §3)。
    但 `ManifestError`(落盘失败)会向上抛出:那是严重错误,必须停止任务。
    """
    chapter = ctx.chapter
    if chapter is None:  # pragma: no cover - 调用约定:runner 必先设置
        raise RuntimeError("ChapterContext.chapter 未设置")

    start = time.perf_counter()

    # 1) 绑定与音色(Raise BindingError 只应在开跑前被拦截;此处兜底)
    binding = ctx.binding_for(sentence.speaker)
    voice_name = binding.voice

    try:
        voice_ref = ctx.cache.voice_ref(voice_name)  # type: ignore[union-attr]
    except NotFoundError as exc:
        return _record_failure(ctx, sentence, idx, voice_name, "binding", str(exc), None, start)

    try:
        emotion = _build_emotion(sentence, binding, ctx)
    except NotFoundError as exc:
        return _record_failure(ctx, sentence, idx, voice_name, "binding", str(exc), None, start)

    # 2) TTS(适配器内部已退避重试;此处只调用一次)
    request = TTSRequest(
        text=sentence.content,
        voice_ref=voice_ref,
        emotion=emotion,
        params=ctx.params,
    )
    try:
        result = ctx.provider.synthesize(request)
    except RetryableError as exc:
        # 适配器已退避重试并耗尽 → 记为 failed,继续下一条。
        return _record_failure(
            ctx, sentence, idx, voice_name, "retryable", str(exc), ctx.retries + 1, start
        )
    except FatalError as exc:
        return _record_failure(ctx, sentence, idx, voice_name, exc.kind, str(exc), 1, start)

    # 3) R7:归一化异构输出(IndexTTS2 声明 is_canonical → 零开销快路径)
    audio_bytes = result.audio
    normalized = False
    if not result.is_canonical:
        try:
            audio_bytes, _ = to_canonical(result.audio, result.format)
            normalized = True
        except FatalError as exc:
            return _record_failure(
                ctx, sentence, idx, voice_name, "fatal", f"归一化失败: {exc}", 1, start
            )

    manifest = ctx.project.manifest
    assert manifest is not None
    # 原子写 + 记账一次完成:save_fragment 会写文件并记录 bytes(供续传字节数校验)。
    manifest.save_fragment(
        chapter.name,
        idx,
        audio_bytes,
        status=FragmentStatus.DONE,
        speaker=sentence.speaker,
        voice=voice_name,
        provider=ctx.provider.id,
        attempts=0,
        persist=True,
    )
    relpath = manifest.fragment_relpath(chapter.name, idx)

    return FragmentResult(
        index=idx,
        speaker=sentence.speaker,
        voice=voice_name,
        provider=ctx.provider.id,
        status=FragmentStatus.DONE,
        relpath=relpath,
        bytes=len(audio_bytes),
        attempts=0,
        error=None,
        error_kind=None,
        duration_s=time.perf_counter() - start,
        normalized=normalized,
    )


def _record_failure(
    ctx: ChapterContext,
    sentence: Sentence,
    idx: int,
    voice_name: str | None,
    error_kind: str,
    error: str,
    attempts: int | None,
    start: float,
) -> FragmentResult:
    """把失败记入 manifest(逐条落盘)并返回失败结果。"""
    chapter = ctx.chapter
    assert chapter is not None
    manifest = ctx.project.manifest
    assert manifest is not None
    attempts_value = attempts if attempts is not None else 1
    manifest.save_fragment(
        chapter.name,
        idx,
        None,
        status=FragmentStatus.FAILED,
        speaker=sentence.speaker,
        voice=voice_name,
        provider=ctx.provider.id,
        attempts=attempts_value,
        error=error,
        persist=True,
    )
    return FragmentResult(
        index=idx,
        speaker=sentence.speaker,
        voice=voice_name,
        provider=ctx.provider.id,
        status=FragmentStatus.FAILED,
        relpath=None,
        bytes=None,
        attempts=attempts_value,
        error=error,
        error_kind=error_kind,
        duration_s=time.perf_counter() - start,
        normalized=False,
    )


__all__ = [
    "EMOTION_KIND_REF_AUDIO",
    "EMOTION_KIND_TEXT",
    "EMOTION_KIND_VECTOR",
    "POLICY_ERROR",
    "POLICY_MAP",
    "POLICY_WARN",
    "ChapterContext",
    "FragmentResult",
    "ReferenceCache",
    "RunControl",
    "check_capabilities",
    "provider_contract_version",
    "required_emotion_kinds",
    "synthesize_one",
]
