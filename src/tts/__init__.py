"""TTS 接入模块。

职责(见 doc/tts/design.md §1):

    把**领域意图**翻译成某个具体 TTS 后端的调用,并把后端的错误翻译回领域语义。

硬规则:
    * 只吃字节、只吐字节 —— 不读文件路径、不碰文件系统(架构 R3)
    * **禁止 import `audio/`**(架构 R1)
    * 不做格式归一化 —— 只声明 `TTSResult.format`,归一化归 `synthesis/`(架构 R7)

扩展方式:**加一个后端 = 加一个文件 + `registry.PROVIDERS` 里注册一行。**
"""

from __future__ import annotations

from .base import (
    CANONICAL_FORMAT,
    AudioFormat,
    EmotionSpec,
    HealthResult,
    TTSProvider,
    TTSRequest,
    TTSResult,
    VoiceRef,
)
from .capabilities import ParamSpec, TTSCapabilities
from .registry import PROVIDERS, create, register

__all__ = [
    "AudioFormat",
    "CANONICAL_FORMAT",
    "EmotionSpec",
    "HealthResult",
    "TTSProvider",
    "TTSRequest",
    "TTSResult",
    "VoiceRef",
    "ParamSpec",
    "TTSCapabilities",
    "PROVIDERS",
    "create",
    "register",
]
