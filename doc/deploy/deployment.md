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
| 体积预估 | **~150MB**(无 FFmpeg) |

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

## 7. 部署验收清单

- [ ] 三个卷都挂载且**可写**(重点验证中文目录名不乱码)
- [ ] `PUID`/`PGID` 与 NAS 共享文件夹所有者一致
- [ ] 容器内 locale 为 UTF-8(`locale` 命令确认)
- [ ] 能访问 TTS 后端,「测试连接」返回 ✓
- [ ] 音色库列出 37 个中文名音色,无乱码
- [ ] 新建工程 → 导入 JSON → 角色列表正确
- [ ] 跑一章 → 成品出现在 `audio/` → `process_audio/` 被清理
- [ ] 中途重启容器 → 重开点「全部开始」→ **只补未完成部分**
- [ ] 若走反代:SSE 进度能实时刷新(未开缓冲)
