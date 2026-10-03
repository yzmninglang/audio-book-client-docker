"""单个音色的解析 —— 「目录即音色,目录名即音色名」。

解析规则严格照 `doc/voice/design.md` §3(处理实测发现的三种异常结构,
见 `doc/findings.md` §1.1):

    | 文件/目录                                   | 判定           | 处理                          |
    |---------------------------------------------|----------------|-------------------------------|
    | `1.wav`                                     | 主参考音频     | 使用                          |
    | `pt.txt`                                    | 参考文本       | 保留为元数据                  |
    | `<名称>.wav` + 同名 `.txt`(非 `1`)          | 情感参考音频   | 识别为情感参考集              |
    | `output_spleeter/` / 含 vocals|accompaniment 的子目录 | 人声分离产物 | **忽略**             |
    | `1 - 副本.wav` / `xxx(1).wav`               | 副本           | **忽略**                      |
    | 无 `1.wav` 但有其他 `*.wav`                 | 兜底           | 取排序第一个 + 告警「非标准」 |
    | 整个目录无可用音频                          | 无效音色       | 不作为音色列出(`None`)        |

**扩展名不可信**:`陈平安1/1.wav` 实为 MP3(`doc/findings.md` §1.1),
因此主音频只看*文件名* `1`,真实格式交给 `audio.probe()` 判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import InvalidInputError

# 视为音频的扩展名集合(内容真实格式由 audio.probe 判定,这里只做筛选)。
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".aiff", ".aif", ".m4a", ".mp4"}
)

MAIN_AUDIO_STEM = "1"
PROMPT_TEXT_FILENAME = "pt.txt"
NORMALIZED_MARKER = ".normalized"
SPLEETER_DIRNAME = "output_spleeter"
SEPARATION_MARKERS = ("vocals.wav", "accompaniment.wav")

# 目录名的字节上限(Linux NAME_MAX = 255)。
MAX_NAME_BYTES = 255

_COPY_SUFFIX_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
_FORBIDDEN_NAME_CHARS = ("/", "\\", "\x00")


@dataclass
class VoiceEntry:
    """一个音色的解析结果。字段与 `doc/voice/design.md` §6 一致。"""

    name: str  # 目录名 = 音色名
    path: str  # 音色目录
    main_audio: str  # 主参考音频路径
    prompt_text: str | None  # pt.txt 内容
    emo_refs: dict[str, str]  # 情感名 -> 音频路径
    is_normalized: bool
    warnings: list[str] = field(default_factory=list)


def validate_voice_name(name: str) -> str:
    """校验音色名可安全用作目录名(design §7)。

    拒绝:空名、路径穿越(`..`、`/`、`\\`)、NUL、控制字符、首尾空白、超长名。

    Args:
        name: 待校验的音色名。

    Returns:
        校验通过的原名(不做任何改写)。

    Raises:
        InvalidInputError: 名字不合法。
    """
    if not isinstance(name, str):
        raise InvalidInputError("音色名必须是字符串", detail={"name": repr(name)})

    if not name or not name.strip():
        raise InvalidInputError("音色名不能为空", detail={"name": name})

    if name != name.strip():
        raise InvalidInputError("音色名首尾不能有空白字符", detail={"name": name})

    if name in (".", ".."):
        raise InvalidInputError("音色名不能是 '.' 或 '..'", detail={"name": name})

    for ch in _FORBIDDEN_NAME_CHARS:
        if ch in name:
            raise InvalidInputError(
                f"音色名不能包含 {ch!r}(路径分隔/穿越风险)", detail={"name": name}
            )

    for ch in name:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise InvalidInputError("音色名不能包含控制字符", detail={"name": repr(name)})

    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise InvalidInputError(
            f"音色名过长(超过 {MAX_NAME_BYTES} 字节)", detail={"name": name}
        )

    return name


def is_copy_stem(stem: str) -> bool:
    """判定文件主名是否为「副本」(design §3:` - 副本` / `(1)` 等模式)。"""
    if "副本" in stem:
        return True
    return bool(_COPY_SUFFIX_RE.search(stem))


def is_separation_dir(child: Path) -> bool:
    """判定子目录是否为人声分离产物(应整体忽略)。"""
    if not child.is_dir():
        return False
    if child.name == SPLEETER_DIRNAME:
        return True
    return any((child / marker).is_file() for marker in SEPARATION_MARKERS)


def _is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def _read_prompt_text(directory: Path) -> str | None:
    prompt_file = directory / PROMPT_TEXT_FILENAME
    if not prompt_file.is_file():
        return None
    try:
        text = prompt_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _collect_emotion_refs(directory: Path) -> dict[str, str]:
    """收集情感参考集:`<名称>.<音频扩展>` 且存在同名 `.txt`(非 `1`)。"""
    refs: dict[str, str] = {}
    for child in directory.iterdir():
        if not _is_audio_file(child):
            continue
        stem = child.stem
        if stem == MAIN_AUDIO_STEM or is_copy_stem(stem):
            continue
        text_file = child.with_suffix(".txt")
        if text_file.is_file():
            refs[stem] = str(child)
    return dict(sorted(refs.items()))


def _pick_main_audio(directory: Path) -> tuple[Path | None, list[str]]:
    """挑主参考音频:优先 `1.*`;否则兜底取排序第一个并告警。"""
    warnings: list[str] = []

    for child in sorted(directory.iterdir()):
        if child.stem == MAIN_AUDIO_STEM and _is_audio_file(child) and not is_copy_stem(child.stem):
            return child, warnings

    candidates = sorted(
        child
        for child in directory.iterdir()
        if _is_audio_file(child) and not is_copy_stem(child.stem)
    )
    if not candidates:
        return None, warnings

    fallback = candidates[0]
    warnings.append(f"非标准结构:未找到 1.wav,回退使用 {fallback.name}")
    return fallback, warnings


def parse_voice_dir(directory: Path | str) -> VoiceEntry | None:
    """解析一个音色目录(design §3)。

    Args:
        directory: 音色目录(目录名即音色名)。

    Returns:
        `VoiceEntry`;目录无可用音频时返回 `None`(不作为音色列出)。

    Raises:
        InvalidInputError: 目录名不合法(路径穿越等)。
    """
    directory = Path(directory)
    validate_voice_name(directory.name)

    main_audio, warnings = _pick_main_audio(directory)
    if main_audio is None:
        return None

    return VoiceEntry(
        name=directory.name,
        path=str(directory),
        main_audio=str(main_audio),
        prompt_text=_read_prompt_text(directory),
        emo_refs=_collect_emotion_refs(directory),
        is_normalized=(directory / NORMALIZED_MARKER).is_file(),
        warnings=warnings,
    )
