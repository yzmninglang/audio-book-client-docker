"""服务端卷内路径浏览(`GET /api/fs/browse`)—— **全项目唯一的攻击面**(design §3)。

安全约束(必须全部满足):

    ① 路径**规范化后**校验前缀,限制在允许的根(工作区 / 音色库卷)内
    ② 拒绝**符号链接逃逸**(`Path.resolve()` 会跟随软链,前缀校验因此可信)
    ③ 目录项数量上限(防超大目录拖垮响应)
    ④ **只读** —— 本路由不提供任何写操作

**虚拟根 `/`**:请求 `/` 返回允许的**卷根列表**(供前端面包屑「回到根」),而非磁盘根目录;
磁盘上的 `/`(以及任何卷外路径)仍被拒绝,故不泄漏容器文件系统结构。

`resolve_within_roots` 同时被 `routes/projects.py` 复用,校验建工程时的 `source_path`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query

from ...errors import InvalidInputError, NotFoundError
from ...project import is_project
from ..deps import AllowedRootsDep, SettingsDep
from ..schemas import FsBrowseOut, FsEntryOut

router = APIRouter(prefix="/api/fs", tags=["fs"])

MAX_DIR_ENTRIES = 1000
"""单次返回的子目录上限(防超大目录拖垮响应)。"""


def resolve_within_roots(raw: str, roots: list[Path]) -> tuple[Path, Path]:
    """把用户路径规范化并校验其落在某个允许的根之内。

    Returns:
        `(规范化后的绝对路径, 命中的根)`。

    Raises:
        InvalidInputError: 相对路径 / 超出允许范围 / 不可解析。
        NotFoundError: 路径不存在。
    """
    if not raw or not raw.strip():
        raise InvalidInputError("缺少 path 参数")

    candidate = Path(raw)
    if not candidate.is_absolute():
        # 相对路径(含 `../../etc` 这类)一律拒绝 —— 浏览只接受卷内绝对路径。
        raise InvalidInputError(f"路径必须是绝对路径: {raw}")

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError(f"路径不存在: {raw}") from exc
    except (OSError, RuntimeError) as exc:
        raise InvalidInputError(f"路径无法解析: {raw}: {exc}") from exc

    for root in roots:
        try:
            resolved_root = Path(root).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == resolved_root or resolved_root in resolved.parents:
            return resolved, resolved_root

    raise InvalidInputError(f"路径超出允许范围(仅限工作区/音色库卷内): {raw}")


def is_root_path(raw: str) -> bool:
    """请求路径是否为虚拟根(字面 `/`)。"""
    return str(Path(raw)) == "/"


def _virtual_root(settings) -> FsBrowseOut:
    """虚拟根 `/`:只列允许的卷根,不暴露磁盘 `/` 的内容。"""
    entries: list[FsEntryOut] = []
    for root in (settings.workspace_root, settings.voices_root):
        path = Path(root)
        name = path.name or str(path)
        has_children = path.is_dir() and _has_child_dir(path)
        entries.append(FsEntryOut(name=name, path=str(path), has_children=has_children))
    return FsBrowseOut(path="/", parent=None, dirs=entries, is_project=False, virtual=True)


def _has_child_dir(directory: Path) -> bool:
    try:
        for child in directory.iterdir():
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


@router.get("/browse", response_model=FsBrowseOut)
def browse(
    roots: AllowedRootsDep,
    settings: SettingsDep,
    path: Annotated[str, Query(description="容器内绝对路径;省略时默认工作区根")] = "",
) -> FsBrowseOut:
    """列出卷内某目录的子目录(只读)。`parent` 为上级;卷根处为 `null`;`/` 为虚拟根。"""
    raw = path or settings.workspace_root
    if is_root_path(raw):
        return _virtual_root(settings)
    resolved, matched_root = resolve_within_roots(raw, roots)

    if not resolved.is_dir():
        raise InvalidInputError(f"不是目录: {resolved}")

    entries: list[FsEntryOut] = []
    for child in sorted(resolved.iterdir(), key=lambda c: c.name):
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if len(entries) >= MAX_DIR_ENTRIES:
            break
        entries.append(FsEntryOut(name=child.name, has_children=_has_child_dir(child)))

    parent = None if resolved == matched_root else str(resolved.parent)
    return FsBrowseOut(
        path=str(resolved),
        parent=parent,
        dirs=entries,
        is_project=is_project(resolved),
    )
