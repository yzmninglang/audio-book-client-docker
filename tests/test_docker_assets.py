"""M7 容器化交付物的静态/等价验证(无需 Docker daemon)。

覆盖:
- Dockerfile:多阶段、locale、依赖集、无 FFmpeg
- docker-compose.yml:三个卷 + PUID/PGID + TZ + UTF-8 + 无 GPU
- .dockerignore:排除 .venv / node_modules / dist / dev-data / __pycache__ / tests
- entrypoint.sh:PUID/PGID → groupadd/groupmod + useradd/usermod + chown + 降权 exec
  (用假系统命令注入 PATH,纯逻辑校验,不依赖真实 useradd)
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


# --------------------------------------------------------------------------- #
# Dockerfile
# --------------------------------------------------------------------------- #

def test_dockerfile_is_multistage_with_expected_bases() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM node:20-alpine AS frontend" in text
    assert "FROM python:3.11-slim AS runtime" in text
    assert "npm ci" in text and "npm run build" in text


def test_dockerfile_sets_utf8_locale() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "LANG=C.UTF-8" in text
    assert "LC_ALL=C.UTF-8" in text


def test_dockerfile_installs_exact_pip_dependency_set_and_no_ffmpeg() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for dep in (
        "fastapi",
        '"uvicorn[standard]"',
        "pydantic",
        "httpx",
        "soundfile",
        "numpy",
        "soxr",
        "python-multipart",
    ):
        assert dep in text, f"缺少依赖 {dep}"
    lowered = text.lower()
    # 无 apt/apk/system 包(image 依赖全为 pip wheel)
    assert "apt-get" not in lowered and "apk add" not in lowered
    # 任何 RUN(安装)行都不得引入 FFmpeg(注释里提及 FFmpeg 不算)
    run_lines = [
        ln.lower() for ln in text.splitlines() if ln.strip().upper().startswith("RUN")
    ]
    assert all("ffmpeg" not in ln for ln in run_lines)


def test_dockerfile_does_not_copy_bloat_and_hosts_dist() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [
        ln for ln in text.splitlines()
        if ln.strip().upper().startswith(("COPY", "ADD"))
    ]
    for banned in (".venv", "node_modules", "dev-data", "tests/", "__pycache__"):
        assert all(banned not in ln for ln in copy_lines), f"COPY 行包含 {banned}"
    # 前端构建产物从 stage1 拷入,app.py 从 src/web/dist 托管
    assert "COPY --from=frontend /build/dist ./src/web/dist" in text
    # 入口脚本与工作目录
    assert "ENTRYPOINT" in text and "WORKDIR /app" in text


# --------------------------------------------------------------------------- #
# docker-compose.yml
# --------------------------------------------------------------------------- #

def test_compose_structure() -> None:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    svc = data["services"]["audiobook"]

    # 三个卷缺一不可(deployment.md §4.1)
    vols = "\n".join(svc["volumes"])
    assert "/workspace" in vols
    assert "/voices" in vols
    assert "/config" in vols

    # PUID/PGID + TZ + UTF-8(deployment.md §4)
    env = "\n".join(svc["environment"])
    for key in ("PUID=", "PGID=", "TZ=Asia/Shanghai", "LANG=C.UTF-8"):
        assert key in env, f"compose 环境缺少 {key}"

    assert svc["restart"] == "unless-stopped"
    # 无 GPU 需求
    assert "runtime" not in svc
    assert "deploy" not in svc


def test_compose_port_mapping() -> None:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    ports = data["services"]["audiobook"]["ports"]
    assert ports == ["8301:8000"]


# --------------------------------------------------------------------------- #
# .dockerignore
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "entry",
    [".venv/", "node_modules/", "dist/", "dev-data/", "__pycache__/", "tests/"],
)
def test_dockerignore_excludes_bloat(entry: str) -> None:
    lines = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert entry in lines, f".dockerignore 未排除 {entry}"


def test_dockerignore_keeps_build_inputs() -> None:
    lines = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    # 构建所需的 docker/ 与 pyproject.toml 不能被排除
    assert "docker/" not in lines
    assert "pyproject.toml" not in lines


# --------------------------------------------------------------------------- #
# entrypoint.sh — PUID/PGID 逻辑(注入假系统命令)
# --------------------------------------------------------------------------- #

_FAKES = {
    "id": "#!/bin/sh\necho 0\n",
    "date": "#!/bin/sh\necho 2026-10-03T00:00:00\n",
    "groupadd": "#!/bin/sh\nrecord groupadd \"$@\"\n",
    "groupmod": "#!/bin/sh\nrecord groupmod \"$@\"\n",
    "useradd": "#!/bin/sh\nrecord useradd \"$@\"\n",
    "usermod": "#!/bin/sh\nrecord usermod \"$@\"\n",
    "chown": "#!/bin/sh\nrecord chown \"$@\"\n",
    "chmod": "#!/bin/sh\nrecord chmod \"$@\"\n",
    "mkdir": "#!/bin/sh\nrecord mkdir \"$@\"\n",
    "setpriv": "#!/bin/sh\nrecord EXEC setpriv \"$@\"\n",
    "getent": textwrap.dedent(
        """\
        #!/bin/sh
        # 依据 M7_STATE 下的标记文件模拟 getent 命中/未命中
        kind="$1"; key="$2"
        if [ -f "$M7_STATE/${kind}_${key}" ]; then
            if [ "$kind" = "group" ]; then echo "app:x:${key}:"; fi
            if [ "$kind" = "passwd" ]; then echo "app:x:${key}:${key}::/:/usr/sbin/nologin"; fi
            exit 0
        fi
        exit 2
        """
    ),
}


def _make_fake_bin(tmp_path: Path) -> tuple[Path, Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name, body in _FAKES.items():
        p = fakebin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fakebin, tmp_path / "calls.log"


def _run_entrypoint(tmp_path: Path, *, puid: int, pgid: int, state: dict[str, bool]) -> str:
    fakebin, calls = _make_fake_bin(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for key, present in state.items():
        if present:
            (state_dir / key).write_text("", encoding="utf-8")

    # record() 供假命令调用
    (fakebin / "record").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$M7_CALLS"\n', encoding="utf-8"
    )
    (fakebin / "record").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "PUID": str(puid),
        "PGID": str(pgid),
        "M7_STATE": str(state_dir),
        "M7_CALLS": str(calls),
    }
    # record 是自定义命令,这里通过函数方式暴露给假脚本
    env["PATH"] = f"{fakebin}:{os.environ['PATH']}"
    # 让假脚本能找到 record:把它定义成可执行文件已在 PATH
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_entrypoint_creates_group_and_user_when_absent(tmp_path: Path) -> None:
    calls = _run_entrypoint(tmp_path, puid=1000, pgid=1000, state={})
    assert "groupadd -o -g 1000 app" in calls
    assert "useradd -o -u 1000 -g app -M -s /usr/sbin/nologin app" in calls
    # 三个卷都被 chown 到 PUID:PGID(已存在→-R 递归;不存在→先建后 chown)
    for vol in ("/workspace", "/voices", "/config"):
        assert any(
            line.startswith("chown") and "1000:1000" in line and line.endswith(vol)
            for line in calls.splitlines()
        ), f"未对 {vol} 执行 chown 1000:1000"
    # 降权 exec,默认命令 host=0.0.0.0 port=8000
    assert "EXEC setpriv --reuid=1000 --regid=1000 --clear-groups" in calls
    assert "uvicorn src.api.app:app --host 0.0.0.0 --port 8000" in calls


def test_entrypoint_modifies_existing_app_user_and_group(tmp_path: Path) -> None:
    # GID/UID 均不存在,但已有名为 app 的组与用户 → 走 groupmod / usermod
    calls = _run_entrypoint(
        tmp_path, puid=1001, pgid=1001, state={"group_app": True, "passwd_app": True}
    )
    assert "groupmod -o -g 1001 app" in calls
    assert "usermod -o -u 1001 -g app app" in calls
    assert "groupadd" not in calls
    assert "useradd" not in calls
    assert "EXEC setpriv --reuid=1001 --regid=1001 --clear-groups" in calls


def test_entrypoint_reuses_existing_uid_gid_without_modifying(tmp_path: Path) -> None:
    calls = _run_entrypoint(
        tmp_path, puid=1002, pgid=1002, state={"group_1002": True, "passwd_1002": True}
    )
    assert "groupadd" not in calls and "groupmod" not in calls
    assert "useradd" not in calls and "usermod" not in calls
    assert "EXEC setpriv --reuid=1002 --regid=1002 --clear-groups" in calls


def test_entrypoint_honours_audiobook_port(tmp_path: Path) -> None:
    fakebin, calls = _make_fake_bin(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (fakebin / "record").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$M7_CALLS"\n', encoding="utf-8"
    )
    (fakebin / "record").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "PUID": "1000",
        "PGID": "1000",
        "AUDIOBOOK_PORT": "9000",
        "M7_STATE": str(state_dir),
        "M7_CALLS": str(calls),
    }
    proc = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = calls.read_text(encoding="utf-8")
    assert "uvicorn src.api.app:app --host 0.0.0.0 --port 9000" in out
