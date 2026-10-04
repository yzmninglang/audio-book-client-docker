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
| `/api/projects` | POST | 新建工程(建目录 + 从**容器内路径**导入 JSON) |
| `/api/projects/upload` | POST | 新建工程(**浏览器上传** JSON,multipart) |
| `/api/projects/{name}/chapters` | POST | 给已有工程**追加/替换章节**(multipart) |
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

**虚拟根 `/`**:根路径返回**允许的卷根列表**(而非磁盘 `/` 的内容),这样前端的面包屑「回到根」才有落点。

```json
{"path": "/", "parent": null, "virtual": true,
 "dirs": [{"name": "workspace", "path": "/workspace", "has_children": true},
          {"name": "voices",    "path": "/voices",    "has_children": true}]}
```

> 磁盘的 `/` 本身**仍然拒绝** —— 虚拟根只暴露被允许的两个卷根,不泄漏容器文件系统结构。

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

- **音频传输**——按接口性质分两类:
  - **资源型 GET**(章节成品 `.../audio`、音色试听 `.../preview`):直接返回 `audio/wav` **字节流**,让浏览器原生播放
  - **诊断型 POST**(`/api/tts/probe`):返回 JSON,其中 `sample_audio` 为 **base64 字符串**

> ⚠️ **不要指望 Pydantic 自动处理**:实测 `pydantic 2.13` / `fastapi 0.142` 下,
> `bytes` 字段在 JSON 模式下**不会**自动 base64 —— `model_dump_json` 抛 `PydanticSerializationError`、
> `jsonable_encoder` 抛 `UnicodeDecodeError`。
> 必须在路由里**显式** `base64.b64encode(...).decode("ascii")`。
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

## 8. 契约细节(已定稿)

> 前端(M6)先于后端(M5)落地,它对若干未钉死的形状做了合理假设。
> **这些假设在此正式确认为契约**,后端 `schemas.py` 必须与之对齐,否则前后端对不上。

### 8.1 `GET /api/projects/{name}` 详情

```json
{
  "name": "三体",
  "chapters": [{"name": "P01_扉页", "sentence_count": 103, "status": "pending",
                "output": null, "done_fragments": 0, "failed_fragments": 0}],
  "roles": [{"name": "旁白", "voice": "男旁白2", "emo_ref": null, "status": "ok"}],
  "bindings": {"version": 1, "bindings": {"旁白": {"voice": "男旁白2", "emo_ref": null}}},
  "manifest_summary": {"done": 0, "partial": 0, "pending": 1},
  "stale_chapters": [{"name": "P01_扉页", "stale_fragments": [12, 13]}],
  "warnings": []
}
```

`roles[].status` ∈ `ok` / `unbound` / `invalid`,**由后端计算**。
依据 [web/design.md §1](../web/design.md) 的 R6:「所有判定由后端给出」—— 前端不做同名推断。

**`stale_chapters`**:绑定被改动后,已生成片段所用的音色与当前绑定不符的章节。

⚠️ **必须用只读函数 `detect_binding_stale()`**([project/design.md §5.5](../project/design.md#55-绑定变更与后端变更的处理)):

- `chapters[].status` 表示 **manifest 里真实持久化的状态**,GET 详情**不得改写它**
- 若误用会就地改写的 `mark_binding_stale()`,同一请求内后续的 `done_fragments` 计数会**归零** ——
  一个实际有 3 条已完成片段的章节会显示成 0 条。这是**读接口产生副作用**导致的数据错误
- 正确语义:`status = done` + `stale_chapters` 里有它 = **「已完成,但基于旧绑定生成」**,由用户决定是否重做
- 会改写的 `mark_binding_stale()` 保留给**显式操作**(UI 的「重做受影响章节」)使用

> 为什么放在这里而不是 `synthesis/` 的对账里:完成态章节会在 §5.4 第 1 步被短路跳过,
> 绑定检查够不到它。而**「打开工程时提示绑定已变更」正是工程详情的天然职责** ——
> 用户一进页面就该看到,而不是等下次跑合成才发现。
>
> 语义照旧:**只提示,不自动重做**(用户可能只是试验),重做由 UI 的「重做受影响章节」按钮触发。

### 8.2 `PUT /api/projects/{name}/bindings` 请求体

```json
{"version": 1, "bindings": {"旁白": {"voice": "男旁白2", "emo_ref": null}}}
```

### 8.3 `POST /api/projects` 请求体

```json
{"name": "三体", "source_path": "/workspace/import/三体"}
```

`source_path` 为**容器内路径**,由 `/api/fs/browse` 选定(容器化约束 #8)。

### 8.4 `PUT /api/voices/{name}` 请求体

`multipart/form-data`,三个字段**均可选**:

| 字段 | 说明 |
|---|---|
| `audio`(**也接受 `file`**) | 新的参考音频文件。前端 `client.ts` 实际发送 `file`,为免对不上,后端两者都收 |
| `prompt_text` | 参考文本;省略=不改动,空串=清除(见 [voice/design.md §4](../voice/design.md)) |
| `rename` | 新音色名;省略=不改名 |

> ⚠️ FastAPI 的 `Form(str | None)` 会把**空串折叠成 `None`**,无法区分「省略」与「清空」。
> 需手动解析 `request.form()` 才能实现上表的语义。

### 8.5 `GET /api/settings/providers`

```json
[{"id": "indextts2", "label": "IndexTTS2",
  "capabilities": {"emotion": ["vector","ref_audio","text"], "emotion_vector_dims": 8,
                   "voice_modes": ["audio_bytes"], "needs_prompt_text": false,
                   "supports_concurrent": false, "returns_canonical": true},
  "param_schema": [{"name": "temperature", "label": "温度", "type": "float",
                    "default": 0.8, "minimum": 0.1, "maximum": 2.0}]}]
```

`param_schema` 位于 provider **顶层**(不在 `capabilities` 内),与 [web/design.md §3.4](../web/design.md) 一致。

### 8.6 `GET /api/jobs/state` 响应体

**队列语义(2026-10-03 修正)**:队列是**进程级全局单例**,允许多个工程先后入队,
按**提交顺序 FIFO** 执行;同一 `(工程, 章节)` 幂等去重。

```json
{"state": "running",
 "project": "三体", "current_chapter": "P01_扉页",
 "done_fragments": 42, "total_fragments": 103,
 "chapters": [{"name": "P01_扉页", "status": "synthesizing",
               "done": 42, "total": 103, "failed": 0}],
 "eta_seconds": 183,
 "pending": [{"project": "西瓜书", "chapters": 17}],
 "recent_logs": [{"level": "warn", "message": "..."}]}
```

| 字段 | 语义 |
|---|---|
| `state` | `idle` / `running` / `paused` / `stopped`(与 `models.JobState` 一致) |
| `project` / `current_chapter` / `done_fragments` / `total_fragments` / `chapters` / `eta_seconds` | **全部是「正在执行的那个工程」的**,不是"最后入队的工程" |
| `pending` | **排队中**(尚未开始)的工程,按入队顺序;`chapters` 是该工程待跑章节数 |

> ⚠️ **曾经的 bug**:`_project_name` 在每次入队时被覆盖 → A 在跑时入队 B,状态会显示"正在运行 B"。
> 必须改为记录**当前正在执行的**任务所属工程。
>
> ⚠️ `pending` 里的工程进度**不在这个接口里** —— 未开始的工程没有"进行中进度"。
> 前端展示某工程的完成度时,应取该工程自己的 manifest 状态(`GET /api/projects/{name}`),
> **不要**用全局队列的数字。这是"B 在跑、你在看 A 的页面却看到 B 的进度"的根因。
>
> `eta_seconds` 可为 `null` —— 前端会退化为「剩余条数 × 3s」本地估算。

### 8.7 SSE 帧格式(`GET /api/jobs/events`)

**标准形式为具名事件**(事件类型即 `models`/`synthesis/events.py` 中的事件名):

```
event: fragment_done
data: {"chapter":"P01_扉页","index":42,"elapsed_ms":3100,"done":42,"total":103}

event: job_state
data: {"state":"running"}
```

- 事件类型:`chapter_started` / `fragment_done` / `fragment_failed` /
  `chapter_concatenated` / `chapter_failed` / `job_state` / `log`
- 另有**心跳注释行**(`: keep-alive`)保活,防反代掐断空闲连接
- 前端同时兼容「默认 message 事件 + data 内带 `type`」的写法,但**后端按具名事件实现**

### 8.8 错误响应(全站统一)

```json
{"error": {"kind": "binding", "message": "角色未绑定音色: 常伟思"}}
```

`kind` 取自异常类的 `kind` 属性。领域异常在 `src/errors.py`;
**HTTP 语义专用**的异常在 `src/api/errors.py`(不污染领域层,见该文件 docstring)。

| `kind` | HTTP | 含义 |
|---|---|---|
| `invalid` | 400 | 参数/输入不合法 |
| `not_found` | 404 | 资源不存在 |
| `config` | 409 | 配置冲突(如绑定失效) |
| `binding` | 409 | 角色未绑定音色 |
| `conflict` | 409 | **操作与进行中的任务冲突**(如删除正在合成的工程/章节) |
| `retryable` / `fatal` / `manifest` | 500 | 内部错误 |

> **两个 409 的例外**:`AlreadyExistsError`(资源已存在)继承 `InvalidInputError`,
> `kind` 仍是 `invalid`,但错误处理器把状态码钉成 **409** —— 因为「已存在」不是"参数写错了"。
> 这类「kind 与状态码解耦」只用于这一处,别扩散。

### 8.9 章节 JSON 上传(需求扩展,2026-10-03)

**为什么需要**:原设计只支持「先把书拷进 NAS 卷 → 用路径浏览器选中」。
但音色库是支持浏览器上传的,工程却不行 —— 这个不对称让用户困惑(见 [requirements.md §5.1](../requirements.md#51-需求扩展记录))。

#### `POST /api/projects/upload` —— 上传建工程

`multipart/form-data`:

| 字段 | 说明 |
|---|---|
| `name` | 工程名 |
| `files` | 一个或多个章节 JSON(`.json`) |

行为:建目录骨架 → 把文件写入 `json/` → 返回工程详情(`ProjectDetailOut`)。

#### `POST /api/projects/{name}/chapters` —— 追加/替换章节

`multipart/form-data`:

| 字段 | 说明 |
|---|---|
| `files` | 一个或多个章节 JSON(`.json`) |

行为:写入 `json/`;**同名文件覆盖**。返回更新后的工程详情。

> **同名覆盖会自然触发续传机制**:该章 `json_sha256` 变化 → [project/design.md §5.4](../project/design.md#54-启动对账算法) 判定片段作废并重做。
> 不需要为「换稿」新增任何机制 —— 续传设计本来就为此准备。

#### ⚠️ 上传是新增的攻击面,必须设限

| 约束 | 值 | 理由 |
|---|---|---|
| 只接受 `.json` 扩展名 | — | 其余一律 400 |
| 文件名净化 | 取 `basename`,拒绝含 `/`、`\`、`..`、控制字符、超长名 | 防路径穿越写入卷外 |
| 单文件大小上限 | **8 MB** | 实测章节 JSON 约 33KB,留足余量 |
| 单次文件数上限 | **200** | 防资源耗尽 |
| 总大小上限 | **64 MB** | 同上 |
| 落盘路径 | 只能落在 `<工程>/json/` 内 | 复用 `resolve_within_roots` 的同类校验思路 |
| 工程名 | 复用 `validate_project_name` | 与既有建工程一致 |
| 工程重名 | **409**,`kind=invalid`(`AlreadyExistsError`) | 不静默覆盖;「已存在」不是"参数写错了",故状态码是 409 而非 400 |

> 与 `/api/fs/browse` 是同一类问题的两面:`browse` 防的是**读**越界,上传防的是**写**越界。

### 8.10 删除工程 / 删除章节(需求扩展,2026-10-03)

**为什么需要**:浏览器上传加上之后,用户必然会传错、建错。若不能删,试错成本高到没法用
(实测:用户直接丢进 workspace 的目录连"工程"都不算,却也删不掉)。

**这两条都是破坏性操作,处理原则与 `audio/.stale/` 一致 —— 可恢复,不真删。**

#### `DELETE /api/projects/{name}`

| 项 | 规则 |
|---|---|
| 前置检查 | **该工程有正在运行的任务 → 409**(删到一半会产生孤儿片段) |
| 行为 | 整个工程目录 **移到 `<workspace>/.trash/<工程名>-<YYYYmmdd-HHMMSS>/`** |
| 返回 | `{"deleted": "<工程名>", "trashed_to": ".trash/<工程名>-<时间戳>"}` |
| 安全 | 复用 `validate_project_name` + `resolve_within_roots`,拒绝路径穿越 |
| 不存在 | 404 |

> `.trash/` 会被工作区扫描忽略(它没有 `json/`,不是合法工程),但**会在路径浏览器里出现** —— 由用户显式清理。
> 这与 [project/design.md §7](project/design.md#7-细节决策已定稿) 中 `.stale/`「保留,不自动清理」的决策一致。

> ⚠️ **`.trash/` 必须位于 `workspace` 卷内,不要"优化"到其他位置。**
> `os.rename()` 只在**同一文件系统内**才是原子的。放到别的卷(如 `/config`)会让"移动"退化为
> **复制 + 删除**:工程可能含数 GB 音频,耗时长,且**中断后会得到一个撕成两半的工程**
> (一半在新位置、一半在旧位置)。放在 workspace 卷内是让删除**原子且瞬时**的必要条件,不是图方便。
>
> 副作用要知道:`.trash/` **占用 workspace 卷空间** —— 删除不等于释放,需手动清理。

#### `DELETE /api/projects/{name}/chapters/{chapter}`

| 项 | 规则 |
|---|---|
| 前置检查 | **该章节正在合成 → 409** |
| 删除 | `json/<章节>.json`、manifest 中该章记录、`process_audio/<章节>/` |
| 成品 | `audio/<章节>.wav` **移到 `audio/.stale/`**(不真删) |
| 返回 | 更新后的 `ProjectDetailOut` |
| 安全 | 章节名取 `basename` 并拒绝 `/`、`\`、`..`、控制字符;必须落在 `<工程>/json/` 内 |
| 不存在 | 404 |

> 与 §8.9 的上传同名覆盖互补:**覆盖**用于"换了稿子",**删除**用于"根本不该有这一章"。
