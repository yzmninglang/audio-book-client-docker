"""M5 HTTP 层离线测试(`fastapi.testclient`,**绝不启动真实服务器、绝不触碰真实后端**)。

覆盖 `doc/dev-workflow.md` §3 M5 完成判据:

    ① 15 条路由齐全(含 /api/fs/browse、/api/settings/providers)
    ② SSE 能实时推事件,并带心跳
    ③ fs/browse 挡住 `../` 路径穿越与符号链接逃逸
    ④ 音频试听接口返回字节流而非 base64
    ⑤ 工程详情返回 roles[].status 与 stale_chapters
    ⑥ 错误映射到正确 HTTP 状态码
    ⑦ src/web/dist 存在时托管并对 SPA 路由回落

`/api/tts/probe` 用注入的假 provider,**真实后端交互零次**。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from _synth_helpers import (
    CHAPTER,
    DEFAULT_VOICE,
    FAKE_AUDIO,
    make_project,
)
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import ProviderConfig, Settings
from src.models import CANONICAL_FORMAT, HealthResult, TTSResult
from src.synthesis import SynthesisQueue
from src.tts import registry
from src.tts.capabilities import ParamSpec, TTSCapabilities
from src.voice import VoiceLibrary

PROBE_AUDIO = b"\x00\xff\xfeRIFF-fake-probe-audio"  # 非 UTF-8,验证显式 base64


class ApiFakeProvider:
    """匹配 `registry` 构造契约的假 provider(离线)。"""

    id = "fake"
    label = "Fake TTS"
    contract_version = 1

    def __init__(self, config, *, probe_voice=None, http_client=None, sleep=None):
        self.config = config
        self._probe_voice = probe_voice
        self._delay = float((config.params or {}).get("delay", 0.0))

    @property
    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            emotion={"vector", "ref_audio"},
            emotion_vector_dims=8,
            voice_modes={"audio_bytes"},
            needs_prompt_text=False,
            supports_concurrent=False,
            returns_canonical=True,
            param_schema=[
                ParamSpec(
                    name="temperature",
                    label="温度",
                    type="float",
                    default=0.8,
                    minimum=0.0,
                    maximum=2.0,
                ),
                ParamSpec(name="style", label="风格", type="enum", default="叙述"),
            ],
        )

    def synthesize(self, req) -> TTSResult:
        if self._delay:
            time.sleep(self._delay)
        return TTSResult(audio=FAKE_AUDIO, format=CANONICAL_FORMAT, is_canonical=True)

    def probe(self) -> HealthResult:
        if self._probe_voice is None:
            return HealthResult(ok=False, kind="bad_request", message="未提供探测音色")
        return HealthResult(
            ok=True,
            kind="ok",
            message="合成成功",
            latency_ms=1,
            sample_audio=PROBE_AUDIO,
            sample_rate=22050,
            duration_ms=100,
        )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "voices").mkdir()
    (tmp_path / "config").mkdir()
    s = Settings()
    s.workspace_root = str(tmp_path)  # 工程直接建在 tmp_path 下
    s.voices_root = str(tmp_path / "voices")
    s.config_dir = str(tmp_path / "config")
    s.active_provider = "fake"
    s.providers["fake"] = ProviderConfig(
        base_url="http://fake.invalid", timeout=5.0, retries=1, params={"delay": 0.1}
    )
    return s


@pytest.fixture()
def app(settings: Settings):
    registry.register("fake", ApiFakeProvider)
    application = create_app(settings=settings, queue=SynthesisQueue())
    try:
        yield application
    finally:
        registry.PROVIDERS.pop("fake", None)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def project_env(tmp_path: Path, client: TestClient):
    """一个可合成的工程:一章 3 条、音色 `男旁白2`、已绑定 `旁白`。"""
    project, voices = make_project(tmp_path, n=3, book="三体")
    return project, voices


# --------------------------------------------------------------------------- #
# ① 15 条路由齐全
# --------------------------------------------------------------------------- #


def test_all_15_routes_present(client: TestClient):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    expected = [
        ("/api/fs/browse", "get"),
        ("/api/settings", "get"),
        ("/api/settings", "put"),
        ("/api/settings/providers", "get"),
        ("/api/tts/probe", "post"),
        ("/api/projects", "get"),
        ("/api/projects", "post"),
        ("/api/projects/{name}", "get"),
        ("/api/projects/{name}/bindings", "get"),
        ("/api/projects/{name}/bindings", "put"),
        ("/api/projects/{name}/chapters/{chapter}/audio", "get"),
        ("/api/voices", "get"),
        ("/api/voices", "post"),
        ("/api/voices/{name}", "put"),
        ("/api/voices/{name}", "delete"),
        ("/api/voices/{name}/preview", "get"),
        ("/api/jobs", "post"),
        ("/api/jobs/state", "get"),
        ("/api/jobs/events", "get"),
        ("/api/jobs/pause", "post"),
        ("/api/jobs/stop", "post"),
    ]
    for path, method in expected:
        assert path in paths, f"缺少路由 {path}"
        assert method in paths[path], f"{path} 缺少 {method}"


# --------------------------------------------------------------------------- #
# ③ fs/browse 安全:路径穿越 + 软链逃逸
# --------------------------------------------------------------------------- #


def test_browse_root_lists_and_parent_none(client: TestClient, tmp_path: Path):
    child = tmp_path / "子目录"
    (child / "深层").mkdir(parents=True)
    (tmp_path / "空目录").mkdir()

    resp = client.get("/api/fs/browse", params={"path": str(tmp_path)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == str(tmp_path.resolve())
    assert body["parent"] is None  # 已是允许的根
    names = {d["name"]: d["has_children"] for d in body["dirs"]}
    assert names["子目录"] is True
    assert names["空目录"] is False
    assert body["is_project"] is False


def test_browse_default_path_is_workspace(client: TestClient, tmp_path: Path):
    (tmp_path / "sub").mkdir()
    resp = client.get("/api/fs/browse")
    assert resp.status_code == 200
    assert resp.json()["path"] == str(tmp_path.resolve())
    assert "sub" in {d["name"] for d in resp.json()["dirs"]}


def test_browse_detects_project(client: TestClient, project_env):
    resp = client.get("/api/fs/browse", params={"path": str(project_env[0].root)})
    assert resp.status_code == 200
    assert resp.json()["is_project"] is True
    assert "json" in {d["name"] for d in resp.json()["dirs"]}


@pytest.mark.parametrize(
    "path",
    [
        "../../etc",  # 相对路径穿越
        "/etc",  # 绝对路径但卷外
        "/etc/passwd",  # 卷外文件
    ],
)
def test_browse_traversal_rejected(client: TestClient, path: str):
    # 注:`/` 现为虚拟根(见 test_browse_virtual_root_*),不再属于「被拒」集合。
    resp = client.get("/api/fs/browse", params={"path": path})
    assert resp.status_code == 400, f"{path} 未被拒绝: {resp.status_code}"
    assert resp.json()["error"]["kind"] == "invalid"


def test_browse_symlink_escape_rejected(client: TestClient, tmp_path: Path):
    link = tmp_path / "escape"
    link.symlink_to("/etc")
    resp = client.get("/api/fs/browse", params={"path": str(link)})
    assert resp.status_code == 400, "符号链接逃逸未被拒绝"
    assert resp.json()["error"]["kind"] == "invalid"


def test_browse_symlink_to_file_rejected(client: TestClient, tmp_path: Path):
    link = tmp_path / "passwd_link"
    link.symlink_to("/etc/passwd")
    resp = client.get("/api/fs/browse", params={"path": str(link)})
    assert resp.status_code == 400


def test_browse_nonexistent_is_404(client: TestClient, tmp_path: Path):
    resp = client.get("/api/fs/browse", params={"path": str(tmp_path / "不存在")})
    assert resp.status_code == 404
    assert resp.json()["error"]["kind"] == "not_found"


def test_browse_virtual_root_lists_only_allowed_roots(client: TestClient, tmp_path: Path):
    """虚拟根 `/`:只暴露允许的卷根,不泄漏磁盘 `/` 的内容。"""
    resp = client.get("/api/fs/browse", params={"path": "/"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "/"
    assert body["parent"] is None
    assert body["virtual"] is True
    assert body["is_project"] is False

    roots = {d["path"] for d in body["dirs"]}
    assert roots == {str(tmp_path), str(tmp_path / "voices")}
    assert len(body["dirs"]) == 2  # 仅两个卷根,不含 /etc、/usr 等
    assert all(d["path"] is not None for d in body["dirs"])


def test_browse_disk_root_paths_still_rejected(client: TestClient):
    """磁盘 `/` 本身与其他卷外绝对路径仍被拒绝(虚拟根不放松真实路径校验)。"""
    for path in ("/etc", "/usr", "/root"):
        resp = client.get("/api/fs/browse", params={"path": path})
        assert resp.status_code == 400, f"{path} 未被拒绝: {resp.status_code}"


# --------------------------------------------------------------------------- #
# settings / providers / probe
# --------------------------------------------------------------------------- #


def test_settings_get_and_merge_put(client: TestClient, tmp_path: Path):
    body = client.get("/api/settings").json()
    assert body["active_provider"] == "fake"
    assert body["workspace_root"] == str(tmp_path)
    assert body["concurrency"] == 1

    resp = client.put("/api/settings", json={"concurrency": 4, "log_level": "DEBUG"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["concurrency"] == 4
    assert resp.json()["log_level"] == "DEBUG"
    assert client.get("/api/settings").json()["concurrency"] == 4
    # 已落盘
    assert (tmp_path / "config" / "config.json").is_file()


def test_settings_put_provider_params_merge(client: TestClient):
    patch = {"providers": {"fake": {"params": {"delay": 0.5}}}}
    resp = client.put("/api/settings", json=patch)
    assert resp.status_code == 200
    fake = resp.json()["providers"]["fake"]
    assert fake["params"]["delay"] == 0.5
    assert fake["base_url"] == "http://fake.invalid"  # 深合并,未丢其他字段


def test_settings_put_unknown_field_is_409(client: TestClient):
    resp = client.put("/api/settings", json={"not_a_field": 1})
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "config"


def test_providers_shape(client: TestClient):
    resp = client.get("/api/settings/providers")
    assert resp.status_code == 200
    providers = {p["id"]: p for p in resp.json()}
    assert "fake" in providers and "indextts2" in providers

    fake = providers["fake"]
    assert set(fake) == {"id", "label", "capabilities", "param_schema"}
    assert fake["label"] == "Fake TTS"  # 来自 provider.label,而非接口层映射表
    assert providers["indextts2"]["label"] == "IndexTTS2"
    # param_schema 位于 provider 顶层,不在 capabilities 内(§8.5)
    assert "param_schema" not in fake["capabilities"]
    names = {p["name"] for p in fake["param_schema"]}
    assert "temperature" in names and "style" in names
    assert fake["capabilities"]["emotion"] == ["ref_audio", "vector"]  # 已排序


def test_probe_returns_base64_audio(client: TestClient, project_env):
    resp = client.post("/api/tts/probe", json={"provider": "fake", "voice": DEFAULT_VOICE})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["kind"] == "ok"
    assert base64.b64decode(body["sample_audio"]) == PROBE_AUDIO


def test_probe_without_voice_is_bad_request(client: TestClient):
    # 空音色库 → 无参考音频 → bad_request,且不联网
    resp = client.post("/api/tts/probe", json={"provider": "fake", "voice": "不存在"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["kind"] == "bad_request"


def test_probe_unknown_provider_is_409(client: TestClient):
    resp = client.post("/api/tts/probe", json={"provider": "nope"})
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "config"


# --------------------------------------------------------------------------- #
# 工程：列表 / 新建 / 详情
# --------------------------------------------------------------------------- #


def test_projects_list_empty(client: TestClient):
    assert client.get("/api/projects").json() == []


def test_create_project_and_detail(client: TestClient, tmp_path: Path):
    source = tmp_path / "import_src"
    source.mkdir()
    (source / f"{CHAPTER}.json").write_text(
        '[{"speaker":"甲","content":"你好","emo_vector":[0,0,0,0,0,0,0,0],"delay":50},'
        '{"speaker":"乙","content":"世界","emo_vector":[0,0,0,0,0,0,0,0],"delay":50}]',
        encoding="utf-8",
    )

    resp = client.post("/api/projects", json={"name": "新书", "source_path": str(source)})
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["name"] == "新书"
    assert detail["path"] == str((tmp_path / "新书").resolve())
    assert len(detail["chapters"]) == 1
    assert detail["chapters"][0]["sentence_count"] == 2
    # 两个角色均未绑定 → 后端计算 unbound
    statuses = {r["name"]: r["status"] for r in detail["roles"]}
    assert statuses == {"甲": "unbound", "乙": "unbound"}
    assert detail["manifest_summary"] == {"done": 0, "partial": 0, "pending": 1}
    assert detail["stale_chapters"] == []

    # 列表含该工程
    listed = {p["name"]: p for p in client.get("/api/projects").json()}
    assert "新书" in listed
    assert listed["新书"]["chapter_count"] == 1
    assert listed["新书"]["has_manifest"] is True

    # 重复创建 → 400
    dup = client.post("/api/projects", json={"name": "新书", "source_path": str(source)})
    assert dup.status_code == 400


def test_create_project_outside_roots_rejected(client: TestClient):
    resp = client.post("/api/projects", json={"name": "x", "source_path": "/etc"})
    assert resp.status_code == 400
    assert resp.json()["error"]["kind"] == "invalid"


def test_create_project_missing_json(client: TestClient, tmp_path: Path):
    empty = tmp_path / "no_json"
    empty.mkdir()
    resp = client.post("/api/projects", json={"name": "x", "source_path": str(empty)})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 合成任务 + 详情 status/stale + 音频字节流
# --------------------------------------------------------------------------- #


def _run_job_to_completion(client: TestClient) -> None:
    resp = client.post("/api/jobs", json={"project": "三体"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "running"
    assert client.app.state.queue.wait_idle(timeout=10.0)


def test_project_detail_roles_and_stale_chapters(client: TestClient, project_env, tmp_path: Path):
    project, _voices = project_env

    # 合成前:角色已绑定且音色存在
    detail = client.get("/api/projects/三体").json()
    role = {r["name"]: r for r in detail["roles"]}["旁白"]
    assert role["voice"] == DEFAULT_VOICE
    assert role["status"] == "ok"

    _run_job_to_completion(client)

    detail = client.get("/api/projects/三体").json()
    chapter = detail["chapters"][0]
    assert chapter["status"] == "done"
    assert chapter["done_fragments"] == 3
    assert chapter["output"] == f"audio/{CHAPTER}.wav"
    assert detail["manifest_summary"]["done"] == 1
    assert detail["stale_chapters"] == []

    # 绑定改到另一个音色 → stale_chapters 提示(不自动重做)
    VoiceLibrary(tmp_path / "voices").create("男旁白3", FAKE_AUDIO, "文本")
    put = client.put(
        "/api/projects/三体/bindings",
        json={"version": 1, "bindings": {"旁白": {"voice": "男旁白3", "emo_ref": None}}},
    )
    assert put.status_code == 200

    detail = client.get("/api/projects/三体").json()
    assert {r["name"]: r["status"] for r in detail["roles"]}["旁白"] == "ok"
    stale = {c["name"]: c["stale_fragments"] for c in detail["stale_chapters"]}
    assert stale == {CHAPTER: [1, 2, 3]}
    # 语义:status=done + 出现在 stale_chapters = 「已完成,但基于旧绑定生成」。
    # 真实持久化状态不被 stale 计算污染(读接口无副作用)。
    assert detail["chapters"][0]["status"] == "done"
    assert detail["chapters"][0]["done_fragments"] == 3
    assert detail["manifest_summary"]["done"] == 1


def test_get_detail_does_not_mutate_done_fragments(client: TestClient, project_env, tmp_path: Path):
    """回归:绑定变更后 GET 详情,已完成片段计数不得被 stale 计算静默改坏(GET 无副作用)。"""
    _run_job_to_completion(client)

    before = client.get("/api/projects/三体").json()["chapters"][0]
    assert before["status"] == "done" and before["done_fragments"] == 3

    VoiceLibrary(tmp_path / "voices").create("男旁白3", FAKE_AUDIO, "文本")
    client.put(
        "/api/projects/三体/bindings",
        json={"version": 1, "bindings": {"旁白": {"voice": "男旁白3", "emo_ref": None}}},
    )

    # 连续两次 GET:都应报真实状态(done / 3),且 stale 仅作提示
    for _ in range(2):
        detail = client.get("/api/projects/三体").json()
        chapter = detail["chapters"][0]
        assert chapter["status"] == "done"
        assert chapter["done_fragments"] == 3
        assert chapter["failed_fragments"] == 0
        assert detail["manifest_summary"]["done"] == 1
        assert {c["name"] for c in detail["stale_chapters"]} == {CHAPTER}


def test_bindings_invalid_version_is_409(client: TestClient, project_env):
    resp = client.put(
        "/api/projects/三体/bindings",
        json={"version": 2, "bindings": {"旁白": {"voice": DEFAULT_VOICE}}},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "binding"


def test_chapter_audio_streams_wav(client: TestClient, project_env):
    _run_job_to_completion(client)
    resp = client.get(f"/api/projects/三体/chapters/{CHAPTER}/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content[:4] == b"RIFF"


def test_chapter_audio_missing_is_404(client: TestClient, project_env):
    resp = client.get(f"/api/projects/三体/chapters/{CHAPTER}/audio")
    assert resp.status_code == 404


def test_jobs_state_pause_stop(client: TestClient, project_env):
    start = client.post("/api/jobs", json={"project": "三体"})
    assert start.status_code == 200
    assert start.json()["state"] == "running"

    paused = client.post("/api/jobs/pause")
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"

    stopped = client.post("/api/jobs/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"

    state = client.get("/api/jobs/state")
    assert state.status_code == 200
    assert state.json()["state"] == "stopped"
    client.app.state.queue.wait_idle(timeout=10.0)


def test_start_job_unknown_project_is_404(client: TestClient):
    resp = client.post("/api/jobs", json={"project": "不存在"})
    assert resp.status_code == 404
    assert resp.json()["error"]["kind"] == "not_found"


# --------------------------------------------------------------------------- #
# ② SSE 事件流 + 心跳
# --------------------------------------------------------------------------- #


def test_sse_streams_events_with_heartbeat(app):
    """SSE 帧生成器:具名事件 + 心跳注释行 + 断线清理(订阅归还)。"""
    import anyio

    from src.api.routes.jobs import open_event_stream

    queue = app.state.queue

    async def consume() -> str:
        stream = open_event_stream(queue, heartbeat=0.05)
        frames = [await stream.__anext__()]  # `: connected`
        queue.bus.emit("log", level="info", message="sse-hello")  # 多客户端广播
        for _ in range(60):
            frames.append(await stream.__anext__())
            if "sse-hello" in "".join(frames) and "keep-alive" in "".join(frames):
                break
        await stream.aclose()  # 触发 finally → unsubscribe
        return "".join(frames)

    text = anyio.run(consume)
    assert ": connected" in text
    assert ": keep-alive" in text, "SSE 未发送心跳注释行"
    assert "event: log" in text and '"message": "sse-hello"' in text, "SSE 未送出具名事件"
    assert queue.bus.subscriber_count == 0, "断线后订阅未清理"


def test_sse_route_media_type_and_headers(app):
    from types import SimpleNamespace

    from src.api.routes.jobs import job_events

    async def _not_disconnected() -> bool:
        return False

    request = SimpleNamespace(app=app, is_disconnected=_not_disconnected)
    response = job_events(request, queue=app.state.queue)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    # 尚未开始迭代,订阅未建立
    assert app.state.queue.bus.subscriber_count == 0


def test_sse_http_frames_over_asgi(app):
    """真·ASGI 层:HTTP 200 + `text/event-stream` + 帧内含事件与心跳(不启动服务器)。"""
    import anyio

    queue = app.state.queue
    app.state.sse_heartbeat_seconds = 0.05

    async def scenario() -> list[dict]:
        messages: list[dict] = []

        async def receive() -> dict:
            await anyio.sleep(30)  # 不主动断线,由 cancel scope 收尾
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)

        async def emit_later() -> None:
            await anyio.sleep(0.1)
            queue.bus.emit("log", level="info", message="sse-http")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/jobs/events",
            "raw_path": b"/api/jobs/events",
            "query_string": b"",
            "headers": [],
            "client": ("test", 0),
            "server": ("test", 80),
            "root_path": "",
        }
        with anyio.move_on_after(1.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(emit_later)
                await app(scope, receive, send)
        return messages

    messages = anyio.run(scenario)
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200
    headers = {k.decode(): v.decode() for k, v in messages[0]["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    text = body.decode("utf-8")
    assert ": keep-alive" in text
    assert "event: log" in text and "sse-http" in text
    assert queue.bus.subscriber_count == 0, "异步生成器在取消时未归还订阅"


# --------------------------------------------------------------------------- #
# 音色库 CRUD + 试听字节流
# --------------------------------------------------------------------------- #


def test_voices_crud_and_preview(client: TestClient, tmp_path: Path):
    created = client.post(
        "/api/voices",
        data={"name": "测试音色", "prompt_text": "参考文本"},
        files={"file": ("1.wav", FAKE_AUDIO, "audio/wav")},
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "测试音色"
    assert created.json()["prompt_text"] == "参考文本"

    names = {v["name"] for v in client.get("/api/voices").json()}
    assert "测试音色" in names

    # 试听:字节流
    preview = client.get("/api/voices/测试音色/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("audio/wav")
    assert preview.content[:4] == b"RIFF"

    # 重命名 + 清除 prompt_text(空串=清除)
    updated = client.put(
        "/api/voices/测试音色",
        data={"rename": "测试音色2", "prompt_text": ""},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "测试音色2"
    assert updated.json()["prompt_text"] is None

    # 删除 → 204;再取 → 404
    assert client.delete("/api/voices/测试音色2").status_code == 204
    assert client.get("/api/voices/测试音色2/preview").status_code == 404


def test_voice_create_duplicate_is_400(client: TestClient, tmp_path: Path):
    data = {"name": "音色A", "prompt_text": "x"}
    files = {"file": ("1.wav", FAKE_AUDIO, "audio/wav")}
    assert client.post("/api/voices", data=data, files=files).status_code == 200
    dup = client.post("/api/voices", data=data, files=files)
    assert dup.status_code == 400
    assert dup.json()["error"]["kind"] == "invalid"


def test_voice_update_unknown_is_404(client: TestClient):
    resp = client.put("/api/voices/不存在", data={"rename": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# ⑥ 错误映射 / ⑦ 静态托管与 SPA 回落
# --------------------------------------------------------------------------- #


def test_error_mapping_not_found(client: TestClient):
    resp = client.get("/api/projects/不存在")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["kind"] == "not_found"
    assert body["error"]["message"]


def test_validation_error_is_400_invalid(client: TestClient):
    resp = client.post("/api/projects", json={})  # 缺 name / source_path
    assert resp.status_code == 400
    assert resp.json()["error"]["kind"] == "invalid"


def test_static_spa_and_assets(client: TestClient):
    root = client.get("/")
    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert "id=\"app\"" in root.text

    # SPA 深链接 → 回落 index.html
    deep = client.get("/projects/三体/synthesis")
    assert deep.status_code == 200
    assert deep.headers["content-type"].startswith("text/html")

    # 构建产物资源
    from src.api.app import DIST_DIR

    asset_files = list((DIST_DIR / "assets").glob("*.js"))
    assert asset_files, "dist/assets 下应有构建产物"
    asset = client.get(f"/assets/{asset_files[0].name}")
    assert asset.status_code == 200

    # 未知 API 路径 → JSON 404(不回落 SPA)
    assert client.get("/api/definitely-not-here").status_code == 404
