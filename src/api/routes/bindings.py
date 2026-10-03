"""角色 → 音色 绑定读写(`GET/PUT /api/projects/{name}/bindings`,§8.2)。"""

from __future__ import annotations

from fastapi import APIRouter

from ...errors import BindingError
from ...project import Project, Workspace
from ..deps import WorkspaceDep
from ..schemas import BindingsFileOut, BindingsIn

router = APIRouter(prefix="/api/projects", tags=["bindings"])


def _open(name: str, workspace: Workspace) -> Project:
    root = workspace.open(name)
    return Project.open(root, book=name)


@router.get("/{name}/bindings", response_model=BindingsFileOut)
def get_bindings(name: str, workspace: WorkspaceDep) -> BindingsFileOut:
    project = _open(name, workspace)
    assert project.bindings is not None
    return BindingsFileOut(**project.bindings.to_dict())


@router.put("/{name}/bindings", response_model=BindingsFileOut)
def put_bindings(name: str, body: BindingsIn, workspace: WorkspaceDep) -> BindingsFileOut:
    """整表替换并落盘。绑定指向不存在的音色**允许**(UI 显示为失效,design §3)。"""
    project = _open(name, workspace)
    table = project.bindings
    assert table is not None

    if body.version != 1:
        raise BindingError(f"不支持的 binding 版本: {body.version!r}")

    for role in list(table.all()):
        table.remove(role, persist=False)
    for role, binding in body.bindings.items():
        table.set(role, binding.voice, binding.emo_ref, persist=False)
    table.save()
    return BindingsFileOut(**table.to_dict())
