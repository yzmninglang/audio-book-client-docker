"""音频处理模块(M1)。

职责(见 `doc/audio/design.md`):**真实的音频字节处理** ——
格式探测、解码、转码、只降不升重采样、裁剪、静音插入、拼接、base64 编解码。

边界(硬规则 R1/R2):

    ✗ 不认识「音色」「工程」「后端」等业务概念
    ✗ 不 import `tts/` 或任何业务模块
    ✗ 不发起网络请求、不依赖 FFmpeg
    ✓ 纯函数式:输入字节/路径,输出字节/元信息 —— 可单独离线单测

对外入口集中在此,调用方只 `from src.audio import probe, to_reference_audio, ...`。
"""

from __future__ import annotations

from .codec import b64_and_estimate, b64_decode, estimate_upload_bytes
from .convert import to_canonical, to_reference_audio
from .probe import AudioInfo, probe
from .segment import concat, silence

__all__ = [
    "AudioInfo",
    "b64_and_estimate",
    "b64_decode",
    "concat",
    "estimate_upload_bytes",
    "probe",
    "silence",
    "to_canonical",
    "to_reference_audio",
]
