# 前端设计(Vue3)

对应源码:`src/web/` · 状态:【已确认】

## 1. 边界

**硬规则 R6**:与后端**零代码共享**,只通过 HTTP/SSE 通信。
前端是**纯展示与交互层** —— 所有判定(哪些片段未完成、绑定是否有效)都由后端给出。

## 2. 页面清单

| 页面 | 内容 | 需求来源 |
|---|---|---|
| **工程列表** | 工作区内工程卡片(进度概览)、新建工程、打开工程 | 「新建工程 / 扫描固定目录」 |
| **角色绑定**(工程主页)| **角色列表 + 每个角色一个可搜索下拉框** | 「每个角色列表给出下拉框(支持搜索)」 |
| **音色库**(二级页)| 音色列表、新建、重命名、编辑、删除、试听 | 「音色新建、重命名、修改入口(可放在二级 web 页面)」 |
| **合成进度** | 章节列表、逐条进度、日志面板、开始/暂停/停止 | 「点击全部开始」「断点续传」 |
| **设置** | **后端选择**(provider 下拉)、URL、测试连接、超时/重试/并发、**由 `ParamSpec` 动态渲染的生成参数**、工作区与音色库路径 | 「手动填写 url 连接后端」「测试连接按钮」「重试次数」 |

## 3. 三个关键交互

### 3.1 角色绑定表(本项目的核心界面)

```
角色(来自 JSON 聚合)      绑定音色(可搜索下拉)        状态
─────────────────────────────────────────────────
旁白                    [ 男旁白2          ▾ ]     ✓
汪淼                    [ 汪淼              ▾ ]     ✓
常伟思                  [ 未绑定            ▾ ]     ⚠ 需绑定
```

要点:

- 下拉框**支持搜索**(角色多、音色 37+ 个,必须能打字过滤)
- 显示**未绑定**计数,开始合成前强提示
- 音色被删除 → 绑定项显示为**失效**并在 UI 高亮
- 批量操作(可选):按名称相似度推荐、一键应用推荐

### 3.2 服务端路径浏览(替代系统文件对话框)

**这是 Docker 部署带来的必要组件**(见 [api/design.md §3](../api/design.md#3-服务端路径浏览apifszbrowse)):

- ❌ 不使用 `<input type="file" webkitdirectory>`,也**不假设**浏览器能访问本机文件系统
- ✅ 调用 `/api/fs/browse?path=`,在页面内渲染一个**容器卷内**的目录树/面包屑导航
- 用于选择工作区、音色库、新工程导入 JSON 的源目录

### 3.3 测试连接

```
[后端地址 http://hinas-v4.ninglang.top:8300 ]  [ 测试连接 ]
 ↓
✓ 连通,延迟 2.4s          (可试听返回的测试音频)
✗ 后端不可达,请检查地址与网络
✗ 后端已启动但模型未加载
✗ 请求参数有误: <原始 message>
```

四种结果对应 [tts/design.md §5](../tts/design.md#5-测试连接需求要求) 的 `kind` 分类。

### 3.4 生成参数表单(动态渲染)

**不得硬编码** `temperature`/`top_p`/`top_k`/`num_beams` 这些 IndexTTS 的字段 ——
否则新增一个没有这些参数的后端就要改前端。

```
GET /api/settings → { active_provider, providers: {...} }
GET /api/settings/providers → [
    { id: "indextts2", label: "IndexTTS2",
      capabilities: { emotion: ["vector","ref_audio","text"], emotion_vector_dims: 8, ... },
      param_schema: [ {name:"temperature", label:"温度", type:"float",
                       default:0.8, minimum:0.1, maximum:2.0}, ... ] }
]
```

前端按 `param_schema` 的 `type` 渲染对应控件(bool→开关、enum→下拉、数值→滑块+输入),
**新增 provider 时前端零改动**。每个 provider 的参数独立存储,切换后端不丢失各自的配置。

> 后端返回的 `capabilities` 也用于界面降级提示:若当前 provider 不支持情感向量,
> 角色绑定页与进度页应显式提示,而不是静默忽略 chapter JSON 里的 `emo_vector`。

## 4. 进度与实时性

| 项 | 设计 |
|---|---|
| 数据源 | SSE `/api/jobs/events` |
| 断线重连 | 重连后先拉 `/api/jobs/state` 补齐,再订阅增量 |
| 页面刷新 | **不影响后端任务**(任务在后端进程内运行),刷新后重连即恢复视图 |
| ETA | 按「剩余条数 × 实测均值」粗估,标注为「约」 |
| 日志面板 | 分级展示,失败条目可定位到章节 + 序号 |

## 5. 技术栈建议

| 项 | 选择 | 理由 |
|---|---|---|
| 框架 | Vue 3 + `<script setup>` | 用户已确认 Vue3 |
| 构建 | Vite | 产出静态文件,由 FastAPI 托管 |
| UI 库 | Element Plus 或 Naive UI | 自带可搜索 `Select`、表格、上传,直接满足绑定表与音色管理 |
| 状态管理 | Pinia | 任务状态与工程上下文跨页共享 |
| 请求 | 原生 `fetch` + 一个薄封装 | SSE 用 `EventSource` |

> **构建产物须与后端解耦**:产物目录由 Docker 多阶段构建注入,前端不从后端 import 任何东西。

## 6. 待确认

- 是否需要**中文以外的界面语言**?(当前素材全中文,建议先做纯中文)
- 音色试听是否需要**波形图**?(建议先只做播放按钮,波形是锦上添花)

## 7. 目录草图

```
src/web/
├── index.html
├── vite.config.ts
├── package.json
└── src/
    ├── main.ts
    ├── api/                 # 后端调用封装(fetch + SSE)
    │   ├── client.ts
    │   └── types.ts         # 与后端 schemas 手写对齐的 TS 类型
    ├── stores/              # Pinia
    ├── components/
    │   ├── PathBrowser.vue      # 服务端路径浏览
    │   ├── VoiceSelect.vue      # 可搜索音色下拉
    │   └── ProgressPanel.vue
    └── views/
        ├── ProjectList.vue
        ├── ProjectBindings.vue
        ├── VoiceLibrary.vue
        ├── Synthesis.vue
        └── Settings.vue
```
