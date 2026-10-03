"""音色库:`<voices_root>/<音色名>/` 的增删改查 + 入库标准化。

职责与边界见 `doc/voice/design.md` §1/§5。可以 import `audio/`(探测 + 转码),不关心工程。

入库标准化(design §4):新建/导入音色时调
`audio.to_reference_audio()` 得到 mono + 只降不升(≤22050)+ 裁 ≤15s 的标准 PCM WAV,
覆写为 `1.wav`,并写 `.normalized` 标记避免重复转码。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from ..audio import to_reference_audio
from ..audio.convert import DEFAULT_MAX_SAMPLE_RATE, DEFAULT_MAX_SECONDS
from ..errors import InvalidInputError, NotFoundError
from .entry import (
    MAIN_AUDIO_STEM,
    NORMALIZED_MARKER,
    PROMPT_TEXT_FILENAME,
    VoiceEntry,
    is_copy_stem,
    parse_voice_dir,
    validate_voice_name,
)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """临时文件 + `os.replace()` 原子改名(避免半截文件)。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


class VoiceLibrary:
    """一个根目录下的音色集合。根目录来自 `Settings.voices_root`。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        max_sample_rate: int = DEFAULT_MAX_SAMPLE_RATE,
    ) -> None:
        self.root = Path(root)
        self.max_seconds = max_seconds
        self.max_sample_rate = max_sample_rate

    # -- 查询 ------------------------------------------------------------- #

    def list(self) -> list[VoiceEntry]:
        """扫描根目录,按 §3 解析,返回所有**有效**音色(目录名排序)。

        无效目录(无可用音频)不作为音色列出。
        """
        if not self.root.is_dir():
            return []
        entries: list[VoiceEntry] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                entry = parse_voice_dir(child)
            except InvalidInputError:
                continue  # 目录名不合法 → 不作为音色
            if entry is not None:
                entries.append(entry)
        return entries

    def exists(self, name: str) -> bool:
        validate_voice_name(name)
        return (self.root / name).is_dir()

    def get(self, name: str) -> VoiceEntry:
        """取单个音色。

        Raises:
            NotFoundError: 目录不存在,或目录内无可用音频。
        """
        validate_voice_name(name)
        directory = self.root / name
        if not directory.is_dir():
            raise NotFoundError(f"音色不存在: {name}")
        entry = parse_voice_dir(directory)
        if entry is None:
            raise NotFoundError(f"音色目录无可用音频: {name}")
        return entry

    # -- 变更 ------------------------------------------------------------- #

    def create(self, name: str, audio: bytes, prompt_text: str | None) -> VoiceEntry:
        """新建音色:入库标准化 → 建目录。

        Raises:
            InvalidInputError: 名字不合法,或音色已存在。
        """
        validate_voice_name(name)
        directory = self.root / name
        if directory.exists():
            raise InvalidInputError(f"音色已存在: {name}", detail={"name": name})

        directory.mkdir(parents=True, exist_ok=False)
        try:
            self._write_main_audio(directory, audio)
            if prompt_text is not None:
                _atomic_write_text(directory / PROMPT_TEXT_FILENAME, prompt_text)
            self._write_normalized_marker(directory)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return self.get(name)

    def rename(self, old: str, new: str) -> None:
        """重命名音色目录。

        Raises:
            InvalidInputError: 新名字不合法,或目标已存在(不静默覆盖)。
            NotFoundError: 原音色不存在。
        """
        validate_voice_name(old)
        validate_voice_name(new)
        src = self.root / old
        dst = self.root / new
        if not src.is_dir():
            raise NotFoundError(f"音色不存在: {old}")
        if dst.exists():
            raise InvalidInputError(f"重命名冲突,目标已存在: {new}", detail={"name": new})
        os.rename(src, dst)

    def update(
        self,
        name: str,
        *,
        audio: bytes | None = None,
        prompt_text: str | None = None,
    ) -> VoiceEntry:
        """编辑音色:替换主参考音频 / 修改 `pt.txt`。

        `prompt_text=None` 表示**不改动**;传空串 `""` 表示清除 `pt.txt`。

        Raises:
            NotFoundError: 音色不存在。
        """
        validate_voice_name(name)
        directory = self.root / name
        if not directory.is_dir():
            raise NotFoundError(f"音色不存在: {name}")

        if audio is not None:
            self._write_main_audio(directory, audio)
            self._write_normalized_marker(directory)

        if prompt_text is not None:
            prompt_file = directory / PROMPT_TEXT_FILENAME
            if prompt_text == "":
                prompt_file.unlink(missing_ok=True)
            else:
                _atomic_write_text(prompt_file, prompt_text)

        return self.get(name)

    def delete(self, name: str) -> None:
        """删除音色目录(引用检查由上层 `project/` 提供,不在此处)。

        Raises:
            NotFoundError: 音色不存在。
        """
        validate_voice_name(name)
        directory = self.root / name
        if not directory.is_dir():
            raise NotFoundError(f"音色不存在: {name}")
        shutil.rmtree(directory)

    # -- 取参考音频(供 synthesis/tts 使用) ------------------------------ #

    def reference_audio(self, name: str) -> bytes:
        """返回主参考音频字节(已在入库时标准化)。

        Raises:
            NotFoundError: 音色不存在或无可用音频。
        """
        return Path(self.get(name).main_audio).read_bytes()

    def emotion_reference_audio(self, name: str, emo_name: str) -> bytes:
        """返回某情感参考音频的字节。

        Raises:
            NotFoundError: 音色不存在,或该情感参考不存在。
        """
        entry = self.get(name)
        try:
            path = entry.emo_refs[emo_name]
        except KeyError as exc:
            raise NotFoundError(f"音色 {name} 无情感参考: {emo_name}") from exc
        return Path(path).read_bytes()

    # -- 内部 ------------------------------------------------------------- #

    def _write_main_audio(self, directory: Path, audio: bytes) -> None:
        normalized = to_reference_audio(
            audio, max_seconds=self.max_seconds, max_sample_rate=self.max_sample_rate
        )
        _atomic_write_bytes(directory / f"{MAIN_AUDIO_STEM}.wav", normalized)

    def _write_normalized_marker(self, directory: Path) -> None:
        marker = {
            "normalized": True,
            "max_seconds": self.max_seconds,
            "max_sample_rate": self.max_sample_rate,
            "at": int(time.time()),
        }
        _atomic_write_text(
            directory / NORMALIZED_MARKER,
            json.dumps(marker, ensure_ascii=False, indent=2),
        )


# 便捷:目录名合法性校验复用(供上层/测试)
__all__ = ["VoiceLibrary", "VoiceEntry", "validate_voice_name", "is_copy_stem"]
