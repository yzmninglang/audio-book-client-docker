"""续传台账 manifest —— **全项目最容易出错的地方**(design §5,请重点过目)。

三条硬规则(design §5.3):

    ① 原子写   片段先写 `<name>.wav.tmp` → `os.replace()` 原子改名
    ② hash 校验 每章记 `json_sha256`,不匹配则**作废该章全部片段**
    ③ 启动对账  用磁盘实际状态与 manifest 交叉校验(§5.4,含第 0 步 provider 来源检查)

外加:

    * **只存相对工程根的路径**(容器化约束 #9;存绝对路径换挂载点后断点续传直接残废)
    * **每完成一条片段就落盘一次**(§5.6);落盘走原子写
    * 落盘失败抛 `ManifestError`(严重错误,不能继续合成)

`FragmentRecord`/`ChapterRecord`/`ChapterStatus`/`FragmentStatus`/`FRAGMENT_INDEX_WIDTH`
来自冻结的 `models.py`。§7 决定「`failed`/重试过的片段额外存 sha256」,
而 `FragmentRecord` 无该字段 —— 故在**本模块内**派生 `FragmentEntry` 承载 sha256
(不修改冻结模型)。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..errors import FatalError, ManifestError
from ..models import (
    FRAGMENT_INDEX_WIDTH,
    Chapter,
    ChapterRecord,
    ChapterStatus,
    FragmentRecord,
    FragmentStatus,
)
from .chapter import compute_sha256
from .workspace import (
    AUDIO_DIRNAME,
    MANIFEST_FILENAME,
    PROCESS_AUDIO_DIRNAME,
    STALE_DIRNAME,
)

MANIFEST_VERSION = 1
MIN_FRAGMENT_BYTES = 1024
"""片段文件的最小可信字节数(design §5.4 第 4 条「> 最小字节数」)。

设计未给具体数值 —— 取 1KiB:任何真实 WAV 片段都远大于它,
而半截写入的残片几乎必然小于它。
"""


# --------------------------------------------------------------------------- #
# 原子写(小工具,失败抛出 OSError,由调用方翻译成领域异常)
# --------------------------------------------------------------------------- #


def atomic_write_bytes(target: Path | str, data: bytes) -> None:
    """`<target>.tmp` 写入后 `os.replace()` 原子改名。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def atomic_write_text(target: Path | str, text: str) -> None:
    """原子写文本(显式 utf-8 —— 容器化约束 #3)。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def atomic_write_json(target: Path | str, obj: object) -> None:
    atomic_write_text(target, json.dumps(obj, ensure_ascii=False, indent=2))


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# 记录模型
# --------------------------------------------------------------------------- #


@dataclass
class FragmentEntry(FragmentRecord):
    """`FragmentRecord` + §7 的 `sha256`(仅 failed/重试过的片段会落盘)。"""

    sha256: str | None = None


def fragment_key(index: int) -> str:
    """片段序号 → manifest 键 / 文件名主名(零填充,`FRAGMENT_INDEX_WIDTH` 位)。"""
    return f"{index:0{FRAGMENT_INDEX_WIDTH}d}"


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def _fragment_to_dict(frag: FragmentRecord) -> dict[str, object]:
    out: dict[str, object] = {"status": frag.status.value}
    if frag.bytes is not None:
        out["bytes"] = frag.bytes
    if frag.speaker is not None:
        out["speaker"] = frag.speaker
    if frag.voice is not None:
        out["voice"] = frag.voice
    if frag.provider is not None:
        out["provider"] = frag.provider
    if frag.attempts:
        out["attempts"] = frag.attempts
    if frag.error is not None:
        out["error"] = frag.error
    sha = getattr(frag, "sha256", None)
    if sha is not None and (frag.attempts > 0 or frag.status is FragmentStatus.FAILED):
        out["sha256"] = sha
    return out


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _fragment_from_dict(key: str, data: Mapping[str, object]) -> FragmentEntry:
    try:
        index = int(key)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"manifest 片段键非法: {key!r}") from exc
    raw_status = data.get("status", FragmentStatus.PENDING.value)
    try:
        status = FragmentStatus(str(raw_status))
    except ValueError as exc:
        raise ManifestError(f"manifest 片段状态非法: {raw_status!r}") from exc
    return FragmentEntry(
        index=index,
        status=status,
        bytes=_opt_int(data.get("bytes")),
        speaker=_opt_str(data.get("speaker")),
        voice=_opt_str(data.get("voice")),
        provider=_opt_str(data.get("provider")),
        attempts=_opt_int(data.get("attempts")) or 0,
        error=_opt_str(data.get("error")),
        sha256=_opt_str(data.get("sha256")),
    )


def _chapter_to_dict(rec: ChapterRecord) -> dict[str, object]:
    out: dict[str, object] = {
        "json_sha256": rec.json_sha256,
        "sentence_count": rec.sentence_count,
        "status": rec.status.value,
        "fragments": {k: _fragment_to_dict(v) for k, v in sorted(rec.fragments.items())},
    }
    if rec.output is not None:
        out["output"] = rec.output
    if rec.provider is not None:
        out["provider"] = rec.provider
    if rec.provider_contract_version is not None:
        out["provider_contract_version"] = rec.provider_contract_version
    return out


def _chapter_from_dict(name: str, data: Mapping[str, object]) -> ChapterRecord:
    raw_status = data.get("status", ChapterStatus.PENDING.value)
    try:
        status = ChapterStatus(str(raw_status))
    except ValueError as exc:
        raise ManifestError(f"manifest 章节状态非法: {raw_status!r}") from exc
    fragments_raw = data.get("fragments", {}) or {}
    if not isinstance(fragments_raw, Mapping):
        raise ManifestError(f"manifest 章节 {name} 的 fragments 必须是对象")
    fragments: dict[str, FragmentRecord] = {
        str(key): _fragment_from_dict(str(key), dict(value))
        for key, value in fragments_raw.items()
        if isinstance(value, Mapping)
    }
    return ChapterRecord(
        name=name,
        json_sha256=str(data.get("json_sha256", "")),
        sentence_count=_opt_int(data.get("sentence_count")) or 0,
        status=status,
        output=_opt_str(data.get("output")),
        provider=_opt_str(data.get("provider")),
        provider_contract_version=_opt_int(data.get("provider_contract_version")),
        fragments=fragments,
    )


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


class Manifest:
    """`<project_root>/.manifest.json` 的读写与逐片段落盘。"""

    def __init__(
        self,
        project_root: str | Path,
        book: str,
        *,
        chapters: dict[str, ChapterRecord] | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.root = Path(project_root)
        self.book = book
        self._chapters: dict[str, ChapterRecord] = chapters or {}
        self.updated_at = updated_at

    # -- 路径 ------------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    def chapter_audio_relpath(self, chapter: str) -> str:
        """成品相对路径:`audio/<章>.wav`(只存相对路径)。"""
        return f"{AUDIO_DIRNAME}/{chapter}.wav"

    def chapter_audio_path(self, chapter: str) -> Path:
        return self.root / self.chapter_audio_relpath(chapter)

    def fragment_dir(self, chapter: str) -> Path:
        return self.root / PROCESS_AUDIO_DIRNAME / chapter

    def fragment_relpath(self, chapter: str, index: int) -> str:
        return f"{PROCESS_AUDIO_DIRNAME}/{chapter}/{fragment_key(index)}.wav"

    def fragment_path(self, chapter: str, index: int) -> Path:
        return self.fragment_dir(chapter) / f"{fragment_key(index)}.wav"

    # -- 加载 / 保存 ------------------------------------------------------ #

    @classmethod
    def load(cls, project_root: str | Path, *, book: str | None = None) -> Manifest:
        root = Path(project_root)
        book_name = book or root.name
        path = root / MANIFEST_FILENAME
        if not path.is_file():
            return cls(root, book_name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"读取 manifest 失败: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError(f"manifest 顶层必须是对象: {path}")
        version = raw.get("version", 1)
        if version != MANIFEST_VERSION:
            raise ManifestError(f"不支持的 manifest 版本: {version!r}")
        chapters_raw = raw.get("chapters", {}) or {}
        if not isinstance(chapters_raw, Mapping):
            raise ManifestError("manifest.chapters 必须是对象")
        chapters = {
            str(name): _chapter_from_dict(str(name), dict(value))
            for name, value in chapters_raw.items()
            if isinstance(value, Mapping)
        }
        return cls(
            root,
            str(raw.get("book", book_name)),
            chapters=chapters,
            updated_at=raw.get("updated_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": MANIFEST_VERSION,
            "book": self.book,
            "updated_at": self.updated_at or _now_iso(),
            "chapters": {k: _chapter_to_dict(v) for k, v in sorted(self._chapters.items())},
        }

    def save(self) -> None:
        """原子落盘。失败抛 `ManifestError`(严重错误,不可继续合成)。"""
        self.updated_at = _now_iso()
        try:
            atomic_write_json(self.path, self.to_dict())
        except OSError as exc:
            raise ManifestError(f"写入 manifest 失败: {self.path}: {exc}") from exc

    # -- 章节访问 --------------------------------------------------------- #

    @property
    def chapters(self) -> dict[str, ChapterRecord]:
        return self._chapters

    def chapter(self, name: str) -> ChapterRecord | None:
        return self._chapters.get(name)

    def set_chapter(self, record: ChapterRecord) -> None:
        self._chapters[record.name] = record

    def get_or_create(
        self,
        name: str,
        *,
        json_sha256: str = "",
        sentence_count: int = 0,
        provider: str | None = None,
        provider_contract_version: int | None = None,
    ) -> ChapterRecord:
        """取章节记录;不存在则新建。**不覆盖**已存在记录的字段。"""
        rec = self._chapters.get(name)
        if rec is None:
            rec = ChapterRecord(
                name=name,
                json_sha256=json_sha256,
                sentence_count=sentence_count,
                status=ChapterStatus.PENDING,
                provider=provider,
                provider_contract_version=provider_contract_version,
            )
            self._chapters[name] = rec
        return rec

    # -- 片段 ------------------------------------------------------------- #

    def write_fragment_bytes(self, chapter: str, index: int, data: bytes) -> str:
        """把片段字节**原子写**到 `process_audio/<章>/<0001>.wav`(规则 ①)。"""
        target = self.fragment_path(chapter, index)
        try:
            atomic_write_bytes(target, data)
        except OSError as exc:
            raise ManifestError(f"写入片段失败: {target}: {exc}") from exc
        return self.fragment_relpath(chapter, index)

    def record_fragment(
        self, chapter: str, fragment: FragmentRecord, *, persist: bool = True
    ) -> FragmentEntry:
        """记录一条片段并在需要时落盘(§5.6:每完成一条就落盘一次)。"""
        rec = self._chapters.get(chapter)
        if rec is None:
            raise ManifestError(f"manifest 无该章节记录: {chapter}")
        entry = (
            fragment
            if isinstance(fragment, FragmentEntry)
            else FragmentEntry(
                index=fragment.index,
                status=fragment.status,
                bytes=fragment.bytes,
                speaker=fragment.speaker,
                voice=fragment.voice,
                provider=fragment.provider,
                attempts=fragment.attempts,
                error=fragment.error,
                sha256=getattr(fragment, "sha256", None),
            )
        )
        rec.fragments[fragment_key(entry.index)] = entry
        if persist:
            self.save()
        return entry

    def save_fragment(
        self,
        chapter: str,
        index: int,
        data: bytes | None = None,
        *,
        status: FragmentStatus = FragmentStatus.DONE,
        speaker: str | None = None,
        voice: str | None = None,
        provider: str | None = None,
        attempts: int = 0,
        error: str | None = None,
        sha256: str | None = None,
        persist: bool = True,
    ) -> FragmentEntry:
        """写片段文件(可选)+ 记录 + 落盘。冻结模型下最常用的一步到位入口。

        `data=None` 表示片段**已由调用方落盘**(如先 `write_fragment_bytes()` 再补记账):
        此时从磁盘文件实际大小推断 `bytes`;文件也不存在才留 `None`。
        否则 `bytes=None` 会让对账的 `frag.bytes == size` 判据恒假,把已完成片段误判为未完成
        → 续传全部重做(静默回退)。
        """
        if data is not None:
            self.write_fragment_bytes(chapter, index, data)
            size: int | None = len(data)
        else:
            path = self.fragment_path(chapter, index)
            size = path.stat().st_size if path.is_file() else None
        if (
            sha256 is None
            and data is not None
            and (attempts > 0 or status is FragmentStatus.FAILED)
        ):
            sha256 = compute_sha256(data)
        entry = FragmentEntry(
            index=index,
            status=status,
            bytes=size,
            speaker=speaker,
            voice=voice,
            provider=provider,
            attempts=attempts,
            error=error,
            sha256=sha256,
        )
        return self.record_fragment(chapter, entry, persist=persist)


# --------------------------------------------------------------------------- #
# 启动对账(design §5.4)
# --------------------------------------------------------------------------- #


@dataclass
class ReconcileAction:
    chapter: str
    action: str  # skip | redo | resume | stale
    reason: str = ""
    todo: list[int] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)
    adopted: list[int] = field(default_factory=list)


@dataclass
class ReconcileReport:
    provider: str
    provider_contract_version: int
    actions: list[ReconcileAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def action_for(self, chapter: str) -> ReconcileAction | None:
        for action in self.actions:
            if action.chapter == chapter:
                return action
        return None

    @property
    def todo_indices(self) -> dict[str, list[int]]:
        return {a.chapter: list(a.todo) for a in self.actions if a.todo}

    @property
    def chapters_to_redo(self) -> list[str]:
        return [a.chapter for a in self.actions if a.action == "redo"]


def _is_valid_fragment_file(path: Path, min_bytes: int) -> bool:
    """无记录但文件存在时:校验可解码且 > 最小字节数(design §5.4 第 4 条)。"""
    from ..audio import probe

    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) <= min_bytes:
        return False
    try:
        info = probe(data)
    except FatalError:
        return False
    return info.duration > 0


def _clear_dir(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _move_to_stale(root: Path, src: Path) -> Path | None:
    """把被作废的成品移到 `audio/.stale/`(保留,不自动清理 —— §7 #4)。"""
    if not src.is_file():
        return None
    stale_dir = root / AUDIO_DIRNAME / STALE_DIRNAME
    stale_dir.mkdir(parents=True, exist_ok=True)
    dst = stale_dir / src.name
    if dst.exists():
        dst = stale_dir / f"{src.stem}.{int(time.time())}{src.suffix}"
    os.replace(src, dst)
    return dst


def reconcile(
    manifest: Manifest,
    chapters: Mapping[str, Chapter] | Iterable[Chapter],
    *,
    provider: str,
    provider_contract_version: int,
    bindings: Mapping[str, str] | None = None,
    min_fragment_bytes: int = MIN_FRAGMENT_BYTES,
    persist: bool = True,
) -> ReconcileReport:
    """按 design §5.4 用磁盘实际状态与 manifest 交叉对账。

    Args:
        manifest: 台账(会被就地更新;是否落盘由 `persist` 决定)。
        chapters: 当前章节(名 → `Chapter`,含最新 `json_sha256`/`sentence_count`)。
        provider / provider_contract_version: **当前激活**的后端与契约版本(第 0 步来源检查)。
        bindings: 角色 → 音色(用于 §5.5 绑定变更检测;仅标 stale,不自动重做)。
        min_fragment_bytes: 「无记录但文件存在」时的最小可信字节数。

    Returns:
        `ReconcileReport`。
    """
    root = manifest.root
    if not isinstance(chapters, Mapping):
        chapters = {c.name: c for c in chapters}

    report = ReconcileReport(provider=provider, provider_contract_version=provider_contract_version)

    for name in sorted(chapters):
        chapter = chapters[name]
        rec = manifest.get_or_create(
            name,
            json_sha256=chapter.json_sha256,
            sentence_count=chapter.sentence_count,
            provider=provider,
            provider_contract_version=provider_contract_version,
        )
        audio_abs = manifest.chapter_audio_path(name)

        # --- 第 0 步:provider 来源检查 ---------------------------------- #
        mismatch = rec.provider is not None and (
            rec.provider != provider or rec.provider_contract_version != provider_contract_version
        )
        if mismatch:
            _move_to_stale(root, audio_abs)
            _clear_dir(manifest.fragment_dir(name))
            rec.fragments.clear()
            rec.status = ChapterStatus.PENDING
            rec.output = None
            rec.json_sha256 = chapter.json_sha256
            rec.sentence_count = chapter.sentence_count
            rec.provider = provider
            rec.provider_contract_version = provider_contract_version
            report.actions.append(
                ReconcileAction(
                    name,
                    "redo",
                    f"来源不一致(旧 provider={rec.provider}),整章重做",
                )
            )
            continue

        hash_matches = rec.json_sha256 == chapter.json_sha256
        audio_exists = audio_abs.is_file()

        # --- 第 1 步:hash 匹配且有成品 → 整章完成 ----------------------- #
        if hash_matches and audio_exists:
            _clear_dir(manifest.fragment_dir(name))
            rec.status = ChapterStatus.DONE
            rec.output = manifest.chapter_audio_relpath(name)
            rec.provider = provider
            rec.provider_contract_version = provider_contract_version
            report.actions.append(ReconcileAction(name, "skip", "整章完成,跳过"))
            continue

        # --- 第 2 步:有成品但 hash 不匹配 → 旧稿成品移到 .stale ---------- #
        if audio_exists and not hash_matches:
            _move_to_stale(root, audio_abs)
            rec.output = None

        # --- 第 3 步:hash 不匹配(无成品)→ 作废该章全部片段 ------------ #
        if not hash_matches:
            _clear_dir(manifest.fragment_dir(name))
            rec.fragments.clear()
            rec.status = ChapterStatus.PENDING
            rec.output = None
            rec.json_sha256 = chapter.json_sha256
            rec.sentence_count = chapter.sentence_count
            rec.provider = provider
            rec.provider_contract_version = provider_contract_version
            report.actions.append(ReconcileAction(name, "redo", "章节 JSON 变更,片段作废"))
            continue

        # --- 第 4 步:hash 匹配、无成品 → 逐条对账 ----------------------- #
        rec.json_sha256 = chapter.json_sha256
        rec.sentence_count = chapter.sentence_count
        rec.provider = provider
        rec.provider_contract_version = provider_contract_version
        rec.output = None

        action = _reconcile_fragments(
            manifest, rec, chapter, min_fragment_bytes=min_fragment_bytes
        )

        # §5.5 绑定变更:受影响片段标 stale(不自动重做)
        if bindings:
            for frag in rec.fragments.values():
                if frag.status is FragmentStatus.DONE and frag.voice and frag.speaker:
                    expected = bindings.get(frag.speaker)
                    if expected is not None and frag.voice != expected:
                        frag.status = FragmentStatus.STALE
                        action.stale.append(frag.index)
            if action.stale:
                action.stale.sort()
                action.action = "stale"
                action.reason = f"{len(action.stale)} 条片段因绑定变更失效(需用户确认重做)"

        if action.action == "stale":
            rec.status = ChapterStatus.PARTIAL
        elif action.todo:
            rec.status = ChapterStatus.PENDING
        else:
            rec.status = ChapterStatus.PARTIAL  # 片段齐但未拼接
        report.actions.append(action)

    # manifest 里有、当前却没有 JSON 的章节 → 告警(不动它)
    for orphan in sorted(set(manifest.chapters) - set(chapters)):
        report.warnings.append(f"manifest 含已不存在的章节: {orphan}")

    if persist:
        manifest.save()
    return report


def _reconcile_fragments(
    manifest: Manifest,
    rec: ChapterRecord,
    chapter: Chapter,
    *,
    min_fragment_bytes: int,
) -> ReconcileAction:
    """§5.4 第 4 步:逐条对账。"""
    action = ReconcileAction(rec.name, "resume", "")
    fragment_dir = manifest.fragment_dir(rec.name)

    on_disk: dict[int, Path] = {}
    if fragment_dir.is_dir():
        for child in fragment_dir.glob("*.wav"):
            try:
                idx = int(child.stem)
            except ValueError:
                continue
            on_disk[idx] = child

    for index in range(1, rec.sentence_count + 1):
        key = fragment_key(index)
        path = on_disk.get(index)
        size = path.stat().st_size if path is not None else None
        frag = rec.fragments.get(key)

        if frag is not None and frag.status is FragmentStatus.DONE:
            if path is not None and frag.bytes == size:
                continue  # 文件在且字节数一致 → 跳过
            # 文件缺失 / 字节数不符 → 视为未完成,重生成
            frag.status = FragmentStatus.PENDING
            action.todo.append(index)
            continue

        if frag is not None and frag.status in (
            FragmentStatus.FAILED,
            FragmentStatus.STALE,
            FragmentStatus.PENDING,
        ):
            action.todo.append(index)
            continue

        # 无记录但文件存在 → 校验可解码且 > 最小字节数 → 补记 done
        if path is not None and _is_valid_fragment_file(path, min_fragment_bytes):
            rec.fragments[key] = FragmentEntry(
                index=index,
                status=FragmentStatus.DONE,
                bytes=size,
                provider=rec.provider,
            )
            action.adopted.append(index)
        else:
            action.todo.append(index)

    action.todo.sort()
    # 磁盘上超出句数的残片 → 告警式提示(不改动)
    extra = sorted(idx for idx in on_disk if idx > rec.sentence_count)
    if extra:
        action.reason = f"process_audio 含超出句数的片段: {extra}"
    if action.todo:
        prefix = f"需生成 {len(action.todo)} 条"
        action.reason = f"{prefix};{action.reason}" if action.reason else prefix
    elif not action.reason:
        action.reason = "片段齐备,待拼接"
    return action


def detect_binding_stale(manifest: Manifest, bindings: Mapping[str, str]) -> dict[str, list[int]]:
    """**只读**计算:哪些已 `done` 的片段所用音色与当前绑定不符(§5.5)。

    返回 `{章节: [片段索引...]}`;不修改任何状态、不落盘、不改写 `frag.status`。
    供工程详情等只读展示使用(可在同一请求内安全地与 `done` 计数并存)。
    """
    changed: dict[str, list[int]] = {}
    for name, rec in manifest.chapters.items():
        stale = [
            frag.index
            for frag in rec.fragments.values()
            if frag.status is FragmentStatus.DONE
            and frag.voice
            and frag.speaker
            and bindings.get(frag.speaker) is not None
            and frag.voice != bindings.get(frag.speaker)
        ]
        if stale:
            changed[name] = sorted(stale)
    return changed


def mark_binding_stale(
    manifest: Manifest, bindings: Mapping[str, str], *, mutate: bool = True
) -> dict[str, list[int]]:
    """把与当前绑定不符的 done 片段标为 stale(§5.5),返回 章 → 索引 列表。

    `mutate=True`(默认,保持既有语义):就地改写 `frag.status = STALE`
    与 `rec.status = PARTIAL`,供「重做受影响章节」等显式动作使用。
    `mutate=False`:只计算、不改写状态,等价于 `detect_binding_stale`。

    只读展示(工程详情 GET 等)请改用 **`detect_binding_stale`**,
    避免在同一次请求内因改写 `done` 片段而影响后续计数。
    无论如何都**不自动重做**。
    """
    changed = detect_binding_stale(manifest, bindings)
    if mutate:
        for name, indices in changed.items():
            rec = manifest.chapters[name]
            for index in indices:
                rec.fragments[fragment_key(index)].status = FragmentStatus.STALE
            rec.status = ChapterStatus.PARTIAL
    return changed

