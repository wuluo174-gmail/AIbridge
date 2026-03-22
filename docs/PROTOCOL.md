# Bridge 协议文档

唯一权威真相源是 [bridge/protocol.py](/Users/809456948qq.com/code/bridge/bridge/protocol.py)。

本文档解释协议语义、对象关系与接口结构；如果和代码冲突，以代码为准。

## 1. 核心对象

Bridge v4 只承认以下六类一等实体：

1. `session`
2. `workflow role`
3. `role lane`
4. `role event`
5. `artifact`
6. `intervention`

其中：

- `session` 负责整体生命周期
- `workflow role` 负责角色到工具的绑定
- `role lane` 负责角色级运行通道
- `role event` 是唯一过程真相
- `artifact` 是唯一结构化结果真相
- `intervention` 是唯一用户输入真相

## 2. 固定角色与模式

### 2.1 固定逻辑角色

- `planner`
- `reviewer`
- `executor`
- `validator`

它们是固定工作流模板中的一等角色，不再由 `planner_tool_id / reviewer_tool_id` 临时推导。

### 2.2 会话级显示模式

- `terminal`
- `scene`

模式是会话级的，不是面板级的。

## 3. 会话状态机

### 3.1 会话状态

- `idle`
- `running`
- `consensus`
- `max_rounds`
- `executing`
- `validating`
- `review_fix`
- `review_max_rounds`
- `repairing`
- `paused`
- `interrupted`
- `aborted`
- `done`
- `error`

### 3.2 活跃阶段

- `planning`
- `reviewing`
- `awaiting_execution`
- `executing`
- `validating`
- `repairing`
- `done`

### 3.3 状态语义

- `running`
  表示协商阶段正在推进
- `consensus`
  表示协商达成共识，等待执行或带理由继续协商
- `max_rounds`
  表示协商达到轮次上限，等待执行或继续协商
- `executing`
  表示执行者正在执行
- `validating`
  表示校验者正在校验
- `review_fix`
  表示校验未收口，等待用户决定是否发起修复
- `review_max_rounds`
  表示修复闭环达到上限
- `repairing`
  表示执行者正在根据校验意见修复
- `paused` / `interrupted`
  均可恢复，但来源不同

## 4. 事件协议

### 4.1 事件结构

```json
{
  "id": 12,
  "type": "lane.stdout_chunk",
  "role_key": "planner",
  "source": "claude-code",
  "data": {
    "text": "..."
  },
  "ts": "2026-03-22T14:53:30.899669"
}
```

字段定义：

- `id`
  会话内单调递增序号，也是 SSE cursor
- `type`
  事件类型
- `role_key`
  事件所属角色；会话级事件可为 `null`
- `source`
  事件来源，通常是 `workflow`、`artifact`、`intervention` 或具体 tool id
- `data`
  事件负载；其中 `data.projection` 为后端生成的 canonical projection delta
- `ts`
  事件时间戳

注意：

- `data.projection` 只用于读取和实时流，不属于持久化 ledger 真相
- SQLite 中保存的是去掉 projection 的原始事件事实
- `/api/stream` 对外发送事件时会重新补齐 `data.projection`，因此恢复后的历史事件与新产生的内存事件对外协议一致

### 4.2 事件类型

- `session.status_changed`
- `session.stage_changed`
- `session.view_mode_changed`
- `lane.status_changed`
- `lane.viewport_changed`
- `lane.thinking_started`
- `lane.cli_started`
- `lane.stdout_chunk`
- `lane.stderr_chunk`
- `lane.command_started`
- `lane.command_output`
- `lane.result_emitted`
- `artifact.published`
- `intervention.received`
- `intervention.consumed`
- `warning.raised`
- `error.raised`

### 4.3 事件边界

- `role_events` 是唯一过程真相
- 会话级事件必须自带 `session` 与 `summary` 快照，前端据此直接更新当前会话和会话列表
- `lane.status_changed` 必须自带 `lane` 快照，前端据此直接更新角色通道状态
- `artifact.published` 与 `intervention.*` 必须自带对应对象快照，前端不得再回拉单独接口补全
- 终端模式与场景模式的增量渲染语义来自 `data.projection`，前端不再自己定义事件到视图的解释规则
- 前端 `Process` 只读事件流或其投影
- 任何“从 artifact 反推过程”或“从日志拼接最终结果”的行为都不属于协议

## 5. Artifact 协议

### 5.1 Artifact 结构

```json
{
  "id": "artifact_id",
  "session_id": "sid",
  "lane_id": "lane_planner_xxx",
  "role_key": "planner",
  "round": 1,
  "phase": "planning",
  "artifact_kind": "plan",
  "content": "markdown output",
  "source_event_seq": 25,
  "created_at": "2026-03-22T15:00:00"
}
```

### 5.2 Artifact 类型

- `plan`
- `review`
- `execution_summary`
- `validation_report`
- `consensus_snapshot`

### 5.3 Artifact 语义

- `Result` 视图只能读 artifact
- 下游角色读取上游结构化输出，也只能读 artifact
- `execution_result` 不再挂在 session 上

## 6. Intervention 协议

### 6.1 Intervention 结构

```json
{
  "id": "intervention_id",
  "session_id": "sid",
  "origin_view": "terminal",
  "origin_role_key": "planner",
  "target_roles": ["planner", "reviewer"],
  "target_scope": "planning",
  "text": "请补上数据回放策略",
  "command": null,
  "status": "queued",
  "consumed_by_roles": {},
  "created_at": "2026-03-22T15:10:00",
  "updated_at": "2026-03-22T15:10:00"
}
```

### 6.2 Intervention 生命周期

- `queued`
- `acknowledged`
- `consumed`
- `cancelled`
- `rejected`

### 6.3 输入规则

- 普通文本输入会创建 `intervention`
- slash command 也会通过统一 `/api/input` 进入系统，但属于控制动作
- `consensus` 状态下禁止普通文本输入，必须使用 `/continue <reason>` 或执行
- `paused` / `interrupted` / 终态会拒绝新的普通文本输入

## 7. HTTP API

## 7.1 GET

- `GET /api/tools`
  返回已注册工具及能力矩阵

- `GET /api/workflow_config`
  返回默认工作流配置；带 `sid` 时返回该会话的工作流配置

- `GET /api/session/state?sid=...`
  返回单个会话快照

- `GET /api/history?sid=...`
  返回会话恢复所需的账本投影：
  - `session`
  - `roles`
  - `events`
  - `artifacts`
  - `interventions`
  - `projections`
  - `lane_cursors`
  - `stream_cursor`
  前端刷新时先用它恢复，再以 `stream_cursor` 继续订阅增量 SSE；不再轮询 `session/state` 来补账

- `GET /api/sessions`
  返回会话索引

- `GET /api/stream?sid=...&since=...`
  SSE 主链路；`since` 为事件 cursor

- `GET /api/browse`
- `GET /api/complete`
- `GET /api/recent_paths`
- `GET /api/prompts`

## 7.2 POST

- `POST /api/workflow_config`
  更新默认工作流配置

- `POST /api/session/start`
  创建会话并立刻进入统一账本

- `POST /api/session/pause`
- `POST /api/session/resume`
- `POST /api/session/stop`
- `POST /api/session/exec`
- `POST /api/session/continue`
- `POST /api/session/review_fix`
- `POST /api/session/review_skip`
- `POST /api/session/review_continue`
- `POST /api/session/view_mode`

- `POST /api/input`
  统一输入入口，处理：
  - terminal 文本
  - scene 文本
  - slash command

- `POST /api/terminal/resize`
  终端视图尺寸同步入口；更新 lane 级 viewport 真相并发布 `lane.viewport_changed`

- `POST /api/prompts`
  更新 prompt 模板

## 7.3 已废弃接口

以下接口已经废弃，不再是协议的一部分：

- `POST /api/inject`
- `GET /api/events`
- `GET /api/state`
- `POST /api/start`
- `POST /api/execute`
- `POST /api/pause`
- `POST /api/resume`
- `POST /api/stop`
- `POST /api/continue`
- `POST /api/role_config`

## 8. 关键响应结构

### 8.1 `GET /api/session/state`

```json
{
  "session_id": "sid",
  "task": "task",
  "project_path": "/path",
  "workflow_template": "standard",
  "view_mode": "scene",
  "status": "running",
  "active_stage": "planning",
  "current_round": 1,
  "current_review_round": 0,
  "consensus_round": 0,
  "max_rounds": 5,
  "max_review_rounds": 3,
  "error": null,
  "interrupt_reason": null,
  "created_at": "...",
  "updated_at": "...",
  "finished_at": null,
  "resume_available": false
}
```

### 8.2 `GET /api/workflow_config`

```json
{
  "view_mode": "scene",
  "workflow_template": "standard",
  "max_rounds": 5,
  "max_review_rounds": 3,
  "roles": [
    {"role_key": "planner", "tool_id": "claude-code", "enabled": true, "sort_order": 0},
    {"role_key": "reviewer", "tool_id": "codex", "enabled": true, "sort_order": 1},
    {"role_key": "executor", "tool_id": "claude-code", "enabled": true, "sort_order": 2},
    {"role_key": "validator", "tool_id": "codex", "enabled": true, "sort_order": 3}
  ]
}
```

## 9. 持久化边界

SQLite 中持久化：

- `sessions`
- `workflow_roles`
- `role_lanes`
- `role_events`
- `artifacts`
- `interventions`
- `cli_tools`
- `prompt_templates`
- `recent_paths`
- `_meta`

不会直接持久化对象引用或进程对象：

- `stop_flag`
- `active_proc`
- `active_pgid`
- 线程锁

后端重启后，运行中的会话会被标记为 `interrupted`，再由账本重建逻辑状态。

当前恢复策略：

- `history` 直接返回已持久化 `events`
- 前端先用账本恢复过程和结果
- 再以 `stream_cursor` 作为 SSE cursor 只追增量事件

刷新恢复不再依赖从 `0` 开始重放全量 SSE。
