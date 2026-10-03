"""工程(`/api/projects`)：列表 / 新建 / 详情 / 章节成品试听。"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Response

from ...errors import InvalidInputError, NotFoundError
from ...models import ChapterStatus, FragmentStatus
from ...project import (
    JSON_DIRNAME,
    MANIFEST_FILENAME,
    Project,
    is_project,
)
from ...project.manifest import detect_binding_stale
from ...voice import VoiceLibrary
from ..deps import AllowedRootsDep, VoicesDep, WorkspaceDep
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
