"""静音插入 + 同格式 PCM 拼接。

后端输出**恒为 22050Hz/16bit/mono**(见 `doc/findings.md` §5),所以章节拼接是
**同格式 PCM 直接连接、零重采样**,纯 stdlib `wave` 即可,不需要 FFmpeg。

**禁止追加淡入淡出**:后端已对每个片段做过 30ms 淡出(`ui-api.py` → `audio.fade_out(30)`),
客户端再叠加会产生可听见的段间凹陷(见 `doc/audio/design.md` §3.4)。
"""

from __future__ import annotations

import wave
from io import BytesIO

from ..errors import InvalidInputError
from .probe import AudioInfo


def silence(ms: int, sample_rate: int, channels: int, sampwidth: int) -> bytes:
    """生成 `ms` 毫秒的纯静音原始 PCM 帧(bytes)。

    Args:
        ms: 静音时长(毫秒),负数按 0 处理。
        sample_rate: 采样率。
        channels: 声道数。
        sampwidth: 每样本字节数(16bit → 2)。

    Returns:
        原始 PCM 静音帧(`b"\\x00" * n`),**不含 WAV 头**。
    """
    if ms <= 0:
        return b""
    n_samples = int(sample_rate * ms / 1000)
    return b"\x00" * (n_samples * channels * sampwidth)


def _read_fragment(fragment: bytes, template: AudioInfo) -> bytes:
    """读取一个片段的原始 PCM 帧,并校验其格式与 template 一致。"""
    if not fragment:
        raise InvalidInputError("片段为空")
    try:
        with wave.open(BytesIO(fragment), "rb") as w:
            params = (w.getnchannels(), w.getframerate(), w.getsampwidth())
            frames = w.readframes(w.getnframes())
    except wave.Error as exc:
        raise InvalidInputError(
            f"片段不是可解析的 PCM WAV:{exc}",
            detail={"reason": "not_pcm_wav"},
        ) from exc

    expected = (template.channels, template.sample_rate, template.bits // 8)
    if params != expected:
        raise InvalidInputError(
            "片段格式与模板不一致,无法零重采样直连",
            detail={"fragment": params, "template": expected},
        )
    return frames


def _pack_wav(pcm: bytes, template: AudioInfo) -> bytes:
    """把原始 PCM 帧包成标准 WAV 字节。"""
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(template.channels)
        w.setsampwidth(template.bits // 8)
        w.setframerate(template.sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def concat(
    fragments: list[bytes],
    delays: list[int],
    *,
    template: AudioInfo,
) -> bytes:
    """把有序片段按 `delay` 插入静音后拼成章节成品。

    输出 = `frag_0 + silence(delay_0) + frag_1 + silence(delay_1) + ...`,
    因此时长恒为 `Σ片段时长 + Σdelay`(`delay` 是「本条之后」的静音,见 `Sentence.delay_ms`)。

    Args:
        fragments: 有序的 WAV 片段字节,格式须与 `template` 一致。
        delays: 每段之后的静音毫秒数,长度须与 `fragments` 相同。
        template: 目标格式(采样率/声道/位深)。

    Returns:
        标准 PCM WAV 字节。

    Raises:
        InvalidInputError: 片段与 delay 数量不等,或片段格式/可解析性不满足。
    """
    if len(fragments) != len(delays):
        raise InvalidInputError(
            "片段数与静音数不匹配",
            detail={"fragments": len(fragments), "delays": len(delays)},
        )

    sampwidth = template.bits // 8
    out = bytearray()
    for fragment, delay in zip(fragments, delays, strict=True):
        out += _read_fragment(fragment, template)
        out += silence(delay, template.sample_rate, template.channels, sampwidth)
    return _pack_wav(bytes(out), template)
