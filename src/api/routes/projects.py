"""工程(`/api/projects`)：列表 / 新建 / 详情 / 章节成品试听 / **章节 JSON 上传**(§8.9)。

上传是新增攻击面(防**写**越界,与 `/api/fs/browse` 防读越界相对):
目标路径由**服务端拼接**,绝不信任客户端文件名;只落在 `<工程>/json/` 内。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from starlette.datastructures import FormData, UploadFile

from ...errors import InvalidInputError, NotFoundError
from ...models import ChapterStatus, FragmentStatus
from ...project import (
    AUDIO_DIRNAME,
    JSON_DIRNAME,
    MANIFEST_FILENAME,
    STALE_DIRNAME,
    Project,
    atomic_write_bytes,
    is_project,
    validate_project_name,
)
from ...project.manifest import detect_binding_stale
from ...voice import VoiceLibrary
from ..deps import AllowedRootsDep, QueueDep, VoicesDep, WorkspaceDep
from ..errors import AlreadyExistsError, ConflictError
from ..schemas import (
    BindingsFileOut,
    ChapterSummaryOut,
    CreateProjectRequest,
    ManifestSummaryOut,
    ProjectDetailOut,
    ProjectSummaryOut,
    RoleViewOut,
    StaleChapterOut,
)
from .fs import resolve_within_roots

router = APIRouter(prefix="/api/projects", tags=["projects"])

# ---- 上传约束(§8.9 表格)------------------------------------------------- #
MAX_UPLOAD_FILE_BYTES = 8 * 1024 * 1024  # 单文件 8 MB
MAX_UPLOAD_FILES = 200  # 单次文件数
MAX_UPLOAD_TOTAL_BYTES = 64 * 1024 * 1024  # 总大小 64 MB
MAX_FILENAME_BYTES = 255

_READ_CHUNK = 1024 * 1024


# --------------------------------------------------------------------------- #
# 上传:校验(防写越界)+ 落盘
# --------------------------------------------------------------------------- #


def _form_str(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) else None


def _safe_json_filename(raw: str) -> str:
    """文件名净化:只接受单段 `.json` 基名(拒绝分隔符/穿越/控制字符/超长)。

    拒绝而非「静默取 basename」—— 客户端给了路径成分本身即视为恶意/错误。
    """
    if not raw:
        raise InvalidInputError("文件名为空")
    if "/" in raw or "\\" in raw:
        raise InvalidInputError(f"文件名不能包含路径分隔符: {raw!r}")
    if ".." in raw:
        raise InvalidInputError(f"文件名不能包含 '..': {raw!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise InvalidInputError("文件名不能包含控制字符")
    if len(raw.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise InvalidInputError(f"文件名过长(超过 {MAX_FILENAME_BYTES} 字节)")
    if not raw.lower().endswith(".json"):
        raise InvalidInputError(f"只接受 .json 文件: {raw!r}")
    if not raw[:-5]:
        raise InvalidInputError(f"文件名不合法: {raw!r}")
    return raw


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """分块读取并强制单文件上限(避免把超大文件整体读进内存)。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise InvalidInputError(
                f"文件超过单文件上限 {limit} 字节: {upload.filename!r}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_chapter_json(data: bytes, filename: str) -> None:
    """章节 JSON 顶层必须是数组(与 `project.chapter.parse` 一致),否则 400。"""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInputError(f"文件不是合法 JSON: {filename}: {exc}") from exc
    if not isinstance(raw, list):
        raise InvalidInputError(f"章节 JSON 顶层必须是数组: {filename}")


async def _collect_json_uploads(form: FormData) -> list[tuple[str, bytes]]:
    """校验全部上传文件(数量 / 名 / 大小),**全部通过后才落盘**。"""
    uploads = [value for value in form.getlist("files") if isinstance(value, UploadFile)]
    if not uploads:
        raise InvalidInputError("缺少上传文件(字段名 files)")
    if len(uploads) > MAX_UPLOAD_FILES:
        raise InvalidInputError(f"单次上传文件数超过上限 {MAX_UPLOAD_FILES}")

    items: list[tuple[str, bytes]] = []
    total = 0
    for upload in uploads:
        filename = _safe_json_filename(upload.filename or "")
        data = await _read_limited(upload, MAX_UPLOAD_FILE_BYTES)
        if not data:
            raise InvalidInputError(f"文件为空: {filename}")
        total += len(data)
        if total > MAX_UPLOAD_TOTAL_BYTES:
            raise InvalidInputError(f"上传总大小超过上限 {MAX_UPLOAD_TOTAL_BYTES} 字节")
        _validate_chapter_json(data, filename)
        items.append((filename, data))
    return items


def _write_json_files(root, items: list[tuple[str, bytes]]) -> None:
    """只写入 `<工程>/json/`;同名覆盖(由 reconcile 按 hash 自然作废旧片段)。"""
    json_dir = root / JSON_DIRNAME
    json_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = json_dir.resolve()
    for filename, data in items:
        target = json_dir / filename
        if target.resolve().parent != resolved_dir:  # 双保险(文件名已净化)
            raise InvalidInputError(f"非法落盘路径: {filename!r}")
        atomic_write_bytes(target, data)


def _role_status(project: Project, voice_names: set[str]) -> list[RoleViewOut]:
    assert project.bindings is not None
    rows: list[RoleViewOut] = []
    for role in project.roles:
        binding = project.bindings.get(role)
        if binding is None:
            rows.append(RoleViewOut(name=role, voice=None, emo_ref=None, status="unbound"))
        elif binding.voice not in voice_names:
            rows.append(
                RoleViewOut(
                    name=role,
                    voice=binding.voice,
                    emo_ref=binding.emo_ref,
                    status="invalid",
                )
            )
        else:
            rows.append(
                RoleViewOut(name=role, voice=binding.voice, emo_ref=binding.emo_ref, status="ok")
            )
    return rows


def _project_detail(project: Project, voices: VoiceLibrary) -> ProjectDetailOut:
    """工程详情;`roles[].status` 与 `stale_chapters` 均**由后端计算**(§8.1)。"""
    assert project.manifest is not None
    assert project.bindings is not None

    voice_names = {entry.name for entry in voices.list()}

    # stale_chapters:绑定被改动后,已生成片段所用的音色与当前绑定不符的章节。
    # 用**纯函数** `detect_binding_stale`:GET 必须无副作用 —— 就地改写会把本请求里
    # 后续统计的 `done_fragments` 静默归零(读接口写出数据错误)。语义仅「提示」,
    # 不自动重做;真正改写状态留给显式操作(UI「重做受影响章节」)。
    current_bindings = {role: b.voice for role, b in project.bindings.all().items()}
    changed = detect_binding_stale(project.manifest, current_bindings)

    chapters: list[ChapterSummaryOut] = []
    summary = ManifestSummaryOut()
    for chapter in project.chapters:
        record = project.manifest.chapter(chapter.name)
        status = record.status.value if record is not None else ChapterStatus.PENDING.value
        done = failed = 0
        if record is not None:
            done = sum(1 for f in record.fragments.values() if f.status is FragmentStatus.DONE)
            failed = sum(1 for f in record.fragments.values() if f.status is FragmentStatus.FAILED)
        chapters.append(
            ChapterSummaryOut(
                name=chapter.name,
                sentence_count=chapter.sentence_count,
                status=status,
                output=record.output if record is not None else None,
                provider=record.provider if record is not None else None,
                done_fragments=done,
                failed_fragments=failed,
            )
        )
        if status == ChapterStatus.DONE.value:
            summary.done += 1
        elif status == ChapterStatus.PARTIAL.value:
            summary.partial += 1
        else:
            summary.pending += 1

    warnings: list[str] = []
    for chapter_name, items in sorted(project.chapter_warnings.items()):
        warnings.extend(f"{chapter_name}: {item}" for item in items)

    return ProjectDetailOut(
        name=project.name,
        path=str(project.root),
        chapters=chapters,
        roles=_role_status(project, voice_names),
        bindings=BindingsFileOut(**project.bindings.to_dict()),
        manifest_summary=summary,
        stale_chapters=[
            StaleChapterOut(name=name, stale_fragments=list(indices))
            for name, indices in sorted(changed.items())
        ],
        warnings=warnings,
    )


@router.get("", response_model=list[ProjectSummaryOut])
def list_projects(workspace: WorkspaceDep) -> list[ProjectSummaryOut]:
    """列出工作区内工程(含 `json/` 的目录)。"""
    out: list[ProjectSummaryOut] = []
    for name in workspace.list_projects():
        root = workspace.path_of(name)
        project = Project.open(root, book=name)
        chapter_count = len(project.chapters)
        done_count = sum(
            1
            for chapter in project.chapters
            if (record := project.manifest.chapter(chapter.name)) is not None
            and record.status is ChapterStatus.DONE
        )
        out.append(
            ProjectSummaryOut(
                name=name,
                path=str(root),
                chapter_count=chapter_count,
                done_count=done_count,
                sentence_count=sum(c.sentence_count for c in project.chapters),
                progress=(done_count / chapter_count) if chapter_count else 0.0,
                has_manifest=(root / MANIFEST_FILENAME).is_file(),
            )
        )
    return out


@router.post("", response_model=ProjectDetailOut)
def create_project(
    body: CreateProjectRequest,
    workspace: WorkspaceDep,
    roots: AllowedRootsDep,
    voices: VoicesDep,
) -> ProjectDetailOut:
    """新建工程:建目录骨架 + 从 `source_path` 导入章节 JSON(§8.3)。"""
    source, _root = resolve_within_roots(body.source_path, roots)
    if not source.is_dir():
        raise InvalidInputError(f"源路径不是目录: {source}")

    json_source = source / JSON_DIRNAME if (source / JSON_DIRNAME).is_dir() else source
    json_files = sorted(json_source.glob("*.json"))
    if not json_files:
        raise InvalidInputError(f"源目录中没有章节 JSON: {source}")

    root = workspace.path_of(body.name)
    if root.exists() and is_project(root):
        raise InvalidInputError(f"工程已存在: {body.name}")

    project = Project.create(root, name=body.name)
    for json_file in json_files:
        shutil.copy2(json_file, root / JSON_DIRNAME / json_file.name)
    project = Project.open(root, book=body.name)
    return _project_detail(project, voices)


@router.post("/upload", response_model=ProjectDetailOut)
async def upload_project(
    request: Request,
    workspace: WorkspaceDep,
    voices: VoicesDep,
) -> ProjectDetailOut:
    """浏览器上传建工程(`multipart/form-data`:`name` + 一个或多个 `files`)。§8.9"""
    form = await request.form()
    name = _form_str(form, "name")
    if not name:
        raise InvalidInputError("缺少工程名 name")
    validate_project_name(name)

    root = workspace.path_of(name)
    if root.exists():
        raise AlreadyExistsError(f"工程已存在: {name}")

    items = await _collect_json_uploads(form)  # 先全部校验
    Project.create(root, name=name)  # 再建骨架
    _write_json_files(root, items)
    return _project_detail(Project.open(root, book=name), voices)


@router.post("/{name}/chapters", response_model=ProjectDetailOut)
async def append_chapters(
    name: str,
    request: Request,
    workspace: WorkspaceDep,
    voices: VoicesDep,
) -> ProjectDetailOut:
    """给已有工程追加/替换章节(`files`;同名覆盖)。§8.9

    同名覆盖无需新机制:该章 `json_sha256` 变化后 `reconcile` 自会作废旧片段重做。
    """
    root = workspace.open(name)  # 工程不存在 → 404
    form = await request.form()
    items = await _collect_json_uploads(form)
    _write_json_files(root, items)
    return _project_detail(Project.open(root, book=name), voices)


# --------------------------------------------------------------------------- #
# 删除(可恢复,§8.10)
# --------------------------------------------------------------------------- #


def _safe_chapter_name(raw: str) -> str:
    """章节名净化:拒绝分隔符 / 穿越 / 控制字符(§8.10 安全)。"""
    if not raw or raw != raw.strip():
        raise InvalidInputError(f"章节名不合法: {raw!r}")
    if "/" in raw or "\\" in raw:
        raise InvalidInputError(f"章节名不能包含路径分隔符: {raw!r}")
    if ".." in raw or raw == ".":
        raise InvalidInputError(f"章节名不能包含 '..': {raw!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise InvalidInputError("章节名不能包含控制字符")
    if len(raw.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise InvalidInputError("章节名过长")
    return raw


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _unique_path(dest):
    if not dest.exists():
        return dest
    index = 1
    while True:
        candidate = dest.with_name(f"{dest.name}-{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _move_audio_to_stale(root, audio_path) -> None:
    """成品移到 `audio/.stale/`(保留,不真删 —— 与既有决策一致)。"""
    if not audio_path.is_file():
        return
    stale_dir = root / AUDIO_DIRNAME / STALE_DIRNAME
    stale_dir.mkdir(parents=True, exist_ok=True)
    os.replace(audio_path, _unique_path(stale_dir / audio_path.name))


@router.delete("/{name}")
def delete_project(name: str, workspace: WorkspaceDep, queue: QueueDep) -> dict[str, str]:
    """删除工程:整个目录**移到 `<workspace>/.trash/<名>-<时间戳>/`**(可恢复)。

    同卷内 `os.rename` 才是原子的(见 design §8.10 的卷内约束)。
    """
    root = workspace.open(name)  # 404 if 不存在/非法;内部 validate_project_name
    snapshot = queue.snapshot()
    if snapshot.get("project") == name and snapshot.get("state") in ("running", "paused"):
        raise ConflictError(f"工程正在合成,不能删除: {name}")

    trash_root = workspace.root / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(trash_root / f"{name}-{_timestamp()}")
    try:
        os.rename(root, dest)
    except OSError as exc:
        raise InvalidInputError(f"移动工程到回收站失败: {name}: {exc}") from exc
    return {"deleted": name, "trashed_to": f".trash/{dest.name}"}


@router.delete("/{name}/chapters/{chapter}", response_model=ProjectDetailOut)
def delete_chapter(
    name: str,
    chapter: str,
    workspace: WorkspaceDep,
    queue: QueueDep,
    voices: VoicesDep,
) -> ProjectDetailOut:
    """删除章节:成品移到 `audio/.stale/`;其余(JSON / manifest 记录 / 片段目录)删除。"""
    chapter = _safe_chapter_name(chapter)
    root = workspace.open(name)
    project = Project.open(root, book=name)
    if project.chapter(chapter) is None:
        raise NotFoundError(f"章节不存在: {chapter}")

    snapshot = queue.snapshot()
    if (
        snapshot.get("project") == name
        and snapshot.get("state") == "running"
        and snapshot.get("current_chapter") == chapter
    ):
        raise ConflictError(f"章节正在合成,不能删除: {chapter}")

    assert project.manifest is not None
    manifest = project.manifest
    _move_audio_to_stale(root, manifest.chapter_audio_path(chapter))
    (root / JSON_DIRNAME / f"{chapter}.json").unlink(missing_ok=True)
    shutil.rmtree(manifest.fragment_dir(chapter), ignore_errors=True)
    manifest.chapters.pop(chapter, None)
    manifest.save()  # 原子落盘
    return _project_detail(Project.open(root, book=name), voices)


@router.get("/{name}", response_model=ProjectDetailOut)
def get_project(name: str, workspace: WorkspaceDep, voices: VoicesDep) -> ProjectDetailOut:
    """工程详情:章节清单、角色列表(含 `status`)、绑定、manifest 摘要、`stale_chapters`。"""
    root = workspace.open(name)
    project = Project.open(root, book=name)
    return _project_detail(project, voices)


@router.get("/{name}/chapters/{chapter}/audio")
def chapter_audio(name: str, chapter: str, workspace: WorkspaceDep) -> Response:
    """章节成品试听 —— **资源型 GET**,返回 `audio/wav` 字节流(design §5)。"""
    root = workspace.open(name)
    project = Project.open(root, book=name)
    if project.chapter(chapter) is None:
        raise NotFoundError(f"章节不存在: {chapter}")
    assert project.manifest is not None
    audio_path = project.manifest.chapter_audio_path(chapter)
    if not audio_path.is_file():
        raise NotFoundError(f"章节成品未生成: {chapter}")
    return Response(content=audio_path.read_bytes(), media_type="audio/wav")
