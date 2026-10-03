"""工程模型:聚合章节清单 / 角色 / 绑定 / manifest(design §2/§6)。

`Project` 只做「读盘 → 聚合」与「写回持久层」,不合成、不调后端(design §1)。
上层 `synthesis/` 消费它:章节清单、角色→音色绑定、续传台账。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BindingError, NotFoundError
from ..models import Chapter
from .binding import BindingTable
from .chapter import ChapterParseResult, aggregate_roles, parse_chapter
from .manifest import Manifest, ReconcileReport, reconcile
from .workspace import JSON_DIRNAME, is_project, validate_project_name


@dataclass
class Project:
    """一个工程(一本书)。"""

    name: str
    root: Path
    chapters: list[Chapter] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    bindings: BindingTable | None = None
    manifest: Manifest | None = None
    chapter_warnings: dict[str, list[str]] = field(default_factory=dict)

    # -- 构造 ------------------------------------------------------------- #

    @classmethod
    def open(cls, root: str | Path, *, book: str | None = None) -> Project:
        """打开一个已有工程:解析章节 → 聚合角色 → 读绑定 → 读 manifest。

        Raises:
            NotFoundError: 目录不是工程(不含 `json/`)。
        """
        root = Path(root)
        if not is_project(root):
            raise NotFoundError(f"不是工程目录(缺 {JSON_DIRNAME}/): {root}")

        name = book or root.name
        chapters: list[Chapter] = []
        warnings: dict[str, list[str]] = {}
        for json_path in sorted((root / JSON_DIRNAME).glob("*.json")):
            result: ChapterParseResult = parse_chapter(json_path, root=root)
            chapters.append(result.chapter)
            if result.warnings:
                warnings[result.chapter.name] = result.warnings

        return cls(
            name=name,
            root=root,
            chapters=chapters,
            roles=aggregate_roles(chapters),
            bindings=BindingTable.load(root),
            manifest=Manifest.load(root, book=name),
            chapter_warnings=warnings,
        )

    @classmethod
    def create(cls, root: str | Path, *, name: str | None = None) -> Project:
        """新建工程:创建 `json/ process_audio/ audio/` + 初始化 manifest 与 binding。

        Raises:
            InvalidInputError: 名字不合法或工程已存在。
        """
        from .workspace import ensure_project_layout

        root = Path(root)
        project_name = name or root.name
        validate_project_name(project_name)
        if root.exists() and is_project(root):
            from ..errors import InvalidInputError

            raise InvalidInputError(f"工程已存在: {root}")
        ensure_project_layout(root)
        manifest = Manifest(root, project_name)
        manifest.save()
        BindingTable(root).save()
        return cls.open(root, book=project_name)

    # -- 查询 ------------------------------------------------------------- #

    @property
    def chapter_names(self) -> list[str]:
        return [c.name for c in self.chapters]

    def chapter(self, name: str) -> Chapter | None:
        for chapter in self.chapters:
            if chapter.name == name:
                return chapter
        return None

    def bound_voices(self) -> set[str]:
        """本工程当前用到的音色名集合(含失效的)。"""
        assert self.bindings is not None
        return {b.voice for b in self.bindings.all().values()}

    # -- 绑定校验(开跑前一次性,design §8) ------------------------------ #

    def validate_bindings(self, voice_names: set[str] | frozenset[str]) -> None:
        """校验所有角色都有可用绑定。

        Raises:
            BindingError: 任一角未绑定,或绑定指向不存在的音色。
        """
        assert self.bindings is not None
        problems = self.bindings.missing(set(self.roles), voice_names)
        if problems:
            raise BindingError("存在未绑定或失效的角色: " + ";".join(problems))

    def binding_status(
        self, voice_names: set[str] | frozenset[str]
    ) -> dict[str, str]:
        """每个工程角色的绑定状态:正常为 `""`,否则原因(供 UI 高亮)。"""
        assert self.bindings is not None
        status: dict[str, str] = {}
        for role in self.roles:
            binding = self.bindings.get(role)
            if binding is None:
                status[role] = "未绑定音色"
            elif binding.voice not in voice_names:
                status[role] = f"音色不存在: {binding.voice}"
            else:
                status[role] = ""
        return status

    # -- 续传 ------------------------------------------------------------- #

    def reconcile(
        self,
        *,
        provider: str,
        provider_contract_version: int,
        voice_bindings: Mapping[str, str] | None = None,
        persist: bool = True,
    ) -> ReconcileReport:
        """按 design §5.4 对本工程做启动对账。"""
        assert self.manifest is not None
        bindings = voice_bindings
        if bindings is None and self.bindings is not None:
            bindings = {role: b.voice for role, b in self.bindings.all().items()}
        return reconcile(
            self.manifest,
            {c.name: c for c in self.chapters},
            provider=provider,
            provider_contract_version=provider_contract_version,
            bindings=bindings,
            persist=persist,
        )

    def save(self) -> None:
        """落盘绑定与 manifest。"""
        if self.bindings is not None:
            self.bindings.save()
        if self.manifest is not None:
            self.manifest.save()


def find_projects_using_voice(workspace_root: str | Path, voice_name: str) -> list[str]:
    """反查哪些工程绑定了某音色(供删除音色前的引用提示,design §5)。"""
    from .workspace import Workspace

    workspace = Workspace(workspace_root)
    hits: list[str] = []
    for name in workspace.list_projects():
        table = BindingTable.load(workspace.path_of(name))
        if any(b.voice == voice_name for b in table.all().values()):
            hits.append(name)
    return sorted(hits)
