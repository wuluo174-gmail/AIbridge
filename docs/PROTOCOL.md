# Bridge 协议文档 / Protocol Specification

**唯一权威真相源**: [bridge/protocol.py](/Users/809456948qq.com/code/bridge/bridge/protocol.py)

本文档只做人类可读说明。当本文档与 `bridge/protocol.py` 冲突时，以代码为准。

---

## 1. 状态机

### 1.1 状态枚举

| 状态 | 含义 |
|------|------|
| `idle` | 当前标签页没有绑定会话 |
| `running` | 协商进行中 |
| `consensus` | 达成共识，等待执行或继续协商 |
| `max_rounds` | 达到协商轮次上限，等待执行或继续协商 |
| `executing` | 执行阶段 |
| `review_pending` | 执行后审查 / 修复后复审进行中 |
| `review_fix` | 需要用户确认是否修复 |
| `review_max_rounds` | 审查修复达到上限，等待继续或跳过 |
| `paused` | 用户主动中断，可恢复 |
| `interrupted` | 进程/后端异常中断，可恢复 |
| `aborted` | 用户主动中止，不可恢复 |
| `done` | 会话完成 |
| `error` | 会话异常终止 |

### 1.2 状态分组

- `EXECUTABLE_STATES`: `consensus`, `max_rounds`
- `FIXABLE_STATES`: `review_fix`
- `CONTINUABLE_STATES`: `consensus`, `max_rounds`
- `REVIEW_CONTINUABLE_STATES`: `review_max_rounds`
- `REVIEW_SKIPPABLE_STATES`: `review_fix`, `review_max_rounds`
- `RESUMABLE_STATES`: `paused`, `interrupted`
- `TERMINAL_STATES`: `idle`, `aborted`, `done`, `error`

### 1.3 生命周期原则

- 会话从 `POST /api/start` 成功时立即入统一账本。
- `paused` / `interrupted` 不是终态，而是可恢复检查点。
- `aborted` 是不可恢复终态。
- 后端重启时，数据库里仍处于活动态的会话会被统一标记为 `interrupted`。

---

## 2. 事件协议

### 2.1 事件结构

```json
{
  "id": 0,
  "type": "round_start",
  "data": {},
  "ts": "2026-03-22T12:00:00"
}
```

### 2.2 事件类型

#### 协商阶段

- `status_change`
- `round_start`
- `agent_thinking`
- `cli_start`
- `agent_chunk`
- `chunk_boundary`
- `agent_stderr`
- `agent_result`
- `agent_response`
- `consensus_reached`
- `max_rounds_reached`
- `warning`
- `rollback`
- `error`

#### 执行阶段

- `execution_done`

#### 审查阶段

- `review_start`
- `review_round_start`
- `review_response`
- `review_needs_fix`
- `review_done`
- `review_max_rounds_reached`

### 2.3 事件语义

- `agent_response` / `review_response` 是历史与版本视图的核心事件。
- `status_change` 只表达状态切换，不承担完整历史回放职责。
- 前端日志是否自动滚到底部，不从事件条数推导，而由视图态 `followTail` 决定。

---

## 3. HTTP API

### 3.1 GET 端点

- `GET /`
  返回前端页面；优先伺服 `frontend/dist`，否则返回构建引导页。

- `GET /api/events?sid={sid}&since={cursor}`
  返回 `{events, next}`。

- `GET /api/state?sid={sid}`
  返回单个会话快照：
  - `status`
  - `round`
  - `max_rounds`
  - `consensus`
  - `consensus_round`
  - `history_len`
  - `error`
  - `planner_tool_id`
  - `reviewer_tool_id`
  - `executor_panel`
  - `review_round`
  - `max_review_rounds`
  - `phase`
  - `updated_at`
  - `finished_at`
  - `interrupt_reason`
  - `resume_available`

- `GET /api/sessions?limit={n}&offset={n}`
  返回统一会话索引 `{sessions}`，每项包含：
  - `session_id`
  - `task`
  - `project_path`
  - `status`
  - `phase`
  - `round`
  - `max_rounds`
  - `updated_at`
  - `finished_at`
  - `interrupt_reason`
  - `resume_available`
  - `planner_tool_id`
  - `reviewer_tool_id`
  - `consensus`
  - `consensus_round`
  - `created_at`

- `GET /api/history?sid={sid}`
  返回统一历史：
  - `entries`
  - `execution_result`
  - `review_entries`
  - `review_round`
  - `review_status`
  - `event_cursor`

- `GET /api/browse?path={path}`
- `GET /api/complete?prefix={prefix}`
- `GET /api/recent_paths`
- `GET /api/prompts`
- `GET /api/tools`
- `GET /api/role_config`

### 3.2 POST 端点

- `POST /api/start`
  创建会话并立即入账。

- `POST /api/execute`
  从 `consensus` / `max_rounds` 进入执行。

- `POST /api/pause`
  将活动会话切到 `paused`，保留恢复能力。

- `POST /api/resume`
  从 `paused` / `interrupted` 恢复。

- `POST /api/stop`
  将会话切到 `aborted`，不可恢复。

- `POST /api/review_fix`
- `POST /api/review_skip`
- `POST /api/review_continue`
- `POST /api/prompts`
- `POST /api/inject`
- `POST /api/continue`
- `POST /api/role_config`

### 3.3 已移除的旧接口

以下接口已不再作为公共协议的一部分：

- `GET /api/archived_sessions`
- `GET /api/archived_session_history`

原因：项目改为统一会话账本模型，不再区分“活动会话”和“归档会话”两套数据真相。

---

## 4. 角色与能力

- Planner / Reviewer 允许是同一工具。
- 角色配置校验基于能力矩阵，不基于工具名互斥。
- 至少需要一个具备执行能力的工具组合。
- 执行者由能力解析自动决定，不强绑某个角色名。

---

## 5. 持久化边界

### 5.1 SQLite 中持久化

- `sessions`
- `session_events`
- `session_history`
- `review_history`
- `cli_tools`
- `role_assignments`
- `prompt_templates`
- `recent_paths`
- `_meta`

### 5.2 不持久化的纯内存态

- 线程锁
- `stop_flag`
- `active_proc`
- `active_pgid`

这些字段在重启后不恢复原对象，只根据账本重建可继续会话的逻辑状态。

---

## 6. 设计约束

- 不再维护 archive-only 模型。
- 不再维护双状态真相源（例如 `status` + `final_status`）。
- 不再从日志条目数量推导用户滚动意图。
- 前端所有恢复和历史查看都建立在统一账本之上，而不是 UI 特判。
