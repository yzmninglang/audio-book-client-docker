"""测试连接(`POST /api/tts/probe`)—— 诊断型 POST,返回 JSON。

音频约定(design §5):诊断型 POST 的 `sample_audio` 为 **base64 字符串**。
注意 Pydantic 在 JSON 模式下**不会**自动 base64(遇非 UTF-8 字节直接报错),
因此本路由显式编码(见下方 `_health_to_out`)。
"""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter

from ...config import ProviderConfig
from ...errors import NotFoundError
from ...models import HealthResult, VoiceRef
from ...tts import registry
from ...voice import VoiceLibrary
from ..deps import SettingsDep, VoicesDep
from ..schemas import ProbeRequest, ProbeResponse

router = APIRouter(prefix="/api/tts", tags=["tts"])


def _health_to_out(result: HealthResult) -> ProbeResponse:
    sample_audio = (
        base64.b64encode(result.sample_audio).decode("ascii")
        if result.sample_audio
        else None
    )
    return ProbeResponse(
        ok=result.ok,
        kind=result.kind,
        message=result.message,
        latency_ms=result.latency_ms,
        duration_ms=result.duration_ms,
        sample_rate=result.sample_rate,
        sample_audio=sample_audio,
    )


def _resolve_voice_ref(voices: VoiceLibrary, voice_name: str | None) -> VoiceRef | None:
    """探测用参考音色字节:优先指定音色,否则取音色库第一个;都无则 None。"""
    if voice_name:
        try:
            return VoiceRef(audio=voices.reference_audio(voice_name))
        except NotFoundError:
            return None
    entries = voices.list()
    if not entries:
        return None
    try:
        return VoiceRef(audio=Path(entries[0].main_audio).read_bytes())
    except OSError:
        return None


@router.post("/probe", response_model=ProbeResponse)
def probe(body: ProbeRequest, settings: SettingsDep, voices: VoicesDep) -> ProbeResponse:
    """对指定(或激活)provider 发一次极短真实合成探测(诊断型,返回 JSON)。"""
    provider_id = body.provider or settings.active_provider

    config = settings.providers.get(provider_id) or ProviderConfig()
    if body.base_url:
        config = replace(config, base_url=body.base_url)
    if body.timeout:
        config = replace(config, timeout=float(body.timeout))

    voice_ref = _resolve_voice_ref(voices, body.voice)

    provider = registry.create(provider_id, config, probe_voice=voice_ref)
    try:
        result = provider.probe()
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return _health_to_out(result)
