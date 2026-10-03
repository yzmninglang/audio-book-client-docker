"""Pydantic 出入参 —— 形状严格照 `doc/api/design.md` §8(契约已定稿)。

原则:
    * 响应模型只做**序列化形状**声明,判定逻辑一律来自下层(硬规则 R5)。
    * `bytes` 字段(Pydantic 在 JSON 模式下**不会**自动 base64)一律在路由层
      显式 base64 编码为 `str`(见 `routes/tts.py` 的 `sample_audio`)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# /api/fs/browse
# --------------------------------------------------------------------------- #


class FsEntryOut(BaseModel):
    name: str
    has_children: bool
    # 仅虚拟根 `/` 的下级(卷根)带显式路径;普通目录项为 None。
    path: str | None = None


class FsBrowseOut(BaseModel):
    path: str
    parent: str | None
    dirs: list[FsEntryOut]
    is_project: bool
    # 虚拟根标记:path="/" 时列出允许的卷根,而非磁盘 `/` 的内容。
    virtual: bool = False


# --------------------------------------------------------------------------- #
# /api/settings/providers —— 能力与参数自描述(前端动态渲染的依据,§8.5)
# --------------------------------------------------------------------------- #


class ParamSpecOut(BaseModel):
    name: str
    label: str
    type: str
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None


class CapabilitiesOut(BaseModel):
    """`TTSCapabilities` 的 JSON 形状。`param_schema` **不在此** —— 它位于 provider 顶层。"""

    emotion: list[str]
    emotion_vector_dims: int | None
    voice_modes: list[str]
    needs_prompt_text: bool
    supports_concurrent: bool
    returns_canonical: bool


class ProviderInfoOut(BaseModel):
    id: str
    label: str
    capabilities: CapabilitiesOut
    param_schema: list[ParamSpecOut]


# --------------------------------------------------------------------------- #
# /api/settings
# --------------------------------------------------------------------------- #


class AuthConfigOut(BaseModel):
    kind: str = "none"
    token: str | None = None
    header_name: str | None = None


class ProviderConfigOut(BaseModel):
    base_url: str = ""
    timeout: float = 300.0
    retries: int = 3
    auth: AuthConfigOut = Field(default_factory=AuthConfigOut)
    params: dict[str, Any] = Field(default_factory=dict)


class SettingsOut(BaseModel):
    workspace_root: str
    voices_root: str
    config_dir: str
    host: str
    port: int
    log_level: str
    active_provider: str
    providers: dict[str, ProviderConfigOut]
    concurrency: int
    reference_max_seconds: float
    reference_max_sample_rate: int


# --------------------------------------------------------------------------- #
# /api/tts/probe
# --------------------------------------------------------------------------- #


class ProbeRequest(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    voice: str | None = None
    timeout: float | None = None


class ProbeResponse(BaseModel):
    """`models.HealthResult` 的 JSON 形状;`sample_audio` 为 base64 字符串(非 bytes)。"""

    ok: bool
    kind: str
    message: str = ""
    latency_ms: int | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    sample_audio: str | None = None


# --------------------------------------------------------------------------- #
# /api/projects
# --------------------------------------------------------------------------- #


class ChapterSummaryOut(BaseModel):
    name: str
    sentence_count: int
    status: str  # pending | partial | done
    output: str | None = None
    provider: str | None = None
    done_fragments: int = 0
    failed_fragments: int = 0


class RoleViewOut(BaseModel):
    name: str
    voice: str | None = None
    emo_ref: str | None = None
    status: Literal["ok", "unbound", "invalid"]


class BindingOut(BaseModel):
    voice: str
    emo_ref: str | None = None


class BindingsFileOut(BaseModel):
    version: int = 1
    bindings: dict[str, BindingOut] = Field(default_factory=dict)


class ManifestSummaryOut(BaseModel):
    done: int = 0
    partial: int = 0
    pending: int = 0


class StaleChapterOut(BaseModel):
    name: str
    stale_fragments: list[int] = Field(default_factory=list)


class ProjectDetailOut(BaseModel):
    name: str
    path: str
    chapters: list[ChapterSummaryOut] = Field(default_factory=list)
    roles: list[RoleViewOut] = Field(default_factory=list)
    bindings: BindingsFileOut = Field(default_factory=BindingsFileOut)
    manifest_summary: ManifestSummaryOut = Field(default_factory=ManifestSummaryOut)
    stale_chapters: list[StaleChapterOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProjectSummaryOut(BaseModel):
    name: str
    path: str
    chapter_count: int
    done_count: int = 0
    sentence_count: int = 0
    progress: float = 0.0
    has_manifest: bool = False


class CreateProjectRequest(BaseModel):
    name: str
    source_path: str


# --------------------------------------------------------------------------- #
# 绑定读写(§8.2)
# --------------------------------------------------------------------------- #


class BindingIn(BaseModel):
    voice: str
    emo_ref: str | None = None


class BindingsIn(BaseModel):
    version: int = 1
    bindings: dict[str, BindingIn] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# /api/voices
# --------------------------------------------------------------------------- #


class VoiceEntryOut(BaseModel):
    name: str
    path: str
    main_audio: str
    prompt_text: str | None = None
    emo_refs: dict[str, str] = Field(default_factory=dict)
    is_normalized: bool = False
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# /api/jobs(§8.6)
# --------------------------------------------------------------------------- #


class ChapterProgressOut(BaseModel):
    name: str
    total: int = 0
    done: int = 0
    failed: int = 0
    status: str = "synthesizing"


class LogEntryOut(BaseModel):
    level: str = "info"
    message: str = ""
    chapter: str | None = None
    index: int | None = None
    ts: str | None = None


class JobStatusOut(BaseModel):
    state: str  # idle | running | paused | stopped
    project: str | None = None
    current_chapter: str | None = None
    done_fragments: int = 0
    total_fragments: int = 0
    chapters: list[ChapterProgressOut] = Field(default_factory=list)
    eta_seconds: int | None = None
    recent_logs: list[LogEntryOut] = Field(default_factory=list)


class StartJobRequest(BaseModel):
    project: str
    chapters: list[str] | None = None
    redo: bool = False
