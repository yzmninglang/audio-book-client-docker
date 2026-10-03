# 部署设计(Docker / NAS)

状态:【已确认】(2026-10-03)

## 1. 部署形态

**NAS 服务端**:单容器跑在 NAS 上,局域网内任意设备的浏览器访问。
不做桌面客户端,不做 Electron(用户决策)。

| 项 | 值 |
|---|---|
| 目标架构 | **x86_64**(无需 multi-arch) |
| 形态 | 单容器、单端口 |
| 前端 | Vue3 构建产物由 FastAPI 静态托管 |

## 1.1 交付物:部署包

仓库 `deploy/` 目录是可直接拷到 NAS 的部署包,**无需源码、无需构建**:

| 文件 | 说明 |
|---|---|
| `audiobook-client-1.0.0-amd64.tar` | 镜像(248MB,x86_64);`docker load` 后为 `audiobook-client:latest` |
| `docker-compose.yml` | NAS 用 compose(**无 `build:` 段** —— NAS 上没有源码) |
| `README.md` | 三步部署速查 + 第一坑与排障 |

> 仓库根目录的 `docker-compose.yml` 是**开发用**的(带 `build:`,可从源码构建);
> `deploy/` 里的是**部署用**的(只认已加载的镜像)。二者用途不同,别混用。

## 2. 为什么这个形态可行(实测支撑)

| 理由 | 依据 |
|---|---|
| 客户端**零算力** → 低功耗 NAS 可跑 | 无推理,仅 HTTP + 拼接 |
| 核心路径**不需要 FFmpeg** | 拼接/静音纯 stdlib,实测通过 |
| 数据天然该在 NAS | 卷挂载语义比桌面更顺 |
| **断点续传 = 架构红利** | 容器重启/升级不丢进度 |
| 局域网多设备访问 | 比装桌面客户端方便 |

## 3. 镜像设计

```dockerfile
# 多阶段构建
┌─ stage 1: node:20-alpine ────────────────────────┐
│  cd src/web && npm ci && npm run build           │
└──────────────────────────────────────────────────┘
                    ▼ 复制 dist/
┌─ stage 2: python:3.11-slim ──────────────────────┐
│  uv pip install fastapi uvicorn pydantic          │
│                 soundfile numpy soxr              │
│  ENV LANG=C.UTF-8 LC_ALL=C.UTF-8  ← 必须!         │
│  CMD uvicorn src.api.app:app --host 0.0.0.0       │
└──────────────────────────────────────────────────┘
```

| 决定 | 理由 |
|---|---|
| `python:3.11-slim` | 不需要 torch(客户端无算力) |
| 依赖全为 **pip wheel,无系统包** | soundfile 自带 libsndfile,soxr/numpy 有 wheel |
| **不装 FFmpeg** | 核心路径纯 stdlib;仅当需要 MP3 成品时才加 |
| **`LANG=C.UTF-8` 必须设置** | 音色目录**全是中文名**,locale 不对会乱码 |
| 依赖集 | `fastapi uvicorn[standard] pydantic httpx soundfile numpy soxr python-multipart` —— 严格对应 `pyproject.toml`,**漏一个都跑不起来**(`httpx` 打后端、`python-multipart` 供音色上传表单) |
| 无 `uv` | `python:3.11-slim` 不自带 uv,用其自带的 `pip install --no-cache-dir`(依赖仍是 pip wheel,决策不变) |
| 用 `ENTRYPOINT` 而非 `CMD` | PUID/PGID 调整必须**先于** exec 执行;`CMD` 会被覆盖掉调整逻辑 |
| 体积实测 | **253MB**(不是预估的 150MB —— `uvicorn[standard]` 会带上 uvloop/httptools/watchfiles/websockets,numpy 另有 ~20MB) |

> 若将来需要 MP3 输出,再加 `ffmpeg`(约 +80MB),或用 `lameenc` pip wheel。

## 4. docker-compose

```yaml
services:
  audiobook:
    image: audiobook-client:latest
    ports: ["8301:8000"]
    environment:
      - PUID=1000            # ← NAS 共享文件夹所有者
      - PGID=1000
      - TZ=Asia/Shanghai
      - LANG=C.UTF-8
    volumes:
      - /volume1/audiobook/workspace:/workspace    # 书籍工程
      - /volume1/audiobook/voices:/voices          # 音色库
      - /volume1/audiobook/config:/config          # 设置
    restart: unless-stopped
```

### 4.1 三个卷(缺一不可)

| 卷 | 内容 | 缺失后果 |
|---|---|---|
| `/workspace` | `<书名>/{json/, process_audio/, audio/}` | 无工程可跑 |
| `/voices` | 音色库(把 `reference/audio/` 的内容放进去) | 角色无法绑定音色 |
| `/config` | `config.json` | 配置丢失,回落默认值 |

### 4.2 PUID / PGID(**NAS 部署第一坑**)

NAS 共享文件夹的所有者与容器内 uid 通常不一致,直接导致**写 `process_audio/`、`audio/` 失败**。

→ 入口脚本按 `PUID`/`PGID` 调整运行用户,或文档中明确指导用户填写正确的值。
**这是 NAS 部署最常见的失败原因,必须在文档里显著提示。**

### 4.3 网络

| 项 | 说明 |
|---|---|
| 容器 → TTS 后端 | 后端在**公网**(`222.20.99.34:8300`),容器需能出网 |
| 后端若在同一 NAS | 建议改用内网地址/容器名,避免绕公网 |
| NAS 反代 | 若用 Nginx 反代,SSE 需**关闭响应缓冲**(`proxy_buffering off`),否则进度事件被攒着不发 |

## 5. 安全(实测发现,必须正视)

| 问题 | 说明 |
|---|---|
| TTS 后端在**公网** | `hinas-v4.ninglang.top` → `222.20.99.34` |
| **无鉴权** | `ui-api.py` 无任何凭证校验 |
| **无 TLS** | 明文 HTTP |

→ 任何知道该地址的人都能白嫖 GPU 做推理。

**本次决策**:不改后端,仅记录风险;客户端配置中**预留鉴权字段**,以后加不返工。

**部署建议**:

1. 客户端**不要直接暴露公网**;仅局域网可访问,或走 NAS 自带的 VPN/反代 + 鉴权
2. 后端建议至少加一层 API Key(可在 `ui-api.py` 前置一个反代做 IP 白名单)
3. 参考音频含版权内容,注意 NAS 的共享权限设置

## 6. 运维

| 项 | 建议 |
|---|---|
| 日志 | 落 `/config/logs/`(卷内),便于在 NAS 文件管理器里直接查看 |
| 备份 | 备份 `/workspace` 与 `/voices`;`/config` 很小也一并备份 |
| 升级 | 拉新镜像重建容器 → **manifest 保证进度不丢**,重开即续 |
| 镜像拉取 | 大陆拉 Docker Hub 慢 → 建议推送到阿里云 ACR,或提供离线 tar |
| 资源占用 | 无 GPU 需求;拼接为 CPU 轻负载(纯 PCM 字节连接) |

### 6.1 关于 `docker exec` 与运行用户

entrypoint 用 `setpriv` **把 uvicorn 进程**降权到 `PUID:PGID`,但镜像**没有设 `USER`** —— 因此:

- **应用进程**:uid 1000(已实测 `/proc/1/status`)
- **`docker exec`**:默认 **root**(除非显式 `docker exec -u 1000`)

排障时别被 `docker exec id` 显示的 root 误导 —— 它测的不是应用进程。
验证卷权限也必须带 `-u 1000`,否则测的是 root 的可写性,失去意义。

## 7. 部署验收清单

> 表中带 ✅ 的项已于 2026-10-03 在 WSL 真机构建(`docker build` + `docker run`)实测通过。

| # | 验收项 | 结果 | 实测证据 |
|---|---|---|---|
| 1 | 容器内 locale 为 UTF-8 | ✅ | `LANG=LC_ALL=C.UTF-8` |
| 2 | 37 个中文名音色无乱码 | ✅ | `GET /api/voices` 返回 37,`丁仪`…`顾灿`;`汪淼` 正确带出 6 组情感参考 |
| 3 | 三卷挂载且**可写** | ✅ | **以 `-u 1000` 实测**(见下注),三卷均可写;目录归 `app` |
| 4 | `PUID`/`PGID` 生效、应用降权 | ✅ | `/proc/1/status` → `Uid: 1000` |
| 5 | 能访问后端,「测试连接」返回 ✓ | ✅ | 容器内 `POST /api/tts/probe` → `ok=true`、`latency_ms=3769`、`sample_rate=22050` |
| 6 | 工程详情:角色聚合正确 | ✅ | `roles=[{name:"旁白",status:"unbound"}]`(音色库无「旁白」,判定正确);`sentence_count=103` |
| 7 | 前端托管 + SPA 回落 | ✅ | `/` 与 `/projects/三体` 均 200 text/html;`/api/nonexistent` 404 |
| 8 | 路径浏览安全 | ✅ | `path=/` 返回虚拟根;**`path=/etc` → 400** |
| 9 | **跑一章 → 成品 → 清理 `process_audio`** | ✅ | 4 句章节:进度 1/4→4/4,产出 `audio/P00_测试.wav`(22050/1ch/16bit,9.07s),`process_audio/` 下章节目录已清 |
| 10 | **容器重启后幂等跳过** | ✅ | `docker restart` 后 3s 恢复;重跑同章立刻 `idle 0/0`,**成品文件时间戳未变**(未被重做) |
| 11 | 若走反代:SSE 实时刷新 | ⏳ 未验证 | 本环境无反代;需在 NAS 上确认 `proxy_buffering off` |

> **第 3 项的方法学提醒**(踩过一次):`docker exec` 默认以镜像 USER(此处为 **root**,镜像未设 `USER`)运行,
> 因此裸 `docker exec ... touch` 测的是 root 的可写性,**证明不了应用能不能写**。
> 必须 `docker exec -u 1000` 才是 NAS 的真实场景。
