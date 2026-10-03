"""真实格式探测 —— **扩展名不可信**。

实测:37 个参考音频里有 3 个 `.wav`(如 `陈平安1/1.wav`)其实是 MP3
(见 `doc/findings.md` §1.1)。因此判格式只能看**文件内容**,不能看后缀。

判定优先级(见 `doc/audio/design.md` §3.1):

    1. RIFF/WAVE 头      → 按 WAV 解析(stdlib `wave` 直接读 PCM)
    2. ID3 / 0xFFEx 帧同步 → 判定为 MP3
    3. 其他              → 交给解码库(`soundfile`)嗅探
    4. 全部失败          → 抛 `FatalError`(不可重试)

本模块只做「字节 → 元信息」,不碰业务概念(R2)。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import soundfile as sf

from ..errors import FatalError

# 扩展名 → 允许的真实格式集合。仅用于 `is_actually_what_ext_says` 判定,
# 不参与格式解析(解析一律看内容)。
_EXT_TO_FORMATS: dict[str, frozenset[str]] = {
    "wav": frozenset({"wav", "wave"}),
    "wave": frozenset({"wav", "wave"}),
    "mp3": frozenset({"mp3"}),
    "flac": frozenset({"flac"}),
    "ogg": frozenset({"ogg", "oga"}),
    "opus": frozenset({"ogg", "opus"}),
    "aiff": frozenset({"aiff"}),
    "aif": frozenset({"aiff"}),
    "m4a": frozenset({"mp4", "m4a"}),
    "mp4": frozenset({"mp4", "m4a"}),
}


@dataclass(frozen=True)
class AudioInfo:
    """音频的真实格式与参数。字段含义见 `doc/audio/design.md` §4。"""

    format: str  # "wav" | "mp3" | "flac" | ...(真实格式,非扩展名)
    sample_rate: int
    channels: int
    bits: int  # PCM 位深;非 PCM(如 MP3)无位深,记 0
    duration: float  # 秒
    is_actually_what_ext_says: bool = True
    """真实格式是否与文件扩展名相符。

    仅在传入 `filename` 时才有判定意义;未传扩展名线索时默认为 True(不臆断)。
    """

    @property
    def is_pcm(self) -> bool:
        return self.format in ("wav", "wave", "aiff")


def _classify_magic(data: bytes) -> str | None:
    """仅凭 magic bytes 做粗分类:返回 "wav" / "mp3" / None(未知)。"""
    if len(data) < 4:
        return None
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    # MPEG 音频帧同步:11 个连续 1(0xFF 后高 3 位为 1)。
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def _ext(format_or_name: str) -> str:
    """取扩展名(小写,不含点)。接受文件名或裸后缀。"""
    name = format_or_name.lower()
    if "." in name:
        name = name.rsplit(".", 1)[1]
    return name


def _ext_matches(filename: str | None, fmt: str) -> bool:
    if not filename:
        return True  # 无扩展名线索,不臆断
    allowed = _EXT_TO_FORMATS.get(_ext(filename))
    if allowed is None:
        return True  # 未知扩展名,无从比较
    return fmt.lower() in allowed


def _bits_from_subtype(subtype: str) -> int:
    """从 soundfile 的 subtype 推位深;非 PCM/浮点返回 0。"""
    sub = subtype.upper()
    if sub == "FLOAT":
        return 32
    if sub == "DOUBLE":
        return 64
    if sub.startswith("PCM"):
        tail = sub.split("_", 1)[1] if "_" in sub else ""
        if tail.isdigit():
            return int(tail)
        if tail in ("S8", "U8"):
            return 8
    return 0


def _available_data_bytes(data: bytes) -> int | None:
    """扫描 RIFF 块,返回 `data` 块**实际存在**的字节数(用于夹紧被截断的头部)。

    真实素材里存在「头部声明的 data 尺寸 > 文件实际长度」的半截文件
    (如 `标准女声/1.wav`:头部声明 486400 字节,文件只放了 187198 字节)。
    stdlib `wave` 会照头返回 `getnframes()`,导致时长虚高;此处按物理可用字节夹紧,
    与解码器(soundfile)看到的实际帧数一致。
    """
    pos = 12  # 跳过 "RIFF" + size + "WAVE"
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        payload = pos + 8
        if cid == b"data":
            return min(size, max(0, len(data) - payload))
        pos = payload + size + (size & 1)  # 块按偶数字节对齐
    return None


def _probe_wav_stdlib(data: bytes) -> AudioInfo:
    """用 stdlib `wave` 直接解析 RIFF/WAVE(设计 §3.5:WAV 走 stdlib)。"""
    import wave

    with wave.open(BytesIO(data), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        header_frames = w.getnframes()

    block_align = sampwidth * channels
    frames = header_frames
    available = _available_data_bytes(data)
    if available is not None and block_align > 0:
        frames = min(header_frames, available // block_align)

    duration = frames / rate if rate else 0.0
    return AudioInfo(
        format="wav",
        sample_rate=rate,
        channels=channels,
        bits=sampwidth * 8,
        duration=duration,
    )


def _probe_with_soundfile(data: bytes) -> AudioInfo:
    """用 `soundfile` 嗅探非 WAV(或 stdlib 读不动的 WAV)。"""
    try:
        info = sf.info(BytesIO(data))
    except Exception as exc:  # soundfile 抛的异常类型繁杂,统一收敛
        raise FatalError(
            f"无法识别音频格式(解码库嗅探失败):{exc}",
            detail={"reason": "undecodable"},
        ) from exc
    if info.samplerate <= 0 or info.frames <= 0:
        raise FatalError("音频为空或采样率非法", detail={"reason": "empty"})
    return AudioInfo(
        format=info.format.lower(),
        sample_rate=int(info.samplerate),
        channels=int(info.channels),
        bits=_bits_from_subtype(info.subtype),
        duration=info.frames / info.samplerate,
    )


def probe(data: bytes, *, filename: str | None = None) -> AudioInfo:
    """探测音频真实格式与参数。

    Args:
        data: 音频字节(不做任何假设)。
        filename: 可选的文件名,仅用于判定 `is_actually_what_ext_says`;

    Returns:
        `AudioInfo`,其中 `format` 来自**内容**而非扩展名。

    Raises:
        FatalError: 数据为空,或所有解码路径都无法识别格式(不可重试)。
    """
    if not data:
        raise FatalError("音频数据为空", detail={"reason": "empty"})

    kind = _classify_magic(data)

    info: AudioInfo | None = None
    if kind == "wav":
        try:
            info = _probe_wav_stdlib(data)
        except Exception:
            info = None  # 罕见 WAV 变体(如 IEEE float / EXTENSIBLE)回落解码库
    if info is None:
        info = _probe_with_soundfile(data)

    if filename is None:
        return info
    return AudioInfo(
        format=info.format,
        sample_rate=info.sample_rate,
        channels=info.channels,
        bits=info.bits,
        duration=info.duration,
        is_actually_what_ext_says=_ext_matches(filename, info.format),
    )
