"""HTTP 路由(严格照 `doc/api/design.md` §2 的 15 条清单)。"""

from __future__ import annotations

from . import bindings, fs, jobs, projects, settings, tts, voices

__all__ = ["bindings", "fs", "jobs", "projects", "settings", "tts", "voices"]
