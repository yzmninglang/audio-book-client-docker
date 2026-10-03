"""工程模块(M3)。

对应设计:`doc/project/design.md`。

    workspace.py  工作区扫描 + 工程发现(§6)
    chapter.py    章节 JSON 解析 + 角色聚合(§3)
    binding.py    角色 → 音色 绑定持久化(§4)
    manifest.py   ★ 续传台账(§5)
    project.py    工程模型:聚合上述四者(§2)
"""

from __future__ import annotations

from .binding import BindingTable
from .chapter import (
    ChapterParseResult,
    aggregate_roles,
    compute_sha256,
    parse_chapter,
    parse_chapter_bytes,
    parse_raw_sentences,
)
from .manifest import (
    MIN_FRAGMENT_BYTES,
    FragmentEntry,
    Manifest,
    ReconcileAction,
    ReconcileReport,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    detect_binding_stale,
    fragment_key,
    mark_binding_stale,
    reconcile,
)
from .project import Project, find_projects_using_voice
from .workspace import (
    AUDIO_DIRNAME,
    BINDING_FILENAME,
    JSON_DIRNAME,
    MANIFEST_FILENAME,
    PROCESS_AUDIO_DIRNAME,
    STALE_DIRNAME,
    Workspace,
    ensure_project_layout,
    is_project,
    validate_project_name,
)

__all__ = [
    "AUDIO_DIRNAME",
    "BINDING_FILENAME",
    "JSON_DIRNAME",
    "MANIFEST_FILENAME",
    "MIN_FRAGMENT_BYTES",
    "PROCESS_AUDIO_DIRNAME",
    "STALE_DIRNAME",
    "BindingTable",
    "ChapterParseResult",
    "FragmentEntry",
    "Manifest",
    "Project",
    "ReconcileAction",
    "ReconcileReport",
    "Workspace",
    "aggregate_roles",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "compute_sha256",
    "detect_binding_stale",
    "ensure_project_layout",
    "find_projects_using_voice",
    "fragment_key",
    "is_project",
    "mark_binding_stale",
    "parse_chapter",
    "parse_chapter_bytes",
    "parse_raw_sentences",
    "reconcile",
    "validate_project_name",
]
