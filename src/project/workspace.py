"""工作区扫描与工程发现(design §6)。

工作区根来自 `Settings.workspace_root`。**目录规范**(design §2):

    <workspace>/<书名>/          一本书 = 一个工程 = 一个隔离空间
        json/                    章节 JSON(输入)
        process_audio/           中间产物(每章一个子目录)
        audio/                   章节成品
        binding.json             角色 → 音色 绑定
        .manifest.json           续传台账

「含 `json/` 的目录」即被识别为工程;非工程目录不列出,
由 UI 提供显式的「初始化工程」操作(`ensure_project_layout`)。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import InvalidInputError, NotFoundError

JSON_DIRNAME = "json"
PROCESS_AUDIO_DIRNAME = "process_audio"
AUDIO_DIRNAME = "audio"
STALE_DIRNAME = ".stale"
BINDING_FILENAME = "binding.json"
MANIFEST_FILENAME = ".manifest.json"

PROJECT_SUBDIRS = (JSON_DIRNAME, PROCESS_AUDIO_DIRNAME, AUDIO_DIRNAME)

MAX_NAME_BYTES = 255
_FORBIDDEN_NAME_CHARS = ("/", "\\", "\x00")


def validate_project_name(name: str) -> str:
    """校验工程名(书名)可安全用作目录名。规则同音色名(design §7 精神)。

    Raises:
        InvalidInputError: 名字不合法。
    """
    if not isinstance(name, str):
        raise InvalidInputError("工程名必须是字符串", detail={"name": repr(name)})
    if not name or not name.strip():
        raise InvalidInputError("工程名不能为空", detail={"name": name})
    if name != name.strip():
        raise InvalidInputError("工程名首尾不能有空白字符", detail={"name": name})
    if name in (".", ".."):
        raise InvalidInputError("工程名不能是 '.' 或 '..'", detail={"name": name})
    for ch in _FORBIDDEN_NAME_CHARS:
        if ch in name:
            raise InvalidInputError(
                f"工程名不能包含 {ch!r}(路径分隔/穿越风险)", detail={"name": name}
            )
    for ch in name:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise InvalidInputError("工程名不能包含控制字符", detail={"name": repr(name)})
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise InvalidInputError(f"工程名过长(超过 {MAX_NAME_BYTES} 字节)", detail={"name": name})
    return name


def is_project(root: Path | str) -> bool:
    """目录是否为一个工程(含 `json/`)。"""
    return (Path(root) / JSON_DIRNAME).is_dir()


def ensure_project_layout(root: Path | str) -> Path:
    """创建工程的三个子目录(`json/ process_audio/ audio/`)。"""
    root = Path(root)
    for sub in PROJECT_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


class Workspace:
    """工作区:扫描 / 发现 / 创建工程。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list_projects(self) -> list[str]:
        """列出工作区下所有工程(含 `json/` 的目录),按名字排序。"""
        if not self.root.is_dir():
            return []
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and not child.name.startswith(".") and is_project(child)
        )

    def path_of(self, name: str) -> Path:
        validate_project_name(name)
        return self.root / name

    def is_project(self, name: str) -> bool:
        validate_project_name(name)
        return is_project(self.root / name)

    def create(self, name: str) -> Path:
        """新建工程目录骨架。

        Raises:
            InvalidInputError: 名字不合法,或工程已存在。
        """
        validate_project_name(name)
        path = self.root / name
        if path.exists():
            raise InvalidInputError(f"工程已存在: {name}", detail={"name": name})
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_project_layout(path)
        return path

    def open(self, name: str) -> Path:
        """取工程根路径(须已存在且为合法工程)。

        Raises:
            NotFoundError: 工程不存在或不是工程目录。
        """
        validate_project_name(name)
        path = self.root / name
        if not is_project(path):
            raise NotFoundError(f"工程不存在或不是工程: {name}")
        return path
