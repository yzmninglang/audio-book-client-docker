# 有声书合成客户端 · 前端(Vue3)

对应设计:`doc/web/design.md`,API 契约:`doc/api/design.md`。

纯展示与交互层。**与后端零代码共享**(架构硬规则 R6),只通过 HTTP/SSE 通信。

## 目录

```
src/
├── main.ts               # 入口;条件装载 mock
├── App.vue               # 顶栏 + 路由出口
├── router.ts             # 路由
├── api/
│   ├── client.ts         # 后端调用封装(fetch + SSE),15 条路由集中于此
│   ├── types.ts          # 与后端 schemas 对齐的 TS 类型
│   └── mock.ts           # 开发用 mock(不进入生产构建)
├── stores/               # Pinia:settings / voices / projects / jobs
├── components/
│   ├── PathBrowser.vue   # 服务端卷内路径浏览(容器化约束 #8)
│   ├── VoiceSelect.vue   # 可搜索音色下拉
│   └── ProgressPanel.vue
└── views/
    ├── ProjectList.vue       # 工程列表 + 新建 + 打开
    ├── ProjectBindings.vue   # 角色→音色绑定(核心界面)
    ├── VoiceLibrary.vue      # 音色库二级页
    ├── Synthesis.vue         # 合成进度 + 开始/暂停/停止 + 日志
    └── Settings.vue          # 后端地址 + 测试连接 + 动态参数表单
```

## 启动

### 1) 纯 mock 开发/演示(无需后端)

```bash
cd src/web
npm install
npm run dev:mock          # 等价于 VITE_USE_MOCK=1 vite
# 打开 http://localhost:5173
```

mock 覆盖全部 15 条路由(工程/角色/绑定/音色/路径浏览/设置/providers/测试连接/任务与进度事件流),
顶栏会显示「MOCK 模式」角标。任务进度(SSE)在 mock 下由定时器模拟流动。

**演示测试连接的四种结果**:在设置页把「后端地址」填成含关键字的 URL 再点「测试连接」:

| 关键字(不区分大小写) | 返回 kind |
|---|---|
| `unreachable` / `不可达` / `offline` | `unreachable` |
| `noload` / `未加载` / `model-not-loaded` | `model_not_loaded` |
| `bad` / `参数` / `invalid` | `bad_request` |
| `error` / `500` / `server` | `server_error` |
| 其他(如默认地址) | `ok`(含延迟 + 音频时长 + 可试听) |

控制台可调用 `window.__AUDIOBOOK_MOCK_RESET__()` 重置任务模拟状态。

### 2) 对接真实后端

```bash
cd src/web
npm install
npm run dev               # dev server 会把 /api 代理到 VITE_API_PROXY(默认 http://localhost:8000)
```

或构建静态产物,由 FastAPI 托管(见 doc/api/design.md §6):

```bash
npm run build             # 产出 dist/
```

## mock 如何不进入生产构建

`main.ts` 中:

```ts
if (import.meta.env.VITE_USE_MOCK === '1') {
  const { installMock } = await import('./api/mock')
  installMock()
}
```

生产构建时 `VITE_USE_MOCK` 未设置(为假),该分支被 tree-shake,动态 import 被消除,
`mock.ts` 不进入 `dist/`。`client.ts` 也**不 import** mock —— 它只惰性读取
`window.__AUDIOBOOK_MOCK__` 这个运行时桥接对象。可用如下命令校验:

```bash
npm run build
grep -rl "__AUDIOBOOK_MOCK_MARKER__" dist/ && echo "!! mock 泄漏进产物" || echo "OK: 产物中无 mock"
```

## 关键实现说明

- **PathBrowser** 一律走 `GET /api/fs/browse?path=`,页面内渲染容器卷内目录;
  不使用 `<input type="file" webkitdirectory>`,不假设浏览器能访问本机文件系统。
- **角色绑定页** 每个角色一个 `VoiceSelect`(Element Plus `filterable` 可搜索);
  未绑定/失效均有计数与高亮提示,开始合成前强提示。
- **设置页生成参数** 完全由 `GET /api/settings/providers` 返回的 `param_schema` 动态渲染
  (`bool→开关`、`enum→下拉`、`int/float→滑块+数字输入`、`str→输入框`);
  代码中不出现任何硬编码参数名。每个 provider 的参数独立存储。
- **绑定变更提示**:`GET /api/projects/{name}` 返回 `stale_chapters`(片段基于旧绑定生成的章节)。
  角色绑定页与合成进度页各有一条横幅列出受影响章节,合成页提供「重做受影响章节」显式按钮
  —— **只提示,不自动重做**。旧版后端不返回该字段时前端按空数组容错。
- **进度** 走 SSE(`GET /api/jobs/events`);后端按**具名事件**发送(`event: <type>`),
  前端同时兼容默认 message 事件;重连时先 `GET /api/jobs/state` 补齐再订阅增量;
  页面刷新不影响后端任务,重连即恢复视图。
- 音频试听接口返回 `audio/wav` 字节流,前端取 Blob URL 交给原生 `<audio>` 播放。
