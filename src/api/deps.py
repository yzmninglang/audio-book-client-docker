"""依赖注入 —— Settings / VoiceLibrary / Workspace / 合成队列。

`Settings` 与队列挂在 `app.state` 上(便于测试注入临时工作区与假 provider);
`VoiceLibrary` / `Workspace` 每次按当前设置构建(轻量,读到最新路径)。

用 `Annotated[...]` 形式声明依赖(FastAPI 推荐,也避免 B008)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from ..config import Settings
from ..project import Workspace
from ..synthesis import SynthesisQueue
from ..voice import VoiceLibrary


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_queue(request: Request) -> SynthesisQueue:
    return request.app.state.queue


def get_voices(request: Request) -> VoiceLibrary:
    settings = get_settings(request)
    return VoiceLibrary(
        settings.voices_root,
        max_seconds=settings.reference_max_seconds,
        max_sample_rate=settings.reference_max_sample_rate,
    )


def get_workspace(request: Request) -> Workspace:
    return Workspace(get_settings(request).workspace_root)


def get_allowed_roots(request: Request) -> list[Path]:
    """`/api/fs/browse` 允许浏览的卷根(工作区 + 音色库)。"""
    settings = get_settings(request)
    return [Path(settings.workspace_root), Path(settings.voices_root)]


SettingsDep = Annotated[Settings, Depends(get_settings)]
QueueDep = Annotated[SynthesisQueue, Depends(get_queue)]
VoicesDep = Annotated[VoiceLibrary, Depends(get_voices)]
WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
AllowedRootsDep = Annotated[list[Path], Depends(get_allowed_roots)]
