"""M4 编排层测试脚手架(**非测试文件**,不会被 pytest 收集)。

提供:

    * `FakeProvider` —— 通过构造注入(不经 registry),可模拟正常合成、`RetryableError`、
      `FatalError`、非 canonical 输出、能力缺失、并发探测、阻塞(kill -9 测试)。
    * `make_project` / `make_wav` / 真实参考音频 —— 复用仓库真实素材。

**绝不触碰真实后端**(见 doc/dev-workflow.md §0「联调节制」)。
"""

from __future__ import annotations

import io
import json
import os
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.audio import to_reference_audio
from src.errors import FatalError, RetryableError
from src.models import (
    CANONICAL_FORMAT,
    AudioFormat,
    HealthResult,
    TTSResult,
)
from src.project import Project
from src.tts.capabilities import TTSCapabilities
from src.voice import VoiceLibrary

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
REF_AUDIO = (REPO_ROOT / "reference" / "audio" / "丁仪" / "1.wav").read_bytes()

CHAPTER = "P01_扉页"
DEFAULT_VOICE = "男旁白2"


def make_wav(duration_ms: int = 100, sample_rate: int = 22050, channels: int = 1) -> bytes:
    """构造一段真实的 PCM16 WAV(纯内存)。"""
    frames = int(sample_rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames * channels)
    return buffer.getvalue()


FAKE_AUDIO = make_wav(100, 22050)  # 标准格式
FAKE_AUDIO_24K = make_wav(100, 24000)  # 非标准(用于 R7)
FRAGMENT_SECONDS = 0.1


def caps(
    *,
    emotion: tuple[str, ...] = ("vector", "ref_audio", "text"),
    dims: int | None = 8,
    canonical: bool = True,
    concurrent: bool = False,
) -> TTSCapabilities:
    return TTSCapabilities(
        emotion=set(emotion),
        emotion_vector_dims=dims,
        voice_modes={"audio_bytes"},
        needs_prompt_text=False,
        supports_concurrent=concurrent,
        returns_canonical=canonical,
        param_schema=[],
    )


class FakeProvider:
    """离线假 provider(不联网、不占 GPU)。"""

    def __init__(
        self,
        provider_id: str = "fake",
        *,
        capabilities: TTSCapabilities | None = None,
        audio: bytes | None = None,
        fmt: AudioFormat | None = None,
        canonical: bool = True,
        fail_on: set[int] | None = None,
        retry_on: set[int] | None = None,
        block_on: int | None = None,
        block_marker: str | Path | None = None,
        contract_version: int = 1,
        call_log: str | Path | None = None,
        delay: float = 0.0,
        on_call: Callable[[Any, int], None] | None = None,
    ) -> None:
        self.id = provider_id
        self.contract_version = contract_version
        self._caps = capabilities or caps(canonical=canonical)
        self._audio = FAKE_AUDIO if audio is None else audio
        if fmt is None:
            fmt = CANONICAL_FORMAT if canonical else AudioFormat(24000, 1, 16)
        self._fmt = fmt
        self._canonical = canonical
        self.fail_on = set(fail_on or ())
        self.retry_on = set(retry_on or ())
        self.block_on = block_on
        self.block_marker = Path(block_marker) if block_marker else None
        self.call_log = Path(call_log) if call_log else None
        self.delay = delay
        self.on_call = on_call
        self.calls: list[Any] = []
        self.active = 0
        self.max_active = 0

    @property
    def capabilities(self) -> TTSCapabilities:
        return self._caps

    def synthesize(self, req: Any) -> TTSResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            n = len(self.calls) + 1
            self.calls.append(req)
            if self.call_log is not None:
                with open(self.call_log, "a", encoding="utf-8") as handle:
                    handle.write(f"{n}\t{req.text}\n")
            if self.on_call is not None:
                self.on_call(req, n)
            if self.block_on is not None and n == self.block_on:
                self._block()
            if self.delay:
                time.sleep(self.delay)
            if n in self.fail_on:
                raise FatalError(f"注入的致命错误 #{n}")
            if n in self.retry_on:
                raise RetryableError(f"注入的可重试错误 #{n}")
            return TTSResult(
                audio=self._audio, format=self._fmt, is_canonical=self._canonical
            )
        finally:
            self.active -= 1

    def _block(self) -> None:
        if self.block_marker is not None:
            self.block_marker.write_text("blocked", encoding="utf-8")
            with open(self.block_marker, "rb") as handle:
                os.fsync(handle.fileno())
        time.sleep(3600)

    def probe(self) -> HealthResult:
        return HealthResult(ok=True, kind="ok", message="fake")


def make_project(
    tmp_path: Path,
    *,
    n: int = 3,
    speaker: str = "旁白",
    book: str = "三体",
    chapter: str = CHAPTER,
    bind: bool = True,
    voice: str = DEFAULT_VOICE,
    emo_vector: list[float] | None = None,
    delays: list[int] | None = None,
) -> tuple[Project, VoiceLibrary]:
    """建一个工程:一章 n 条、一个音色、一条绑定。"""
    root = tmp_path / book
    Project.create(root, name=book)
    sentences = []
    for i in range(1, n + 1):
        sentences.append(
            {
                "speaker": speaker,
                "content": f"S{i}",
                "emo_vector": list(emo_vector) if emo_vector is not None else [0.0] * 8,
                "delay": delays[i - 1] if delays else 100,
            }
        )
    (root / "json" / f"{chapter}.json").write_text(
        json.dumps(sentences, ensure_ascii=False), encoding="utf-8"
    )
    project = Project.open(root, book=book)

    voices = VoiceLibrary(tmp_path / "voices")
    voices.create(voice, to_reference_audio(REF_AUDIO, max_seconds=1.0), "参考文本")
    if bind:
        assert project.bindings is not None
        project.bindings.set(speaker, voice)
    return project, voices


def write_chapter(root: Path, chapter: str, sentences: list[dict]) -> None:
    (root / "json" / f"{chapter}.json").write_text(
        json.dumps(sentences, ensure_ascii=False), encoding="utf-8"
    )


def sentence(i: int, speaker: str = "旁白", content: str | None = None) -> dict:
    return {
        "speaker": speaker,
        "content": content if content is not None else f"S{i}",
        "emo_vector": [0.0] * 8,
        "delay": 100,
    }
