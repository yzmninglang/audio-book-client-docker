"""provider 注册表。

> **加一个新的 TTS 后端 = 加一个文件 + 在 `PROVIDERS` 里注册一行。**

`audio/`、`synthesis/`、`api/`、`web/` 零改动(架构 R1+R3+R7 的收益)。

所有适配器遵守统一构造契约:

    Client(config: ProviderConfig, *, probe_voice=None, http_client=None, sleep=...) -> TTSProvider
"""

from __future__ import annotations

from typing import Any

from ..config import ProviderConfig
from ..errors import ConfigError
from ..models import TTSProvider
from .indextts_client import IndexttsClient

PROVIDERS: dict[str, type[TTSProvider]] = {
    "indextts2": IndexttsClient,
    # 新增后端: 在此加一行
}


def register(provider_id: str, client_cls: type[TTSProvider]) -> None:
    """注册一个 provider(新增后端时也可用装饰器/显式调用代替改字典)。"""
    PROVIDERS[provider_id] = client_cls


def create(provider_id: str, config: ProviderConfig, **kwargs: Any) -> TTSProvider:
    """按 id 实例化 provider。未知 id 抛出 `ConfigError`。"""
    try:
        client_cls = PROVIDERS[provider_id]
    except KeyError as exc:
        raise ConfigError(
            f"未知的 TTS provider: {provider_id!r}(已注册: {sorted(PROVIDERS)})"
        ) from exc
    return client_cls(config, **kwargs)
