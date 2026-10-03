"""角色 → 音色 绑定持久化(design §4)。

`<工程>/binding.json`:

    {
      "version": 1,
      "bindings": {
        "旁白":   {"voice": "男旁白2"},
        "汪淼":   {"voice": "汪淼"},
        "常伟思": {"voice": "常伟思", "emo_ref": "叙述"}
      }
    }

* 多个角色**可绑定同一音色**(多对一)
* **默认不做同名自动绑定**,只做提示(design §3:`旁白` 在音色库中并不存在)
* 音色被删除后,绑定点变为「失效」—— 由 `invalid()` / `missing()` 检出
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from pathlib import Path

from ..errors import BindingError
from ..models import Binding
from .manifest import atomic_write_text
from .workspace import BINDING_FILENAME

BINDING_VERSION = 1


class BindingTable:
    """一个工程的绑定表。绑定值以角色名为键。"""

    def __init__(
        self,
        project_root: str | Path,
        bindings: dict[str, Binding] | None = None,
    ) -> None:
        self.root = Path(project_root)
        self._bindings: dict[str, Binding] = bindings or {}

    @property
    def path(self) -> Path:
        return self.root / BINDING_FILENAME

    # -- 加载 / 保存 ------------------------------------------------------ #

    @classmethod
    def load(cls, project_root: str | Path) -> BindingTable:
        """读取 `binding.json`;文件不存在则返回空表(非错误)。"""
        root = Path(project_root)
        path = root / BINDING_FILENAME
        if not path.is_file():
            return cls(root)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BindingError(f"读取绑定失败: {path}: {exc}") from exc
        return cls(root, cls._bindings_from_dict(raw, path))

    @staticmethod
    def _bindings_from_dict(raw: object, path: Path) -> dict[str, Binding]:
        if not isinstance(raw, Mapping):
            raise BindingError(f"binding.json 顶层必须是对象: {path}")
        if raw.get("version", BINDING_VERSION) != BINDING_VERSION:
            raise BindingError(f"不支持的 binding 版本: {raw.get('version')!r}")
        bindings_raw = raw.get("bindings", {}) or {}
        if not isinstance(bindings_raw, Mapping):
            raise BindingError("binding.bindings 必须是对象")
        out: dict[str, Binding] = {}
        for role, value in bindings_raw.items():
            if not isinstance(value, Mapping):
                raise BindingError(f"角色 {role!r} 的绑定必须是对象")
            voice = value.get("voice")
            if not isinstance(voice, str) or not voice:
                raise BindingError(f"角色 {role!r} 的绑定缺少有效 voice")
            emo_ref = value.get("emo_ref")
            out[str(role)] = Binding(voice=voice, emo_ref=emo_ref)
        return out

    def to_dict(self) -> dict[str, object]:
        bindings: dict[str, object] = {}
        for role, binding in sorted(self._bindings.items()):
            item: dict[str, object] = {"voice": binding.voice}
            if binding.emo_ref is not None:
                item["emo_ref"] = binding.emo_ref
            bindings[role] = item
        return {"version": BINDING_VERSION, "bindings": bindings}

    def save(self) -> None:
        """原子落盘(显式 utf-8)。失败抛 `BindingError`。"""
        try:
            atomic_write_text(
                self.path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            )
        except OSError as exc:
            raise BindingError(f"写入绑定失败: {self.path}: {exc}") from exc

    # -- 访问 ------------------------------------------------------------- #

    def all(self) -> dict[str, Binding]:
        return dict(self._bindings)

    def get(self, role: str) -> Binding | None:
        return self._bindings.get(role)

    def set(
        self, role: str, voice: str, emo_ref: str | None = None, *, persist: bool = True
    ) -> Binding:
        """设置/覆盖一个绑定。`persist=True` 时立即原子落盘。"""
        if not isinstance(role, str) or not role:
            raise BindingError("角色名不能为空")
        if not isinstance(voice, str) or not voice:
            raise BindingError(f"角色 {role!r} 的音色名不能为空")
        binding = Binding(voice=voice, emo_ref=emo_ref)
        self._bindings[role] = binding
        if persist:
            self.save()
        return binding

    def remove(self, role: str, *, persist: bool = True) -> bool:
        existed = self._bindings.pop(role, None) is not None
        if existed and persist:
            self.save()
        return existed

    # -- 失效检测 --------------------------------------------------------- #

    def invalid(self, voice_names: Collection[str]) -> dict[str, str]:
        """检出绑定的音色已不存在的角色 → 角色: 原因。"""
        return {
            role: f"音色不存在: {binding.voice}"
            for role, binding in sorted(self._bindings.items())
            if binding.voice not in voice_names
        }

    def missing(
        self, required_roles: Collection[str], voice_names: Collection[str]
    ) -> list[str]:
        """开跑前校验:哪些必需角色「未绑定」或「绑定失效」。"""
        problems: list[str] = []
        for role in sorted(required_roles):
            binding = self._bindings.get(role)
            if binding is None:
                problems.append(f"{role}:未绑定音色")
            elif binding.voice not in voice_names:
                problems.append(f"{role}:绑定的音色不存在({binding.voice})")
        return problems
