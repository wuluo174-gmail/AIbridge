# Bridge v4

Bridge 是一个面向 AI CLI 编排的四角色工作台。

它不再把系统建模成“两个面板 + 底部注入栏”，而是明确建模为同一套统一账本上的六类核心对象：

- `session`
- `role lane`
- `event`
- `artifact`
- `intervention`
- `view projection`

在这个模型上，Bridge 提供两种会话级视图：

- `terminal`：给专业用户的四角色终端工作台
- `scene`：给非专业用户的场景化时间线视图

两种模式共享一套底层事实流，不允许各自维护独立真相。
同一会话可以在运行中切换 `scene / terminal`，切换不会生成新会话，也不会复制数据。
页面刷新时会先从 ledger snapshot 恢复，再按 `stream_cursor` 追增量事件，不再从头重放整条 SSE 流。

## 当前版本的核心变化

- 固定四个一等逻辑角色：`planner`、`reviewer`、`executor`、`validator`
- 同一工具可以承担多个角色，也支持单工具包办四角
- 旧的 `session_history` / `review_history` / `session_events` 已废弃
- 旧的 `/api/inject` 和二角色 `role_config` 协议已废弃
- `Process` 只读 `role_events`
- `Result` 只读 `artifacts`
- 用户输入不再混进 history，而是作为独立的 `intervention`
- 实时链路从轮询转为 `SSE /api/stream`

这是一轮开发期破坏式重构。项目当前没有历史用户数据，因此不保留兼容层。

## 架构概览

```text
User Input
   |
   v
POST /api/input
   |
   v
Python Backend (truth owner)
   |
   +--> Workflow Engine
   |      planner -> reviewer -> executor -> validator
   |
   +--> Lane Runtime (one per role)
   |      stdout/stderr/result -> role_events
   |
   +--> Artifact Publisher
   |      plan/review/execution_summary/validation_report
   |
   +--> Intervention Ledger
   |      queued/acknowledged/consumed/cancelled/rejected
   |
   +--> SQLite
          sessions / workflow_roles / role_lanes / role_events / artifacts / interventions
```

## 快速开始

前提：

- `python3`
- `claude` CLI
- `codex` CLI

启动：

```bash
python3 bridge.py
```

或：

```bash
python3 bridge.py --port 9090
```

前端开发：

```bash
cd frontend
npm install
npm run dev
```

生产前端构建：

```bash
cd frontend
npm run build
```

## 会话工作流

标准模板固定为四阶段：

1. `planner` 产出 `plan`
2. `reviewer` 产出 `review`
3. `executor` 执行并产出 `execution_summary`
4. `validator` 校验并产出 `validation_report`

如果校验不通过，系统进入 `repairing`，由 `executor -> validator` 继续闭环，直到：

- 收口成功，进入 `done`
- 达到修复轮次上限，进入 `review_max_rounds`

## 视图模式

### Terminal

- 默认四角色 `2x2` 工作区
- 每个角色都有自己的终端表面
- 输入直接发到统一 `/api/input`
- slash command 由 Bridge 自己解释，不直接透传到底层 CLI 原始 stdin

支持的控制命令包括：

- `/pause`
- `/stop`
- `/exec`
- `/continue <reason>`
- `/fix`
- `/skip`
- `/review-continue <n>`

### Scene

- 把 `role_events + artifacts + interventions` 投影成场景时间线
- 底部保留友好输入框
- 但它只是同一套 `intervention` 账本的另一种输入壳

## 数据模型

### `sessions`

只描述会话整体状态，不直接装角色结果。

### `workflow_roles`

角色绑定表，显式声明每个逻辑角色用哪个工具。

### `role_lanes`

角色运行通道，持有 lane 状态、transport、last seq。

### `role_events`

唯一过程真相，终端模式和场景模式都从这里投影。

### `artifacts`

结构化结果真相。前端 `Result` 区只读这里。

### `interventions`

用户高层输入真相，生命周期明确，不再依赖 history 尾部连续 user 条目。

## API 入口

关键接口：

- `GET /api/tools`
- `GET /api/workflow_config`
- `GET /api/session/state`
- `GET /api/history`
- `GET /api/sessions`
- `GET /api/stream`
- `POST /api/workflow_config`
- `POST /api/session/start`
- `POST /api/session/pause`
- `POST /api/session/resume`
- `POST /api/session/stop`
- `POST /api/session/exec`
- `POST /api/session/continue`
- `POST /api/session/review_fix`
- `POST /api/session/review_skip`
- `POST /api/session/review_continue`
- `POST /api/input`
- `POST /api/terminal/resize`

详细协议见 [docs/PROTOCOL.md](/Users/809456948qq.com/code/bridge/docs/PROTOCOL.md)。

## 文档

- [docs/ARCHITECTURE.md](/Users/809456948qq.com/code/bridge/docs/ARCHITECTURE.md)
- [docs/PROTOCOL.md](/Users/809456948qq.com/code/bridge/docs/PROTOCOL.md)
- [docs/REQUIREMENTS.md](/Users/809456948qq.com/code/bridge/docs/REQUIREMENTS.md)

## 验证

后端：

```bash
pytest -q
```

前端：

```bash
cd frontend
npm run check
npm run test
npm run build
```
