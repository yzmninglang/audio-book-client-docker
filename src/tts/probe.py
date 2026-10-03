"""测试连接(设计见 doc/tts/design.md §5)。

后端**没有健康检查端点**,只有 `/api/tts`。因此:

    测试连接 = 发一次**极短文本的真实合成**(如 "测试连接。")。

这样能真实反映可用性,并把失败区分为四类 `HealthResult.kind`:

    | 现象                          | kind               |
    |-------------------------------|--------------------|
    | 连接被拒 / DNS 失败 / 超时    | unreachable        |
    | 500 + 「模型未成功加载」      | model_not_loaded   |
    | 500 其他                      | server_error       |
    | 400                           | bad_request        |
    | 200                           | ok                 |

本函数只依赖 `synthesize` 可调用对象,因此对**任何** provider 都通用,
不引入后端专有概念。
"""

from __future__ import annotations

import io
import time
import wave
from collections.abc import Callable

import httpx

from ..errors import FatalError, InvalidInputError, RetryableError
from ..models import HealthResult, TTSRequest, TTSResult, VoiceRef
from .retry import (
    REASON_BAD_REQUEST,
    REASON_MODEL_NOT_LOADED,
    REASON_NETWORK,
    REASON_TIMEOUT,
)

PROBE_TEXT = "测试连接。"
"""探测用极短文本。真实合成,但开销最小。"""


def run_probe(
    synthesize: Callable[[TTSRequest], TTSResult],
    voice_ref: VoiceRef | None,
    *,
    probe_text: str = PROBE_TEXT,
) -> HealthResult:
    """用一次极短真实合成探测后端可用性。

    `synthesize` 应为**单次**调用(不套重试),以便精确分类失败原因,
    也避免探测过程反复消耗 GPU。
    """
    if voice_ref is None or voice_ref.is_empty:
        return HealthResult(
            ok=False,
            kind="bad_request",
            message="未提供探测音色(需要一段参考音频字节)",
        )

    request = TTSRequest(text=probe_text, voice_ref=voice_ref)
    started = time.perf_counter()
    try:
        result = synthesize(request)
    except (RetryableError, FatalError, httpx.HTTPError, OSError) as exc:
        # FatalError 已覆盖 InvalidInputError(后者继承前者)。
        return _classify_failure(exc, started)

    latency_ms = _elapsed_ms(started)
    return HealthResult(
        ok=True,
        kind="ok",
        message=f"合成成功,耗时 {latency_ms} ms",
        latency_ms=latency_ms,
        sample_audio=result.audio,
        sample_rate=result.format.sample_rate,
        duration_ms=_wav_duration_ms(result.audio),
    )


def _classify_failure(exc: BaseException, started: float) -> HealthResult:
    latency_ms = _elapsed_ms(started)
    detail = getattr(exc, "detail", None)
    reason = getattr(detail, "reason", None)

    # 注意:`InvalidInputError` 继承 `FatalError`,必须先判定,否则会被 FatalError 分支吞掉。
    if isinstance(exc, InvalidInputError):
        return HealthResult(
            ok=False, kind="bad_request", message=str(exc), latency_ms=latency_ms
        )

    if isinstance(exc, RetryableError):
        if reason in (REASON_NETWORK, REASON_TIMEOUT):
            return HealthResult(
                ok=False,
                kind="unreachable",
                message="后端不可达,请检查地址与网络",
                latency_ms=latency_ms,
            )
        return HealthResult(
            ok=False, kind="server_error", message=str(exc), latency_ms=latency_ms
        )

    if isinstance(exc, FatalError):
        if reason == REASON_MODEL_NOT_LOADED:
            return HealthResult(
                ok=False,
                kind="model_not_loaded",
                message="后端已启动但模型未加载,请检查后端",
                latency_ms=latency_ms,
            )
        if reason == REASON_BAD_REQUEST:
            return HealthResult(
                ok=False, kind="bad_request", message=str(exc), latency_ms=latency_ms
            )
        return HealthResult(
            ok=False, kind="server_error", message=str(exc), latency_ms=latency_ms
        )

    if isinstance(exc, (httpx.HTTPError, OSError, TimeoutError)):
        return HealthResult(
            ok=False,
            kind="unreachable",
            message=f"后端不可达,请检查地址与网络: {exc}",
            latency_ms=latency_ms,
        )

    return HealthResult(
        ok=False,
        kind="server_error",
        message=f"探测时发生未预期错误: {exc}",
        latency_ms=latency_ms,
    )


def _wav_duration_ms(audio: bytes) -> int | None:
    """从 wav 头直接算时长(纯 stdlib,`io.BytesIO` 内存流,不碰文件系统)。

    解析失败(如返回的不是标准 wav)时返回 None,不影响探测成功判定。
    """
    try:
        with wave.open(io.BytesIO(audio)) as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
        if rate <= 0:
            return None
        return int(round(frames * 1000 / rate))
    except (wave.Error, EOFError):
        return None


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))
