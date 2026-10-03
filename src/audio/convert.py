"""两条**不同**的转码路径,切勿合并:

* `to_reference_audio` —— 音色**入库**时的上传优化:mono + **只降不升**(≤22050)+ 裁 ≤15s。
* `to_canonical` —— 后端**输出**片段的格式归一化(硬规则 R7 入口):
  任意格式 → 恒为 `CANONICAL_FORMAT`(22050Hz/1ch/16bit),**可升可降、不裁剪**。

二者刻意不一致:入库对 16k 升采样只会变大且无收益(后端内部本就会重采样);
而章节拼接要求所有片段格式**完全一致**,否则静默损坏,故归一化必须**恰好**到 22050。

---

`to_reference_audio` 的规则来源(见 `doc/audio/design.md` §3.2/§3.3、`doc/findings.md` §5、§6):

* **只降不升**:源采样率 > 22050 才降;≤ 22050 保持原样。
  对 16kHz 音色升采样会**变大且无收益**(后端内部本就会重采样)。
* **多声道 → mono**:体积减半。
* **裁剪 ≤15s**:后端 `_load_and_cut_audio(prompt, 15)` 只读前 15 秒,
  超出部分纯属白传流量,裁掉零质量损失。

后端输出恒为 22050Hz/1ch/16bit,故本函数产出的即标准内部 PCM WAV 字节,
供入库与上传使用。全程零 FFmpeg(soundfile + soxr + numpy)。
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
import soundfile as sf
import soxr

from ..errors import FatalError
from ..models import CANONICAL_FORMAT, AudioFormat
from .probe import probe

# 默认值直接绑定冻结常量,避免与 models.py 漂移。
DEFAULT_MAX_SAMPLE_RATE = CANONICAL_FORMAT.sample_rate  # 22050
DEFAULT_MAX_SECONDS = 15.0
_OUT_SUBTYPE = "PCM_16"


def _decode(data: bytes) -> tuple[np.ndarray, int]:
    """解码为 float32 二维数组 (frames, channels) 与源采样率。"""
    if not data:
        raise FatalError("音频数据为空", detail={"reason": "empty"})
    try:
        arr, rate = sf.read(BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # soundfile 异常类型繁杂,统一收敛为不可重试
        raise FatalError(
            f"无法解码音频:{exc}",
            detail={"reason": "undecodable"},
        ) from exc
    if arr.size == 0 or rate <= 0:
        raise FatalError("音频为空或采样率非法", detail={"reason": "empty"})
    return arr, int(rate)


def _to_mono(arr: np.ndarray) -> np.ndarray:
    """多声道降为单声道(取通道均值);已是 mono 则原样返回。"""
    if arr.shape[1] == 1:
        return arr
    return arr.mean(axis=1, keepdims=True)


def _trim(arr: np.ndarray, sample_rate: int, max_seconds: float) -> np.ndarray:
    """截取前 `max_seconds` 秒。"""
    if max_seconds <= 0:
        return arr[:0]
    limit = int(round(max_seconds * sample_rate))
    return arr[:limit] if arr.shape[0] > limit else arr


def _encode_wav_pcm16(arr: np.ndarray, sample_rate: int) -> bytes:
    """把 float32 数组写成标准 PCM_16 WAV 字节。"""
    buf = BytesIO()
    sf.write(buf, arr, sample_rate, format="WAV", subtype=_OUT_SUBTYPE)
    return buf.getvalue()


def _is_riff_wave(data: bytes) -> bool:
    """仅校验约 12 字节的容器头:是否为 RIFF/WAVE(不做解码,零开销)。"""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def to_reference_audio(
    data: bytes,
    *,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_sample_rate: int = DEFAULT_MAX_SAMPLE_RATE,
) -> bytes:
    """把任意输入音频转成标准参考音频字节。

    处理顺序(先裁后降,省算力):解码 → 降 mono → 裁 ≤max_seconds → 条件降采样。

    Args:
        data: 任意可解码的音频字节(格式由内容判定,扩展名无关)。
        max_seconds: 时长上限,默认 15s。
        max_sample_rate: 采样率上限,默认 22050。**只降不升**。

    Returns:
        标准 PCM WAV(单声道 / ≤max_sample_rate / ≤max_seconds / 16bit)字节。

    Raises:
        FatalError: 输入无法解码或为空。
    """
    arr, rate = _decode(data)

    arr = _to_mono(arr)
    arr = _trim(arr, rate, max_seconds)

    out_rate = rate
    if rate > max_sample_rate:
        arr = soxr.resample(arr, rate, max_sample_rate)
        out_rate = max_sample_rate

    return _encode_wav_pcm16(arr, out_rate)


def to_canonical(data: bytes, fmt: AudioFormat | None = None) -> tuple[bytes, AudioFormat]:
    """把任意后端输出归一化为 `CANONICAL_FORMAT`(22050Hz/1ch/16bit PCM WAV)。

    与 `to_reference_audio` **刻意不同**(勿合并):
    本函数**可升可降**采样率、**不裁剪**时长 —— 章节拼接要求所有片段格式完全一致。

    Args:
        data: 后端返回的音频字节(任意可解码格式)。
        fmt: provider 声明的输出格式;为 None 时自行探测。若声明已等于
            `CANONICAL_FORMAT`,走**零开销快路径**原样返回(IndexTTS2 走这条)。

    Returns:
        `(audio, CANONICAL_FORMAT)`,与 architecture.md §5.2 用法对齐:
        `audio, _ = to_canonical(result.audio, result.format)`。

    Raises:
        FatalError: 输入为空或无法解码。
    """
    if not data:
        raise FatalError("音频数据为空", detail={"reason": "empty"})

    # 快路径 1:provider 声明自己已是标准格式 —— 信任声明,不重编码、不重探测。
    #
    # 但要防「provider 谎报」:AudioFormat 只有采样率/声道/位深,无法区分
    # 「22050/1/16 的 WAV」与「同参数的 MP3」。若把 MP3 原样透传,下游 `concat`
    # 会把它当同格式 PCM 直接 bytes 拼接 → **产出损坏音频且不报错**(静默损坏)。
    # 故这里只花 ~12 字节校验容器头;不是 RIFF/WAVE 就不信任声明,落入真实转换路径
    # (真实路径会解码重编码,MP3 会被正确转成 WAV)。不做完整解码,保持快路径零开销。
    if fmt is not None and fmt == CANONICAL_FORMAT and _is_riff_wave(data):
        return data, CANONICAL_FORMAT

    # 快路径 2:未提供声明时自行探测;已是标准格式则原样返回。
    info = probe(data)
    if (
        info.format == "wav"
        and info.sample_rate == CANONICAL_FORMAT.sample_rate
        and info.channels == CANONICAL_FORMAT.channels
        and info.bits == CANONICAL_FORMAT.bits
    ):
        return data, CANONICAL_FORMAT

    arr, rate = _decode(data)
    arr = _to_mono(arr)
    if rate != CANONICAL_FORMAT.sample_rate:
        arr = soxr.resample(arr, rate, CANONICAL_FORMAT.sample_rate)
    return _encode_wav_pcm16(arr, CANONICAL_FORMAT.sample_rate), CANONICAL_FORMAT
