"""章节 JSON 解析 + 角色聚合(design §3)。

输入结构(实测 `book-json/P01_扉页.json`):

    [{"speaker": "旁白", "content": "扉页",
      "emo_vector": [0,0,0,0,0,0,0,0], "delay": 600}, ...]

**解析容错**(design §3,不中断):

    * `emo_vector` 缺失或长度 ≠ 8 → 按全零处理并告警
    * `delay` 缺失 → 0;为负 → 归零并告警
    * `content` 为空 → **跳过该条,且不占片段编号**

`Chapter`(含 `json_relpath` / `json_sha256`)来自冻结的 `models.py`,
本模块只负责「字节/文件 → Chapter + 告警」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InvalidInputError, NotFoundError
from ..models import EMOTION_VECTOR_DIMS, Chapter, Sentence


@dataclass
class ChapterParseResult:
    """章节解析结果:`Chapter` + 解析告警(模型无 warnings 字段,故分开携带)。"""

    chapter: Chapter
    warnings: list[str] = field(default_factory=list)


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _coerce_emo_vector(raw: object, where: str, warnings: list[str]) -> list[float]:
    """把 emo_vector 归一成 8 维浮点;缺失/长度不符/非数值 → 全零 + 告警。"""
    if isinstance(raw, (list, tuple)) and len(raw) == EMOTION_VECTOR_DIMS:
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            warnings.append(f"{where}:emo_vector 含非数值,按全零处理")
            return [0.0] * EMOTION_VECTOR_DIMS
    if raw is None:
        warnings.append(f"{where}:emo_vector 缺失,按全零处理")
    else:
        warnings.append(f"{where}:emo_vector 维度不是 {EMOTION_VECTOR_DIMS},按全零处理")
    return [0.0] * EMOTION_VECTOR_DIMS


def _coerce_delay(raw: object, where: str, warnings: list[str]) -> int:
    """delay 缺失 → 0;为负 → 归零并告警。"""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        warnings.append(f"{where}:delay 非法({raw!r}),按 0 处理")
        return 0
    value = int(raw)
    if value < 0:
        warnings.append(f"{where}:delay 为负({raw!r}),归零")
        return 0
    return value


def parse_raw_sentences(raw: object, *, chapter_name: str = "") -> tuple[list[Sentence], list[str]]:
    """把章节 JSON 的顶层对象(应为 list)解析为有序 `Sentence` 列表。

    空 `content` 的条目被跳过且**不占片段编号**;编号从 1 起连续。
    """
    if not isinstance(raw, list):
        raise InvalidInputError(
            f"章节 JSON 顶层必须是数组,实为 {type(raw).__name__}",
            detail={"chapter": chapter_name},
        )

    sentences: list[Sentence] = []
    warnings: list[str] = []
    next_index = 1

    for pos, item in enumerate(raw):
        where = f"{chapter_name} 第 {pos + 1} 项" if chapter_name else f"第 {pos + 1} 项"
        if not isinstance(item, dict):
            warnings.append(f"{where}:不是对象,跳过")
            continue

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            warnings.append(f"{where}:content 为空,跳过(不占编号)")
            continue

        speaker_raw = item.get("speaker")
        speaker = speaker_raw if isinstance(speaker_raw, str) else ""
        if not speaker:
            warnings.append(f"{where}:speaker 缺失,记为未命名角色")

        sentences.append(
            Sentence(
                index=next_index,
                speaker=speaker,
                content=content,
                emo_vector=_coerce_emo_vector(item.get("emo_vector"), where, warnings),
                delay_ms=_coerce_delay(item.get("delay"), where, warnings),
            )
        )
        next_index += 1

    return sentences, warnings


def parse_chapter(
    json_path: Path | str,
    *,
    root: Path | str | None = None,
    name: str | None = None,
) -> ChapterParseResult:
    """解析一个章节 JSON 文件。

    Args:
        json_path: 章节 JSON 路径。
        root: 工程根;给出时 `Chapter.json_relpath` 为**相对工程根**的路径
            (容器化约束 #9:只存相对路径)。
        name: 章节名(默认取文件名去扩展名)。

    Raises:
        NotFoundError: 文件不存在。
        InvalidInputError: JSON 无法解析,或顶层不是数组。
    """
    json_path = Path(json_path)
    try:
        data = json_path.read_bytes()
    except FileNotFoundError as exc:
        raise NotFoundError(f"章节文件不存在: {json_path}") from exc
    except OSError as exc:
        raise InvalidInputError(f"读取章节文件失败: {json_path}: {exc}") from exc

    chapter_name = name or json_path.stem
    if root is not None:
        try:
            relpath = json_path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            relpath = json_path.name
    else:
        relpath = json_path.name

    return parse_chapter_bytes(data, name=chapter_name, json_relpath=relpath)


def parse_chapter_bytes(
    data: bytes,
    *,
    name: str,
    json_relpath: str,
) -> ChapterParseResult:
    """从字节解析章节(便于测试与导入场景)。"""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"章节 JSON 无法解析: {name}: {exc}") from exc

    sentences, warnings = parse_raw_sentences(raw, chapter_name=name)
    chapter = Chapter(
        name=name,
        json_relpath=json_relpath,
        sentences=sentences,
        json_sha256=compute_sha256(data),
    )
    return ChapterParseResult(chapter=chapter, warnings=warnings)


def aggregate_roles(chapters: list[Chapter]) -> list[str]:
    """聚合多个章节的 `speaker` 去重,得到工程角色列表(排序)。"""
    roles: set[str] = set()
    for chapter in chapters:
        for sentence in chapter.sentences:
            if sentence.speaker:
                roles.add(sentence.speaker)
    return sorted(roles)
