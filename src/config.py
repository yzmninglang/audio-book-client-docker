"""配置:三层覆盖 —— 默认值 < config.json < 环境变量。

(见 doc/dev-workflow.md §1.4)

    开发时用本地路径,部署时用容器路径,**同一套代码零改动**。

所有文件 IO 显式 `encoding="utf-8"` —— 容器 locale 缺失时不会因为中文路径炸掉
(容器化约束 #3)。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

ENV_PREFIX = "AUDIOBOOK_"

DEFAULT_HOST = "0.0.0.0"  # 容器外要能访问(容器化约束 #4)
DEFAULT_PORT = 8000

DEFAULT_INDEXTTS_BASE_URL = "http://hinas-v4.ninglang.top:8300"


# --------------------------------------------------------------------------- #
# provider 配置
# --------------------------------------------------------------------------- #


@dataclass
class AuthConfig:
    """鉴权配置 —— **本次不使用**,为将来预留。

    原因见 doc/findings.md §7:后端在公网且无鉴权,但本次决策不改后端。
    预留字段使得将来加鉴权无需改配置结构。
    """

    kind: str = "none"  # none | bearer | header
    token: str | None = None
    header_name: str | None = None


@dataclass
class ProviderConfig:
    """单个 TTS 后端的配置。每个 provider 独立一份,切换后端不互相影响。"""

    base_url: str = ""
    timeout: float = 300.0
    retries: int = 3
    auth: AuthConfig = field(default_factory=AuthConfig)
    params: dict[str, Any] = field(default_factory=dict)


def _default_providers() -> dict[str, ProviderConfig]:
    return {
        "indextts2": ProviderConfig(
            base_url=DEFAULT_INDEXTTS_BASE_URL,
            timeout=300.0,
            retries=3,
            params={
                # 键名用请求体的拼写(sentence 而非 segment)—— 见 doc/tts/design.md §2.1
                "max_text_tokens_per_sentence": 120,
                "do_sample": True,
                "temperature": 0.8,
                "top_p": 0.8,
                "top_k": 30,
                "num_beams": 3,
                "repetition_penalty": 10.0,
                "length_penalty": 0.0,
                "max_mel_tokens": 1500,
            },
        )
    }


# --------------------------------------------------------------------------- #
# 全局设置
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    # 三个卷(容器内路径;本地开发用环境变量指到 dev-data/)
    workspace_root: str = "/workspace"
    voices_root: str = "/voices"
    config_dir: str = "/config"

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = "INFO"

    active_provider: str = "indextts2"
    providers: dict[str, ProviderConfig] = field(default_factory=_default_providers)

    # 合成行为
    concurrency: int = 1  # 后端无锁无并发保护,默认严格串行
    reference_max_seconds: float = 15.0  # 后端只读前 15 秒
    reference_max_sample_rate: int = 22050  # 只降不升

    @property
    def config_file(self) -> Path:
        return Path(self.config_dir) / "config.json"

    # -- 序列化 ---------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"配置文件中有未知字段: {sorted(unknown)}")

        raw = dict(data)
        providers_raw = raw.pop("providers", None)
        settings = cls(**raw)
        if providers_raw:
            settings.providers = {
                name: ProviderConfig(
                    base_url=p.get("base_url", ""),
                    timeout=float(p.get("timeout", 300.0)),
                    retries=int(p.get("retries", 3)),
                    auth=AuthConfig(**p.get("auth", {})),
                    params=dict(p.get("params", {})),
                )
                for name, p in providers_raw.items()
            }
        return settings

    def save(self, path: Path | None = None) -> None:
        target = path or self.config_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, target)  # 原子写
        except OSError as exc:
            raise ConfigError(f"写入配置失败: {target}: {exc}") from exc

    # -- 加载 ------------------------------------------------------------ #

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """按「默认值 < config.json < 环境变量」装载。

        `config_dir` 本身可被 `AUDIOBOOK_CONFIG` 覆盖,因此要分两步:
        先定目录 → 再读文件 → 最后叠加其余环境变量。
        """
        settings = cls()

        # 1) 配置文件位置(环境变量优先)
        env_config_dir = os.environ.get(f"{ENV_PREFIX}CONFIG")
        if env_config_dir:
            settings.config_dir = env_config_dir
        target = Path(path) if path else settings.config_file

        # 2) 文件层
        if target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"读取配置失败: {target}: {exc}") from exc
            settings = cls.from_dict(data)
            settings.config_dir = target.parent.as_posix()

        # 3) 环境变量层(最高优先级)
        settings._apply_env()
        return settings

    def _apply_env(self) -> None:
        env = os.environ

        mapping = {
            f"{ENV_PREFIX}WORKSPACE": "workspace_root",
            f"{ENV_PREFIX}VOICES": "voices_root",
            f"{ENV_PREFIX}CONFIG": "config_dir",
            f"{ENV_PREFIX}HOST": "host",
            f"{ENV_PREFIX}LOG_LEVEL": "log_level",
            f"{ENV_PREFIX}PROVIDER": "active_provider",
        }
        for env_key, attr in mapping.items():
            value = env.get(env_key)
            if value:
                setattr(self, attr, value)

        for env_key, attr, caster in (
            (f"{ENV_PREFIX}PORT", "port", int),
            (f"{ENV_PREFIX}CONCURRENCY", "concurrency", int),
        ):
            value = env.get(env_key)
            if value:
                try:
                    setattr(self, attr, caster(value))
                except ValueError as exc:
                    raise ConfigError(f"环境变量 {env_key} 不是合法数值: {value}") from exc

    # -- 便捷访问 --------------------------------------------------------- #

    def provider(self, provider_id: str | None = None) -> ProviderConfig:
        pid = provider_id or self.active_provider
        try:
            return self.providers[pid]
        except KeyError as exc:
            raise ConfigError(f"未配置的 provider: {pid}") from exc
