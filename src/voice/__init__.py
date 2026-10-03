"""音色库模块(M3)。

对应设计:`doc/voice/design.md`。

    entry.py    VoiceEntry + 单个音色目录的解析(§3)
    library.py  VoiceLibrary:扫描/新建/重命名/编辑/删除/取参考音频(§5)
"""

from __future__ import annotations

from .entry import VoiceEntry, parse_voice_dir, validate_voice_name
from .library import VoiceLibrary

__all__ = ["VoiceEntry", "VoiceLibrary", "parse_voice_dir", "validate_voice_name"]
