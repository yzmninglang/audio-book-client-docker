"""全局设置(`GET/PUT /api/settings`)与 provider 自描述(`GET /api/settings/providers`)。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request

from ...config import ProviderConfig, Settings
from ...errors import ConfigError
from ...tts import registry
from ...tts.capabilities import TTSCapabilities
from ..deps import SettingsDep
from ..schemas import (
    CapabilitiesOut,
    ParamSpecOut,
    ProviderInfoOut,
    SettingsOut,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def capabilities_to_out(caps: TTSCapabilities) -> CapabilitiesOut:
    return CapabilitiesOut(
        emotion=sorted(caps.emotion),
        emotion_vector_dims=caps.emotion_vector_dims,
        voice_modes=sorted(caps.voice_modes),
        needs_prompt_text=caps.needs_prompt_text,
        supports_concurrent=caps.supports_concurrent,
        returns_canonical=caps.returns_canonical,
    )


def param_spec_to_out(spec: Any) -> ParamSpecOut:
    return ParamSpecOut(
        name=spec.name,
        label=spec.label,
        type=spec.type,
        default=spec.default,
        minimum=spec.minimum,
        maximum=spec.maximum,
        choices=spec.choices,
    )


def settings_to_out(settings: Settings) -> SettingsOut:
    return SettingsOut(**settings.to_dict())


def _provider_config(settings: Settings, provider_id: str) -> ProviderConfig:
    return settings.providers.get(provider_id) or ProviderConfig()


@router.get("", response_model=SettingsOut)
def read_settings(settings: SettingsDep) -> SettingsOut:
    return settings_to_out(settings)


@router.get("/providers", response_model=list[ProviderInfoOut])
def list_providers(settings: SettingsDep) -> list[ProviderInfoOut]:
    """可用 provider + 各自 `capabilities` 与顶层 `param_schema`(§8.5,前端动态渲染)。"""
    out: list[ProviderInfoOut] = []
    for provider_id in sorted(registry.PROVIDERS):
        provider = registry.create(provider_id, _provider_config(settings, provider_id))
        try:
            caps = provider.capabilities
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        out.append(
            ProviderInfoOut(
                id=provider_id,
                label=provider.label,
                capabilities=capabilities_to_out(caps),
                param_schema=[param_spec_to_out(spec) for spec in caps.param_schema],
            )
        )
    return out


def _merge_settings(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把局部 patch 合并进完整设置字典;`providers` 逐 provider 深合并。"""
    merged = dict(current)
    for key, value in patch.items():
        if key == "providers" and isinstance(value, dict):
            providers = dict(merged.get("providers", {}))
            for pid, pcfg in value.items():
                existing = providers.get(pid)
                if isinstance(pcfg, dict) and isinstance(existing, dict):
                    providers[pid] = {**existing, **pcfg}
                else:
                    providers[pid] = pcfg
            merged["providers"] = providers
        else:
            merged[key] = value
    return merged


@router.put("", response_model=SettingsOut)
def update_settings(
    request: Request,
    settings: SettingsDep,
    patch: Annotated[dict[str, Any] | None, Body()] = None,
) -> SettingsOut:
    """局部更新并落盘(未知字段 → `ConfigError` → 409)。"""
    merged = _merge_settings(settings.to_dict(), patch or {})
    try:
        updated = Settings.from_dict(merged)
    except ConfigError:
        raise
    updated.config_dir = settings.config_dir
    updated.save()
    request.app.state.settings = updated
    return settings_to_out(updated)
