"""能力与参数自描述(`TTSCapabilities` / `ParamSpec`)。

设计见 doc/tts/design.md §6.4 / §6.5。

领域层只传递 `params: dict`,键名来自本 provider 的 `param_schema`;
前端按 `param_schema` **动态渲染**设置表单 —— 因此新增一个后端,
领域模型与前端都零改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ParamType = Literal["bool", "int", "float", "str", "enum"]


@dataclass
class ParamSpec:
    """一个可配置生成参数的自描述。"""

    name: str
    label: str
    type: ParamType
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None

    def coerce(self, value: Any) -> Any:
        """把来自配置/前端的值转成本参数声明的类型。"""
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if self.type == "int":
            return int(value)
        if self.type == "float":
            return float(value)
        return str(value)


@dataclass
class TTSCapabilities:
    """后端能力自描述。"""

    emotion: set[str]  # 支持的情感意图:{"vector", "ref_audio", "text"}
    emotion_vector_dims: int | None  # 情感向量维度;不支持向量时为 None
    voice_modes: set[str]  # 支持的音色指定方式:{"audio_bytes", "voice_id"}
    needs_prompt_text: bool  # 是否要求参考文本(prompt_text)
    supports_concurrent: bool  # 是否可并发(IndexTTS2 后端无锁 → False)
    returns_canonical: bool  # 输出是否恒为标准内部格式(见设计 §8)
    param_schema: list[ParamSpec] = field(default_factory=list)
