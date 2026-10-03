"""M0:配置三层覆盖 —— 默认值 < config.json < 环境变量。

见 doc/dev-workflow.md §1.4。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings
from src.errors import ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主机的 AUDIOBOOK_* 环境变量,保证测试可重复。"""
    import os

    for key in list(os.environ):
        if key.startswith("AUDIOBOOK_"):
            monkeypatch.delenv(key, raising=False)


def _write_config(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 三层优先级
# --------------------------------------------------------------------------- #


def test_defaults_when_no_file_and_no_env() -> None:
    settings = Settings.load(path=Path("/nonexistent/config.json"))

    assert settings.workspace_root == "/workspace"
    assert settings.voices_root == "/voices"
    assert settings.host == "0.0.0.0"  # 容器外要能访问(容器化约束 #4)
    assert settings.concurrency == 1  # 后端无并发保护
    assert settings.active_provider == "indextts2"
    assert settings.providers["indextts2"].base_url.startswith("http")


def test_file_layer_overrides_defaults(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path / "config.json",
        {
            "workspace_root": "/from/file",
            "port": 1234,
            "providers": {"indextts2": {"base_url": "http://example", "retries": 5}},
        },
    )

    settings = Settings.load(path=cfg)

    assert settings.workspace_root == "/from/file"
    assert settings.port == 1234
    assert settings.providers["indextts2"].retries == 5
    # 文件未提供的字段回落默认值
    assert settings.voices_root == "/voices"


def test_env_overrides_file_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path / "config.json", {"workspace_root": "/from/file", "port": 1234})
    monkeypatch.setenv("AUDIOBOOK_WORKSPACE", "/from/env")
    monkeypatch.setenv("AUDIOBOOK_PORT", "9999")

    settings = Settings.load(path=cfg)

    assert settings.workspace_root == "/from/env"
    assert settings.port == 9999
    # 未被环境变量覆盖的仍取文件层
    assert settings.providers["indextts2"].retries == 3


def test_env_only() -> None:
    import os

    os.environ["AUDIOBOOK_VOICES"] = "/tmp/v"
    try:
        assert Settings.load(path=Path("/nonexistent.json")).voices_root == "/tmp/v"
    finally:
        del os.environ["AUDIOBOOK_VOICES"]


def test_env_config_dir_redirects_file_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AUDIOBOOK_CONFIG 决定 config.json 的位置,因此必须先定目录再读文件。"""
    _write_config(tmp_path / "config.json", {"workspace_root": "/via/env-dir"})
    monkeypatch.setenv("AUDIOBOOK_CONFIG", str(tmp_path))

    assert Settings.load().workspace_root == "/via/env-dir"


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_unknown_field_is_rejected() -> None:
    """未知字段静默忽略会让「配置写错了」变成难查的 bug。"""
    with pytest.raises(ConfigError, match="未知字段"):
        Settings.from_dict({"nope": 1})


def test_invalid_env_number_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIOBOOK_PORT", "not-a-number")

    with pytest.raises(ConfigError, match="不是合法数值"):
        Settings.load(path=Path("/nonexistent.json"))


def test_unknown_provider_lookup_fails() -> None:
    with pytest.raises(ConfigError, match="未配置的 provider"):
        Settings().provider("does-not-exist")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


def test_save_is_atomic_and_round_trips(tmp_path: Path) -> None:
    settings = Settings(config_dir=str(tmp_path), workspace_root="/x", port=8080)
    settings.save()

    target = tmp_path / "config.json"
    assert target.is_file()
    assert not (tmp_path / "config.json.tmp").exists()  # 临时文件已被原子改名

    assert Settings.load(path=target).workspace_root == "/x"
    assert Settings.load(path=target).port == 8080


def test_save_preserves_chinese(tmp_path: Path) -> None:
    """音色名、书籍名都是中文,配置里也可能出现 —— 必须不转义乱码。"""
    settings = Settings(config_dir=str(tmp_path), workspace_root="/书籍/三体")
    settings.save()

    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "三体" in raw  # ensure_ascii=False
    assert Settings.load(path=tmp_path / "config.json").workspace_root == "/书籍/三体"


def test_save_reports_unwritable_target(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="写入配置失败"):
        Settings(config_dir=str(blocker / "sub")).save()
