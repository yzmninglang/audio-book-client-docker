# HTTP 层设计

对应源码:`src/api/` · 状态:【已确认】

## 1. 职责与边界

**职责**:HTTP 入口 —— Pydantic 出入参校验、调用下层、序列化、SSE 推送、静态托管前端产物。

**边界(硬规则 R5)**:**不含业务逻辑**。任何判断「该不该重做某章」之类的逻辑都在 `synthesis/` 或 `project/`。

## 2. 路由清单

| 路由 | 方法 | 作用 |
|---|---|---|
| `/api/fs/browse?path=` | GET | **服务端卷内路径浏览**(替代系统文件对话框) |
| `/api/settings` | GET/PUT | 全局设置:工作区路径、音色库路径、激活的后端、超时、重试、并发 |
| `/api/settings/providers` | GET | 可用 provider 列表 + 各自 `capabilities` 与 `param_schema`(供前端**动态渲染**) |
| `/api/tts/probe` | POST | **测试连接**(真实极短合成探测) |
| `/api/projects` | GET | 列出工作区内工程 |
| `/api/projects` | POST | 新建工程(建目录 + 导入 JSON) |
| `/api/projects/{name}` | GET | 工程详情:章节清单、角色列表、绑定、manifest 摘要 |
| `/api/projects/{name}/bindings` | GET/PUT | 角色→音色 绑定读写 |
| `/api/projects/{name}/chapters/{ch}/audio` | GET | 试听章节成品 |
| `/api/voices` | GET | 音色列表 |
| `/api/voices` | POST | 新建音色(上传音频 + 参考文本) |
| `/api/voices/{name}` | PUT/DELETE | 重命名 / 编辑 / 删除 |
| `/api/voices/{name}/preview` | GET | 试听参考音频 |
| `/api/jobs` | POST | 开始(全部 / 指定章节) |
| `/api/jobs/state` | GET | 当前任务状态(SSE 断线后的兜底) |
| `/api/jobs/events` | GET | **SSE** 进度事件流 |
| `/api/jobs/pause` · `/stop` | POST | 暂停 / 停止 |

## 3. 服务端路径浏览(`/api/fs/browse`)

**这是 Docker 部署带来的必要设计**(见 [architecture.md §7](../architecture.md#7-三条从部署形态推导的硬约束)):
容器内没有系统文件对话框,用户只能选容器内已挂载的路径。

```
GET /api/fs/browse?path=/workspace/三体
→ { "path": "/workspace/三体",
    "parent": "/workspace",
    "dirs": [{"name": "json", "has_children": true}, ...],
    "is_project": true }
```

**安全约束(必须实现)**:

| 约束 | 原因 |
|---|---|
| 路径必须**规范化后**校验前缀,限制在允许的根(工作区/音色库卷)内 | 防目录穿越读容器内任意文件 |
| 拒绝符号链接逃逸 | 软链可绕过前缀检查 |
| 目录项数量上限 | 防超大目录拖垮响应 |
| 只读 | 本接口不提供任何写操作;建目录走工程创建接口 |

## 4. SSE 进度推送

```
GET /api/jobs/events        (text/event-stream)
```

| 设计点 | 决定 |
|---|---|
| 断线重连 | 前端重连后先调 `/api/jobs/state` 补齐当前状态,再订阅增量事件 |
| 背压 | 按片段完成粒度推送(约每 2~4s 一条),不做高频刷新 |
| 多客户端 | 事件广播给所有订阅者(队列是全局单例,状态天然共享) |
| 心跳 | 定期发注释行保活(反代常会掐断空闲连接) |

> 反代注意:NAS 上的 Nginx 等反代需要关闭 SSE 的响应缓冲,否则事件会被攒着不发。写进部署文档。

## 5. 出入参要点

- **音频传输**:试听类接口直接返回 `audio/wav` 字节流(不是 base64),让浏览器原生播放
- **上传**:音色新建/编辑用 `multipart/form-data`
- **错误格式**:统一 `{"error": {"kind": "...", "message": "..."}}`,
  `kind` 复用 `errors.py` 的分类(`retryable` / `fatal` / `config` / `not_found` / `invalid`)
- **长耗时接口**:`/api/jobs` 立即返回(入队即返回),不阻塞等待整章完成 —— 进度走 SSE

## 6. 静态托管与部署形态

```
src/api/app.py
  ├── /api/**              → 路由
  ├── /assets/**           → Vue3 构建产物
  └── /**                  → 回落到 index.html(SPA 路由)
```

单容器单端口:FastAPI 同时提供 API 与前端静态文件,最适合 NAS 部署。
前端构建产物由 Docker 多阶段构建注入(见 [deploy/deployment.md](../deploy/deployment.md))。

## 7. 待确认

- 是否需要**访问鉴权**?当前决策为局域网可信环境、不做登录。
  若 NAS 有端口暴露风险,建议至少加一层反代 Basic Auth(部署层解决,不改应用代码)。
