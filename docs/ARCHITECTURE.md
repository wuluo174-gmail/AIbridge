# Bridge 架构设计

## 1. 架构原则

### 1.1 Python 后端是唯一业务真相源

- 编排在 Python
- 持久化在 Python
- 角色运行态在 Python
- CLI 输出归一化在 Python
- 前端只持有视图态，不持有业务真相

### 1.2 先改数据结构，再改视图

Bridge v4 的核心不是“换一个布局”，而是纠正核心对象定义：

- 旧模型：`logs / versions / executionResult / injectValue`
- 新模型：`session / role lane / event / artifact / intervention / projection`

只有先把账本改对，终端模式和场景模式才能自然长出来。

### 1.3 Process 与 Result 必须分源

- `Process` 只读 `role_events`
- `Result` 只读 `artifacts`
- 前端实时更新依赖 `history snapshot + SSE 增量事件`，而不是定时轮询后端状态接口
- 终端/场景投影语义由后端生成并随 `history + SSE` 下发，前端只做渲染与增量合并
- projection 不落为 ledger 真相；账本持久化原始事件事实，projection 在读取和实时流阶段生成

不能再靠日志反推结果，也不能再靠 history 拼装过程。

## 2. 分层设计

```text
┌────────────────────────────────────────────────────┐
│ Frontend View                                      │
│ terminal mode / scene mode                         │
└──────────────────────▲─────────────────────────────┘
                       │
┌──────────────────────┴─────────────────────────────┐
│ Projection Layer                                   │
│ project_terminal() / project_scene() / history     │
└──────────────────────▲─────────────────────────────┘
                       │
┌──────────────────────┴─────────────────────────────┐
│ Workflow Engine                                    │
│ planning -> reviewing -> executing -> validating   │
│                            -> repairing -> done    │
└──────────────────────▲─────────────────────────────┘
                       │
┌──────────────────────┴─────────────────────────────┐
│ Lane Runtime                                       │
│ one runtime per role lane                          │
│ adapter stdout/stderr/result -> role_events        │
│ result extraction -> artifacts                     │
└──────────────────────▲─────────────────────────────┘
                       │
┌──────────────────────┴─────────────────────────────┐
│ SQLite Ledger                                      │
│ sessions / workflow_roles / role_lanes /           │
│ role_events / artifacts / interventions            │
└────────────────────────────────────────────────────┘
```

## 3. 统一工作流模板

当前只实现一个固定模板：

1. `planner` 产出方案
2. `reviewer` 审查方案
3. 达成共识后 `executor` 执行
4. `validator` 校验是否收口
5. 如果未收口，则进入 `executor -> validator` 修复闭环

为什么固定四角色而不是任意 N 角色：

- 当前目标是消除返工，不是做抽象玩具
- 四角色已经覆盖双人对抗、多人分工、单工具全包
- 数据结构仍然是归一化的，将来真要扩展时不需要再改 schema

## 4. 核心实体

## 4.1 Session

负责：

- 项目路径
- 用户任务
- 会话状态
- 当前阶段
- 当前轮次
- view mode
- interrupt / error 信息

不负责：

- 存放某个角色的最终输出
- 存放过程日志

## 4.2 WorkflowRole

角色到工具的绑定关系：

- `role_key`
- `tool_id`
- `enabled`
- `sort_order`

它是配置对象，不是运行态。

## 4.3 RoleLane

角色运行通道：

- `lane_id`
- `lane_status`
- `transport_kind`
- `last_seq`
- `resume_state`

`RoleLane` 是终端模式成立的基础，因为“终端”并不是某个 UI 组件，而是角色级运行通道的投影。

lane 还拥有自己的 viewport 元数据：

- `width_px`
- `height_px`
- `cols`
- `rows`

## 4.4 RoleEvent

不可逆事实流，负责记录：

- thinking started
- CLI started
- stdout/stderr chunk
- command started
- command output
- result emitted
- intervention consumed
- session stage / status 变化

## 4.5 Artifact

结构化产物，负责成为下游输入和历史视图的稳定依赖：

- `plan`
- `review`
- `execution_summary`
- `validation_report`
- `consensus_snapshot`

## 4.6 Intervention

用户输入是一等对象，不再作为 `history.role=user` 的补丁。

它有明确生命周期：

- `queued`
- `acknowledged`
- `consumed`
- `cancelled`
- `rejected`

## 5. 运行时数据流

### 5.1 协商阶段

```text
task
  -> planner prompt
  -> planner lane runtime
  -> role_events(stdout/stderr/result)
  -> artifact(plan)
  -> reviewer prompt
  -> reviewer lane runtime
  -> artifact(review)
  -> consensus / max_rounds
```

### 5.2 执行阶段

```text
artifact(plan)
  -> executor prompt
  -> executor lane runtime
  -> role_events
  -> artifact(execution_summary)
```

### 5.3 校验阶段

```text
artifact(execution_summary) + git diff
  -> validator prompt
  -> validator lane runtime
  -> artifact(validation_report)
  -> done or repairing
```

### 5.4 用户输入流

```text
terminal text / scene text / slash command
  -> POST /api/input
  -> intervention ledger or control action
  -> workflow engine chooses when to consume
  -> consumed_by_roles recorded
```

## 6. 双模式投影

## 6.1 Terminal 模式

面向专业用户。

特点：

- 四角色终端工作区
- 桌面默认 `2x2`
- 小屏退化为 tabs
- 每个角色独立输入
- slash command 由 Bridge 命令解释器接管

关键边界：

- 不是通用 IDE
- 不是把用户键盘直连到底层 CLI 原始 stdin

## 6.2 Scene 模式

面向非专业用户。

特点：

- 同一底层事实流投影为时间线
- 更像“几个 agent/真人在协商”
- 底部友好输入框只是统一 `/api/input` 的另一个壳

## 7. 持久化架构

统一账本如下：

- `sessions`
- `workflow_roles`
- `role_lanes`
- `role_events`
- `artifacts`
- `interventions`

持久化策略：

- `SessionState` 每次状态变化后持久化
- 事件、artifact、intervention 都以 append 为主
- 进程对象和锁不持久化
- 重启时活动会话统一标记为 `interrupted`

## 8. 恢复机制

恢复只依赖统一账本，不依赖前端补推导：

1. 读 `sessions`
2. 读 `workflow_roles`
3. 读 `role_lanes`
4. 读 `role_events`
5. 读 `artifacts`
6. 读 `interventions`
7. 重建 `SessionState`

前端刷新时只调用：

- `GET /api/session/state`
- `GET /api/history`
- `GET /api/stream`

其中：

- `GET /api/history` 直接返回统一账本中的 `events / artifacts / interventions`
- 前端先用账本恢复，再用 `stream_cursor` 补增量流
- 不再通过“从 0 开始重放所有 SSE”来重建过程区

## 9. 与旧架构的决裂点

以下设计已经被显式废弃：

- `planner_tool_id / reviewer_tool_id` 二元角色模型
- `_resolve_execution_roles()` 推导执行角色
- `session_history / review_history / session_events`
- `execution_result` 挂在 session 上
- `/api/inject`
- `Process / Result` 共享同一份非结构化数据

## 10. 非目标

本轮不做：

- 任意 N 角色可视化编排图
- 文件树
- 代码编辑器
- 真 PTY 透传
- 旧协议兼容层

Bridge 仍然是编排器，不是通用 IDE。
