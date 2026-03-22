# Bridge 开发指南

本指南描述 Bridge v4 的开发顺序与约束。旧版本中围绕 `session_history`、`review_history`、`/api/inject`、二角色 `role_config` 的迁移步骤已经失效，不应再作为开发依据。

## 1. 开发顺序

扩展或修改 Bridge 时，默认按下面顺序推进：

1. 先改 `bridge/protocol.py`
2. 再改 `bridge/workflow.py` 和持久化 schema
3. 再改 `bridge/session.py` / `bridge/persistence/store.py`
4. 再改 `bridge/orchestration/engine.py`
5. 最后改 `bridge/server.py` 和前端投影

不要反过来从 UI 倒推后端字段。

## 2. 改动落点

### 2.1 新增会话级状态

改这里：

- `bridge/protocol.py`
- `bridge/session.py`
- `bridge/persistence/schema.sql`
- `bridge/persistence/store.py`
- `frontend/src/App.svelte`

### 2.2 新增角色级过程事件

改这里：

- `bridge/protocol.py`
- `bridge/adapters/base.py`
- 对应具体 adapter
- `frontend/src/App.svelte`

### 2.3 新增结构化产物

改这里：

- `bridge/protocol.py`
- `bridge/orchestration/engine.py`
- `bridge/session.py`
- `frontend/src/App.svelte`

### 2.4 新增用户输入规则

改这里：

- `bridge.py` 里的 `handle_input`
- `bridge/session.py` 里的 `add_intervention` / `consume_interventions`
- 前端输入组件

## 3. 不允许再引入的旧模式

- 不要新增 `history.role=user` 式输入补丁
- 不要新增 `planner_tool_id / reviewer_tool_id` 式二元配置
- 不要新增从日志反推结果的逻辑
- 不要为旧接口增加兼容层
- 不要让 `terminal` 和 `scene` 各自持有不同真相源

## 4. 新功能开发检查单

提交前至少确认：

- [ ] `pytest -q` 通过
- [ ] `python3 -m py_compile bridge.py bridge/*.py bridge/*/*.py` 通过
- [ ] `cd frontend && npm run check` 通过
- [ ] `cd frontend && npm run test` 通过
- [ ] `cd frontend && npm run build` 通过
- [ ] 如果改了 API，`docs/PROTOCOL.md` 已同步
- [ ] 如果改了工作流或产品边界，`docs/ARCHITECTURE.md` / `docs/REQUIREMENTS.md` 已同步

## 5. 烟测建议

最小烟测至少覆盖：

1. 启动服务
2. `GET /api/tools`
3. `GET /api/workflow_config`
4. `POST /api/session/start`
5. `GET /api/history?sid=...`
6. `GET /api/stream?sid=...&since=0`
7. `POST /api/input`

如果这些链路都通，说明“配置 -> 会话 -> 事件 -> 恢复 -> 输入”主干是成立的。
