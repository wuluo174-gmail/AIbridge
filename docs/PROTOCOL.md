# Bridge 协议文档 / Protocol Specification

**唯一权威真相源**: `bridge/protocol.py` — 所有事件类型、状态枚举、API 端点、提示词键的机器可读定义。contract tests 以 `protocol.py` 为断言基准，与 `bridge.py` 代码做双向校验。

**本文档是 `protocol.py` 的人类可读说明书**，提供 payload 结构、前端处理逻辑、使用场景等语境信息。当本文档与 `protocol.py` 冲突时，以 `protocol.py` 为准。

所有行号引用基于 bridge.py 当前版本 (commit cdc4613)。

---

## 1. 状态机 (State Machine)

### 1.1 状态枚举 (9 种)

| 状态 | 含义 | UI 标签 |
|------|------|--------|
| `idle` | 空闲，无活动任务 | IDLE |
| `running` | 协商进行中 | NEGOTIATING |
| `consensus` | Codex APPROVED，等待用户确认 | CONSENSUS |
| `max_rounds` | 达到最大轮次，未获 APPROVED | MAX ROUNDS |
| `executing` | Claude 正在执行代码修改 | EXECUTING |
| `review_pending` | Codex 正在审查执行结果 / Claude 正在修复 | REVIEWING |
| `review_fix` | Codex 发现问题，等待用户确认修复 | NEEDS FIX |
| `done` | 任务完成 | DONE |
| `error` | 异常 | ERROR |

### 1.2 状态转换图

```
idle ──POST /api/start──► running
                            │
                  ┌─────────┼─────────────┐
                  │ (is_approved)   (max rounds reached)
                  ▼                        ▼
              consensus              max_rounds
                  │                        │
    ┌─────────────┼──────────┐     ┌───────┼───────┐
    │ POST        │ POST     │     │ POST  │ POST  │
    │ /execute    │/continue │     │/exec  │/cont  │
    ▼             ▼          │     ▼       ▼       │
 executing    running ◄──────┘  executing running ◄┘
    │
    ▼ (自动)
 review_pending
    │
    ├─ "任务收口成功" ──► done
    │
    ▼
 review_fix
    │
    ├─ POST /api/review_skip ──► done
    │
    ▼ POST /api/review_fix
 review_pending ──► (循环，最多 max_review_rounds=3)
    │
    ├─ "任务收口成功" ──► done
    ├─ 达到最大审查轮次 ──► done
    └─ 异常 ──► error

任何阶段:
  POST /api/stop ──► idle
  异常 ──► error
```

### 1.3 状态转换约束

所有状态转换由 `status_lock` (threading.Lock) 保护，采用 CAS (Compare-And-Swap) 模式：

| 转换 | 前置状态 | 触发方式 | 代码位置 |
|------|---------|---------|---------|
| → executing | consensus \| max_rounds | POST /api/execute | L1119-1122 |
| → review_pending | review_fix | POST /api/review_fix | L1146-1148 |
| consensus → running | consensus | POST /api/continue (必须携带 reason) | L1196-1211 |
| max_rounds → running | max_rounds | POST /api/continue | L1202-1208 |

---

## 2. 事件协议 (Event Protocol)

### 2.1 事件结构

每个事件由 `add_event()` (L115-120) 生成：

```json
{
  "id": 0,          // 会话内自增序号
  "type": "...",     // 事件类型（见下表）
  "data": { ... },   // 事件 payload
  "ts": "2024-..."   // ISO 8601 时间戳
}
```

`add_history_event()` (L123-130) 是 `add_event()` 的增强版，原子地同时追加到 history 列表和事件流。

### 2.2 完整事件类型清单 (20 种)

#### 协商阶段 (14 种)

| # | 事件类型 | payload 结构 | 发出位置 | 前端处理位置 | 说明 |
|---|---------|-------------|---------|-------------|------|
| 1 | `status_change` | `{status: string, msg: string}` | L640,646,761,809,857,1138,1224 | L1826 | stopped 时显示中止提示 |
| 2 | `round_start` | `{round: int, max: int}` | L650 | L1719 | 双面板显示轮次分隔线 |
| 3 | `agent_thinking` | `{agent: "claude"\|"codex", round: int}` | L653,682,811,864,884 | L1724 | 切到对应 agent tab + 显示提示 |
| 4 | `cli_start` | `{agent: "claude"\|"codex", round: int}` | L220,357 | **未处理** (落入 switch default) | CLI 进程启动通知 |
| 5 | `agent_chunk` | `{agent: string, text: string}` 或 `{agent: string, text: string, chunk_type: "command"\|"command_output"}` | L194,277,285,388,399,404,410 | L1729 | 按 chunk_type 分 text/command/command_output 渲染 |
| 6 | `chunk_boundary` | `{agent: string, boundary_type: string}` | L411 | L1742 | 关闭当前 collapsible fold (command_output 折叠) |
| 7 | `agent_stderr` | `{agent: string, text: string, is_mcp: bool}` | L192 | L1751 | 仅 is_mcp=true 时显示 (MCP 噪音淡化) |
| 8 | `agent_result` | `{agent: "claude"\|"codex", text: string}` | L331,427 | L1756 | **前端 case 存在但为空 break** (结果通过 agent_response 展示) |
| 9 | `agent_response` | `{round: int, role: "claude"\|"codex"\|"user", phase: string, content: string}` | L676,700,1185,1218 (via add_history_event) | L1758 | 版本历史更新 + 方案预览 + tab 切换；user 角色时双面板显示 |
| 10 | `consensus_reached` | `{round: int, msg: string}` | L708 | L1791 | 双面板绿色提示 |
| 11 | `max_rounds_reached` | `{round: int, msg: string}` | L716 | L1795 | 双面板警告提示 |
| 12 | `warning` | `{msg: string}` | L319 | L1813 | Claude 面板警告 (plan 文件不相关时触发) |
| 13 | `rollback` | `{round: int, max: int, plan: string, msg: string}` | L746 | L1816 | 裁剪版本历史至指定轮次 + 双面板警告 |
| 14 | `error` | `{msg: string}` | L756,800,841,912 | L1809 | 双面板红色错误 |

#### 执行阶段 (1 种)

| # | 事件类型 | payload 结构 | 发出位置 | 前端处理位置 | 说明 |
|---|---------|-------------|---------|-------------|------|
| 15 | `execution_done` | `{result: string}` | L786 | L1799 | 执行完成标记 + 执行结果展示 + "✓ 执行完毕" badge |

#### 审查循环阶段 (5 种)

| # | 事件类型 | payload 结构 | 发出位置 | 前端处理位置 | 说明 |
|---|---------|-------------|---------|-------------|------|
| 16 | `review_start` | `{round: int, max: int}` | L810 | L1846 | 审查开始分隔线 |
| 17 | `review_round_start` | `{round: int, max: int}` | L858 | L1850 | 审查修复轮分隔线 |
| 18 | `review_response` | `{round: int, role: "claude"\|"codex", phase: string, content: string}` | L826,877,897 (via add_history_event) | L1833 | 审查意见/修复总结 details 折叠 |
| 19 | `review_needs_fix` | `{round: int, msg: string, review: string}` | L835,906 | L1854 | 双面板警告 |
| 20 | `review_done` | `{round: int, msg: string, success: bool}` | L831,851,902,1161 | L1858 | 成功绿色/失败警告 + badge 更新 |

---

## 3. HTTP API 契约

### 3.1 GET 端点 (9 个)

#### `GET /`
- **返回**: text/html — 优先返回 `frontend/dist/index.html` (Svelte 5 构建产物)；无 dist 时降级返回内嵌 HTML_UI 冻结快照
- **位置**: server.py do_GET L112-122

#### `GET /api/events?sid={sid}&since={cursor}`
- **返回**: `{events: Event[], next: int}`
- **说明**: 从 cursor 位置开始返回新事件，next 为下次轮询的 cursor 值
- **位置**: L954-961

#### `GET /api/state?sid={sid}`
- **返回 (有会话)**:
  ```json
  {
    "status": "running",
    "round": 1,
    "max_rounds": 5,
    "consensus": false,
    "consensus_round": 0,
    "history_len": 2,
    "error": null
  }
  ```
- **返回 (无会话/无 sid)**:
  ```json
  {
    "status": "idle",
    "round": 0,
    "max_rounds": 5,
    "consensus": false,
    "consensus_round": 0,
    "history_len": 0,
    "error": null
  }
  ```
- **位置**: L962-976

#### `GET /api/sessions`
- **返回**: `{sessions: [{session_id, task, project_path, status, round, max_rounds}]}`
- **位置**: L977-987

#### `GET /api/history?sid={sid}`
- **返回**:
  ```json
  {
    "entries": [{"round": 1, "role": "claude", "phase": "方案", "content": "..."}],
    "execution_result": "..." | null,
    "review_entries": [...],
    "review_round": 0,
    "review_status": null | {"round": 1, "status": "review_fix"},
    "event_cursor": 42
  }
  ```
- **位置**: L988-1020

#### `GET /api/browse?path={path}`
- **返回**: `{current: string, parent: string|null, dirs: [{name, path, is_git}], is_git: bool, truncated: bool}`
- **说明**: 最多返回 200 个目录条目，隐藏以 `.` 开头的目录
- **位置**: L1021-1050

#### `GET /api/complete?prefix={prefix}`
- **返回**: `{suggestions: [{name, path, is_git}]}`
- **说明**: 最多 15 条建议
- **位置**: L1051-1079

#### `GET /api/recent_paths`
- **返回**: `{paths: string[]}`
- **说明**: 最多 10 条
- **位置**: L1080-1081

#### `GET /api/prompts`
- **返回**: 包含 11 个键的 JSON 对象 (见下文提示词配置键清单)
- **位置**: L1082-1083

### 3.2 POST 端点 (8 个)

#### `POST /api/start`
- **请求**: `{task: string, project_path: string, max_rounds?: int}`
- **返回**: `{ok: true, session_id: string}`
- **验证**: task 和 project_path 不能为空，project_path 必须是有效目录
- **副作用**: 创建 SessionState，启动 negotiation 线程，更新 recent_paths
- **位置**: L1089-1112

#### `POST /api/execute`
- **请求**: `{session_id: string}`
- **返回**: `{ok: true}`
- **前置条件**: status ∈ {consensus, max_rounds}
- **副作用**: CAS 切换到 executing，启动 execution 线程
- **位置**: L1113-1124

#### `POST /api/stop`
- **请求**: `{session_id: string}`
- **返回**: `{ok: true}`
- **副作用**: 设 stop_flag，kill active_proc，状态切到 idle
- **位置**: L1125-1139

#### `POST /api/review_fix`
- **请求**: `{session_id: string}`
- **返回**: `{ok: true}`
- **前置条件**: status == review_fix
- **副作用**: CAS 切到 review_pending，启动修复循环线程
- **位置**: L1140-1151

#### `POST /api/review_skip`
- **请求**: `{session_id: string}`
- **返回**: `{ok: true}`
- **前置条件**: status == review_fix
- **副作用**: 状态切到 done
- **位置**: L1152-1162

#### `POST /api/prompts`
- **请求**: `{...prompt_fields}`
- **返回**: `{ok: true}`
- **副作用**: 更新全局 prompt_config + 写入 prompts.json
- **位置**: L1163-1168

#### `POST /api/inject`
- **请求**: `{session_id: string, message: string}`
- **返回**: `{ok: true}`
- **前置条件**: status != consensus, message 不为空
- **副作用**: 注入 user 条目到 history
- **位置**: L1169-1186

#### `POST /api/continue`
- **请求**: `{session_id: string, extra_rounds: int, message?: string}`
- **返回**: `{ok: true}`
- **前置条件**: status ∈ {consensus, max_rounds}; consensus 时 message 必填
- **验证**: extra_rounds ∈ [1, 20]; consensus → running 必须 reason 非空; CAS 防并发
- **副作用**: 重置 consensus 标记 (如适用)，启动 negotiation 续接线程
- **位置**: L1187-1230

### 3.3 OPTIONS

- `OPTIONS *` → 204, CORS headers (L1234-1239)

---

## 4. 提示词配置键清单 (11 个)

从 prompts.json 和前端 cfgKeys (L1910) 逐项确认：

| # | 键名 | 用途 | 使用位置 | 模板变量 |
|---|------|------|---------|---------|
| 1 | `claude_first` | Claude 首轮方案提示 | L452 build_claude_first_prompt | `{task}` |
| 2 | `claude_revise` | Claude 修订提示 | L474 build_claude_revise_prompt | `{codex_feedback}`, `{inject_section}` |
| 3 | `codex_first` | Codex 首轮审查提示 | L480 build_codex_first_prompt | `{task}`, `{claude_plan}` |
| 4 | `codex_review` | Codex 后续审查提示 | L491 build_codex_review_prompt | `{claude_revision}`, `{inject_section}` |
| 5 | `execution` | 执行提示 (APPROVED) | L502 build_execution_prompt | `{task}`, `{plan_section}` |
| 6 | `execution_unapproved` | 执行提示 (未 APPROVED) | L506 build_execution_prompt | `{task}`, `{plan_section}` |
| 7 | `codex_post_review` | 执行后 Codex 审查提示 | L592 build_codex_post_review_prompt | `{task}`, `{approved_plan}`, `{execution_result}`, `{diff_section}` |
| 8 | `claude_post_fix` | Claude 修复提示 | L598 build_claude_post_fix_prompt | `{review_feedback}` |
| 9 | `codex_post_review_followup` | Codex 再评审提示 | L605 build_codex_post_review_followup_prompt | `{fix_result}`, `{diff_section}` |
| 10 | `user_inject_label_claude` | 用户注入标签 (Claude 侧) | L472 build_claude_revise_prompt | — (直接作为 section title) |
| 11 | `user_inject_label_codex` | 用户注入标签 (Codex 侧) | L489 build_codex_review_prompt | — (直接作为 section title) |

---

## 5. 前端轮询协议

前端以 300ms 间隔轮询 (L1955 附近)：

```
每 300ms:
  GET /api/events?sid={sid}&since={cursor}  → 处理新事件, 更新 cursor
  GET /api/state?sid={sid}                  → 更新状态 pill + 按钮可用性
```

页面刷新恢复流程：
1. URL 中 `?sid=xxx` 保留会话 ID
2. `GET /api/state?sid=xxx` 恢复状态
3. `GET /api/history?sid=xxx` 恢复版本历史 + 审查历史
4. 从 `event_cursor` 开始轮询增量事件
