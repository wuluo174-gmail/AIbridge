# Bridge 分步开发指南 / Step-by-Step Development Guide

本文档定义了从单文件 bridge.py 迁移到模块化桌面应用的分步计划。每一步设计为可在**单个 CLI 对话中完整完成**。

## 通用规则

1. **每步结束后 `python bridge.py` 必须功能完全不变**
2. **每步结束后运行 `python3 -m unittest discover -s tests -v` 确认 contract tests 通过**
3. 每步结束后验证对应的 NR (No-Regression) checklist 项 (见 REQUIREMENTS.md §4)
4. 不引入外部依赖 (标准库 only)，直到 Step 8 桌面壳选型

---

## Step 1: 协议固化 + 骨架 + Contract Tests ✅ (本次 PR)

### 目标
固化现有行为契约，创建 Python 模块骨架 (接口声明，不接入运行路径)，编写可执行 contract tests。

### 产出
- `docs/` — 5 份文档
- `bridge/` — 13 个骨架文件
- `tests/test_contract.py` — contract tests

### 验证
- `python bridge.py` 正常工作
- `python3 -m unittest tests.test_contract -v` 全部通过
- `python -c "import bridge; import bridge.protocol; import bridge.adapters.base"` 无报错

---

## Step 2: Python 拆分 — Session + Event + Protocol

### 目标
将 SessionState 类、add_event、add_history_event 从 bridge.py 迁入 bridge/session.py。bridge.py 改为 `from bridge.session import ...`。

### 前置条件
- Step 1 完成

### 具体文件改动
1. **bridge/session.py**: 填入完整 SessionState 类代码 (从 bridge.py L64-131)，add_event (L115-120), add_history_event (L123-130), get_session (L110-112), sessions dict, sessions_lock
2. **bridge/protocol.py**: 填入事件类型常量和状态枚举
3. **bridge.py**: 删除 SessionState 类等已迁出代码，改为 `from bridge.session import SessionState, add_event, add_history_event, get_session, sessions, sessions_lock`

### 不回归验证
- NR-1 (状态机), NR-2 (多会话隔离), NR-11 (事件协议)
- `python3 -m unittest discover -s tests -v`
- `python bridge.py` 完整功能

### 提示词 (新开 CLI 对话使用)

```
请阅读以下文件，理解项目结构和迁移计划：
- docs/ARCHITECTURE.md
- docs/PROTOCOL.md
- docs/REQUIREMENTS.md (§4 不回归 checklist)
- bridge/session.py (当前骨架)
- bridge/protocol.py (当前骨架)
- bridge.py (当前单文件实现)

任务：执行 Step 2 — 将 SessionState、事件管理从 bridge.py 迁入 bridge/session.py。

具体要求：
1. 将 bridge.py 中 SessionState 类 (L64-131)、add_event (L115-120)、add_history_event (L123-130)、get_session (L110-112)、sessions dict、sessions_lock、LOG_DIR 迁入 bridge/session.py
2. 将事件类型常量和状态枚举填入 bridge/protocol.py
3. bridge.py 改为 from bridge.session import ... 和 from bridge.protocol import ...
4. bridge.py 删除已迁出的代码，但不改变任何业务逻辑
5. 验证：python bridge.py 功能完全不变
6. 验证：python3 -m unittest discover -s tests -v 全部通过
```

---

## Step 3: Python 拆分 — CLIAdapter 层

### 目标
将 call_claude_streaming、call_codex_streaming 迁入 adapter 实现，引入 CLIAdapter 基类。

### 前置条件
- Step 2 完成 (session 已独立)

### 具体文件改动
1. **bridge/adapters/base.py**: 填入 CLIAdapter ABC 完整实现 (capabilities, check_installed, detect_approval, detect_closure)
2. **bridge/adapters/claude_adapter.py**: 从 bridge.py L199-335 提取 call_claude_streaming 逻辑，封装为 ClaudeCodeAdapter 类
3. **bridge/adapters/codex_adapter.py**: 从 bridge.py L337-437 提取 call_codex_streaming 逻辑，封装为 CodexAdapter 类
4. **bridge/adapters/__init__.py**: 注册两个 adapter
5. **bridge.py**: 改为调用 adapter 实例的方法

### 关键注意事项
- `_stderr_reader` (L178-196) 是两个 wrapper 共享的，应放在 base 或 session 模块
- plan 文件检测逻辑 (L133-172) 是 Claude 特有的，应放在 ClaudeCodeAdapter 内
- `plan_file_lock` 全局锁需要在 adapter 层可访问

### 不回归验证
- NR-3 (协商引擎), NR-4 (会话绑定), NR-8 (Plan 文件归属)
- `python3 -m unittest discover -s tests -v`

### 提示词

```
请阅读以下文件，理解迁移计划和已完成的步骤：
- docs/ARCHITECTURE.md (§3 目标架构, CLIAdapter 抽象)
- docs/PROTOCOL.md
- bridge/adapters/base.py (当前骨架)
- bridge/adapters/claude_adapter.py (当前骨架)
- bridge/adapters/codex_adapter.py (当前骨架)
- bridge/session.py (Step 2 已迁入)
- bridge.py (当前状态)

任务：执行 Step 3 — 将 CLI wrapper 函数迁入 adapter 实现。

具体要求：
1. 填入 CLIAdapter ABC 完整实现 (base.py)
2. 从 bridge.py 提取 call_claude_streaming (L199-335) → ClaudeCodeAdapter
3. 从 bridge.py 提取 call_codex_streaming (L337-437) → CodexAdapter
4. _stderr_reader (L178-196) 放入合适位置（两个 adapter 共享）
5. plan 文件检测逻辑 (L133-172) 放入 ClaudeCodeAdapter
6. bridge.py 改为通过 adapter 实例调用
7. 验证：python bridge.py 功能不变 + python3 -m unittest discover -s tests -v 通过
```

---

## Step 4: Python 拆分 — 编排引擎

### 目标
将 run_negotiation、run_execution、run_first_review、run_review_fix_cycle 迁入 bridge/orchestration/engine.py。

### 前置条件
- Step 3 完成 (adapter 已独立)

### 具体文件改动
1. **bridge/orchestration/engine.py**: 从 bridge.py L609-912 提取完整编排引擎
2. **bridge.py**: 改为 `from bridge.orchestration.engine import ...`

### 关键注意事项
- last_complete_round (L612-623) 和 is_approved (L626-629) 应迁入 engine 或 protocol
- 引擎依赖 adapter 和 session，import 关系要理清
- 续接失败回退逻辑 (L722-751) 是最复杂的部分，必须完整迁移

### 不回归验证
- NR-3, NR-6, NR-7 (协商/执行/审查完整流程)
- `python3 -m unittest discover -s tests -v`

### 提示词

```
请阅读项目当前状态（Step 3 已完成），执行 Step 4 — 将编排引擎迁入 bridge/orchestration/engine.py。

提取范围：
- run_negotiation (含 start_round 续接参数)
- run_execution (含 git baseline 捕获)
- run_first_review
- run_review_fix_cycle
- last_complete_round, is_approved 辅助函数

关键：续接失败回退逻辑 (裁剪 history, 恢复 current_round/max_rounds) 必须完整保留。
验证：python bridge.py 完整协商→执行→审查→修复流程可用 + tests 通过。
```

---

## Step 5: Python 拆分 — 提示词 + 路径 + HTTP Server

### 目标
将提示词构建、路径浏览/补全、HTTP Server 分别迁入模块。bridge.py 缩减为薄入口。

### 前置条件
- Step 4 完成

### 具体文件改动
1. **bridge/orchestration/prompts.py**: 从 bridge.py L440-606 提取所有 build_*_prompt 函数 + Git 工具函数
2. **bridge/server.py** (新增): 提取 BridgeHandler + ThreadedHTTPServer
3. **bridge.py**: 缩减为入口脚本 (~50行)：import 各模块，解析参数，启动 server

### 不回归验证
- NR-5, NR-8, NR-9, NR-10 (注入/Plan/提示词/前端)
- `python3 -m unittest discover -s tests -v`

### 提示词

```
执行 Step 5 — 完成 bridge.py 模块化拆分。

1. 提示词构建函数 → bridge/orchestration/prompts.py
2. Git 工具函数 (baseline 捕获/diff) → bridge/orchestration/prompts.py 或独立文件
3. HTTP Server (BridgeHandler, ThreadedHTTPServer) → bridge/server.py
4. bridge.py 缩减为入口脚本：解析参数 + 启动 server
5. HTML_UI 字符串保留在 server.py 中（前端迁移留给 Step 9）

目标：bridge.py 从 2167 行缩减到 ~50 行，但 python bridge.py 行为完全不变。
```

---

## Step 6: SQLite 持久化

### 目标
引入 SQLite 持久化层 (Python sqlite3 标准库)，保存已完成会话历史、提示词配置、最近路径。

### 前置条件
- Step 5 完成 (模块化拆分完成)

### 具体文件改动
1. **bridge/persistence/store.py**: 实现 Store 类 (init_db, save_session, list_sessions, get_session_history, save_prompts, load_prompts, save_recent_paths, load_recent_paths)
2. **bridge/persistence/schema.sql**: 已有，作为建表参考
3. **bridge/server.py**: 在会话完成时 (status → done/error) 调用 store.save_session()
4. **新增 API**: GET /api/archived_sessions — 查看历史会话

### 关键约束
- **活动会话仍然是内存态** — SQLite 只存终态快照
- **重启后活动会话标记为"中断"** — 不尝试恢复
- prompts.json 和 recent_paths.json 保持向后兼容 (首次启动时迁移到 SQLite)

### 不回归验证
- 所有 NR 项 (既有功能不受影响)
- 新增测试: 会话完成 → 重启 → GET /api/archived_sessions 可见

### 提示词

```
执行 Step 6 — 引入 SQLite 持久化。

使用 Python sqlite3 标准库，零外部依赖。
- 实现 bridge/persistence/store.py
- 参考 bridge/persistence/schema.sql 建表
- 会话完成时 (done/error) 保存快照到 SQLite
- 添加 GET /api/archived_sessions 端点
- prompts.json → SQLite 迁移（首次启动时自动导入）
- recent_paths.json → SQLite 迁移

关键：活动会话仍在内存，SQLite 只存终态。重启后活动会话不可恢复。
```

---

## Step 7: Adapter 扩展 + 角色配置

### 目标
实现 adapter 注册表和角色配置 UI，让用户可以选择哪个工具做 Planner、哪个做 Reviewer。

### 前置条件
- Step 6 完成

### 具体文件改动
1. **bridge/adapters/__init__.py**: 实现 AdapterRegistry (discover, register, get_by_id)
2. **bridge/server.py**: 新增 API
   - GET /api/tools — 列出已安装工具 + 能力矩阵
   - POST /api/role_config — 设置 planner/reviewer
3. **前端 (HTML_UI)**: 在控制栏添加 Planner/Reviewer 下拉选择

### 不回归验证
- 默认配置 (Claude=Planner, Codex=Reviewer) 必须与当前行为一致
- 新增测试: 角色互换后协商流程正常

### 提示词

```
执行 Step 7 — 实现 adapter 注册表和角色配置。

1. AdapterRegistry: 启动时扫描已安装 CLI 工具，注册可用 adapter
2. API: GET /api/tools 返回工具列表 + 能力矩阵
3. API: POST /api/role_config 设置 planner_tool_id + reviewer_tool_id
4. 前端: 控制栏添加 Planner/Reviewer 下拉选择
5. 编排引擎: 根据角色配置选择对应 adapter 调用

默认行为必须与当前一致（Claude=Planner, Codex=Reviewer）。
```

---

## Step 8: 桌面壳选型 + 集成

### 目标
基于前 7 步稳定的协议层，选择并集成桌面壳 (Tauri v2 / Electron / 其他)。

### 前置条件
- Step 7 完成 (Python 内核完全模块化 + 持久化 + 角色配置)

### 关键决策点
- 评估 Tauri v2 sidecar 管理 Python 进程的可行性
- 评估 Electron child_process 管理的成熟度
- 评估是否继续使用 HTTP+浏览器方案

### 提示词

```
Bridge 项目已完成 Python 内核模块化（Step 1-7）。现在需要选择桌面壳方案。

请评估以下候选方案，并实施选定方案：
1. Tauri v2 — Python 作为 sidecar
2. Electron — Python 作为 child process
3. 保持 HTTP + 浏览器 — 添加系统托盘和桌面快捷方式

评估维度：包体大小、Python 进程管理、IPC 复杂度、移动端扩展性、开发体验。
当前 Python 后端通过 HTTP API 暴露所有功能，协议已固化（见 docs/PROTOCOL.md）。
```

---

## Step 9: 前端框架迁移

### 目标
将 HTML_UI 中的 vanilla JS 迁移到选定的前端框架。

### 前置条件
- Step 8 完成 (桌面壳集成)

### 不回归验证
- NR-10 全部通过 (前端行为完整验证)

### 提示词

```
Bridge 项目已完成桌面壳集成（Step 8）。现在需要将内嵌的 vanilla JS 前端迁移到组件化框架。

当前前端特性（必须完整保留，参见 REQUIREMENTS.md NR-10）：
- 双面板 + Process/Result tab
- 版本历史 R1/R2/R3
- agent_thinking 自动切 tab
- MCP stderr 淡化
- 路径浏览器 + 自动补全 + 最近路径
- 页面刷新恢复
- 提示词编辑器 modal
- 状态 pill + 按钮可用性

事件处理逻辑在 handle(e) switch-case 中（20 种事件类型），必须全部迁移。
```

---

## Step 10: 移动端 daemon + remote client

### 目标
基于 MOBILE_DESIGN.md 实现桌面端 WebSocket daemon 和移动端远程控制客户端。

### 前置条件
- Step 9 完成 (桌面端完全稳定)

### 提示词

```
Bridge 项目桌面端已完全稳定（Step 1-9 完成）。现在实现移动端远程控制。

请参考 docs/MOBILE_DESIGN.md 实施：
1. 桌面端 WebSocket daemon（复用现有事件协议）
2. 配对流程（QR code + 配对码）
3. 移动端 UI（简化版，详见 MOBILE_DESIGN.md §7）
4. 断连/重连处理

初期仅支持局域网直连。
```
