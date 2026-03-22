# Bridge 移动端设计

移动端不是桌面端 UI 的简单缩放版，而是统一账本的远程视图与控制端。

## 1. 基本边界

- CLI 只在桌面端运行
- 移动端不运行 `claude` / `codex`
- 移动端只查看会话、发送输入、触发控制动作
- 桌面端后端仍然是唯一业务真相源

## 2. 与桌面端共享的核心模型

移动端和桌面端必须共享同一套协议对象：

- `session`
- `workflow role`
- `role lane`
- `role event`
- `artifact`
- `intervention`

移动端绝不能引入一套新的“移动端专属会话模型”。

## 3. 同步方式

建议：

- 查询：复用 HTTP API
- 实时更新：复用事件流语义；如果未来需要双向长连接，可以用 WebSocket 包裹相同 payload

换句话说，移动端扩展的是传输层，不是业务协议。

## 4. 远程动作映射

移动端常用操作应映射到现有 v4 API：

| action | 桌面端接口 | payload |
|--------|------------|---------|
| `get_tools` | `GET /api/tools` | `{}` |
| `get_workflow_config` | `GET /api/workflow_config` | `{sid?}` |
| `start_session` | `POST /api/session/start` | `{task, project_path, view_mode, max_rounds, max_review_rounds, roles}` |
| `get_history` | `GET /api/history` | `{sid}` |
| `get_state` | `GET /api/session/state` | `{sid}` |
| `stream` | `GET /api/stream` | `{sid, since}` |
| `pause` | `POST /api/session/pause` | `{session_id}` |
| `resume` | `POST /api/session/resume` | `{session_id}` |
| `stop` | `POST /api/session/stop` | `{session_id}` |
| `exec` | `POST /api/session/exec` | `{session_id}` |
| `continue` | `POST /api/session/continue` | `{session_id, extra_rounds, message}` |
| `review_fix` | `POST /api/session/review_fix` | `{session_id}` |
| `review_skip` | `POST /api/session/review_skip` | `{session_id}` |
| `review_continue` | `POST /api/session/review_continue` | `{session_id, extra_rounds}` |
| `input` | `POST /api/input` | `{session_id, origin_view, role_key?, text}` |

## 5. 移动端视图建议

移动端建议只提供 `scene` 风格投影：

- 时间线卡片
- 最新状态摘要
- 角色切换查看
- 控制按钮
- 输入框

不建议在移动端完整复刻桌面 `terminal 2x2` 工作区。

## 6. 断线与恢复

断线后移动端应做的不是缓存一套真相，而是：

1. 优先用 `GET /api/history` 恢复统一账本快照：
   - `session`
   - `roles`
   - `artifacts`
   - `interventions`
   - `projections`
   - `stream_cursor`
2. 再用 `GET /api/stream?since=stream_cursor` 只追增量事件
3. `GET /api/session/state` 只适合拿轻量摘要，不应作为恢复主链路

## 7. 后续扩展原则

如果后续真的引入：

- WebSocket
- 配对
- 设备认证
- 局域网发现

也必须保持下面这条不变：

传输层可以升级，底层账本和业务协议不能分叉。
