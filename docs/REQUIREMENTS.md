# Bridge 需求文档 / Requirements Specification

## 1. 产品定位

Bridge 是一个轻量级 vibe coding 辅助工具，聚焦于 CLI 编排。核心价值：让多个 AI CLI 工具以对抗式审查的方式协作，产出比单个工具更高质量的代码方案。

- **不是**通用 IDE —— 不提供代码编辑器、文件树、终端等传统 IDE 功能
- **是** CLI 编排器 —— 管理 CLI 工具的安装、认证、角色分配、执行和审查循环
- **依赖** CLI 工具自身的开发能力 —— Bridge 只做编排，不自己写代码

## 2. 用户角色

| 角色 | 环境 | 能力 |
|------|------|------|
| 桌面端开发者 | macOS (Step 8A POC) / Linux (POSIX 延伸) / Windows (待实现) | 完整功能：协商、执行、审查、历史、配置 |
| 移动端监控者 | iOS / Android | 远程查看会话状态、发送反馈注入、触发执行/停止；**不运行 CLI** |

## 3. 功能模块

### F1: CLI 工具管理

- **安装检测**: 检查 CLI 工具是否已安装 (`which <tool>`)
- **认证能力矩阵** (非统一接口，按工具区分):
  - 可检测安装 / 可检测认证 / 可触发认证 / 仅手工配置
  - 每个工具的认证方式不同 (browser OAuth, API key 环境变量, 配置文件等)
  - 当前代码唯一的认证相关逻辑是 FileNotFoundError 报错，所有工具的认证检测均为**待验证**
- **版本检测**: 获取已安装版本号
- **能力声明**: 每个工具通过 capability matrix 声明自己支持的功能

### F2: 角色配置

- 生成者 (Planner) / 评审者 (Reviewer) 角色可配置
- 任何已安装的 CLI 工具都可以被分配到任一角色
- 角色分配保存在持久化存储中
- 当前默认: Claude Code = Planner, Codex = Reviewer

### F3: 协商引擎

- 对抗式审查循环: Planner 出方案 → Reviewer 审查 → Planner 修订 → ...
- 可配置最大轮数
- 共识检测 (APPROVED) + 收口检测 (任务收口成功)
- 共识后可带理由驳回继续
- 续接失败自动回退到最后完整轮次

### F4: 执行引擎

- 获得共识 (APPROVED) 或达到最大轮次后可触发执行
- 执行前 Git baseline 捕获 (stash create + untracked snapshot)
- 执行使用 --dangerously-skip-permissions 模式

### F5: 执行后审查

- 执行完成后自动触发 Reviewer 审查 (带 git diff)
- "任务收口成功" → done
- 发现问题 → 等待用户确认修复
- Claude 修复 → Codex 再评审，最多 3 轮
- 用户可跳过修复

### F6: 实时流式展示

- 双面板 (Planner / Reviewer)
- 每面板 Process tab (实时流) + Result tab (版本化结果)
- Result tab 版本历史 (R1/R2/R3... 按钮)
- agent_thinking 时自动切到对应面板
- MCP stderr 噪音淡化显示
- Codex 命令输出可折叠

### F7: 用户反馈注入

- 协商过程中可随时注入反馈
- 注入内容同时影响 Planner 修订提示和 Reviewer 审查提示
- 共识状态下禁止注入 (应使用"继续协商"并附带理由)

### F8: 提示词管理

- 11 个可配置模板 (详见 PROTOCOL.md §4)
- 实时热更新 + 持久化
- 自动检测项目 CLAUDE.md 注入首轮提示
- 未来: 支持按工具覆盖 (不同 Planner/Reviewer 使用不同提示模板)

### F9: 会话历史持久化

- **可持久化**:
  - 统一会话账本 (`sessions`, `session_events`, `session_history`, `review_history`)
  - 会话全生命周期快照 (task, project_path, status, phase, round, execution_result, interrupt_reason, adapter_state_json)
  - 提示词模板配置
  - 最近项目路径
  - CLI 工具注册信息
- **不可持久化** (纯内存运行态):
  - stop_flag, active_proc, active_pgid, event_lock, status_lock
  - exec_baseline_ref, exec_baseline_untracked
  - 具体线程 / 锁 / 子进程对象本身
  - **重启后运行中的进程不会续跑**，但活动会话会标记为 `interrupted`，可基于账本重新打开或恢复

- **说明**:
  - `adapter_state_json` 属于可持久化逻辑状态，不属于纯内存运行态
  - 不再区分“活动会话”和“归档会话”两套数据真相；历史视图直接读取统一会话账本

### F10: 项目管理

- 路径浏览器 (目录导航 + git 仓库检测 + 搜索跳转)
- 路径自动补全 (≤15 条建议)
- 最近项目路径 (≤10 条)

### F11: 移动端远程控制

- 架构: 桌面 daemon + 远程 client (非同一 app 跨平台编译)
- 详见 MOBILE_DESIGN.md

---

## 4. 现有行为不回归 Checklist (No-Regression)

**以下每一项都是后续迁移步骤的验收标准。** 每个 Step 完成后必须验证相关项未丢失。

### NR-1: 状态机完整性
- [ ] 所有 13 种状态可达: idle, running, consensus, max_rounds, executing, review_pending, review_fix, review_max_rounds, paused, interrupted, aborted, done, error
- [ ] 原子 CAS 状态迁移 (status_lock 保护)
- [ ] 执行仅从 consensus/max_rounds 触发
- [ ] review_fix 仅从 review_pending 触发
- [ ] UI 按钮可用性与状态严格对应 (L1886-1903)

### NR-2: 多会话隔离
- [ ] 每个 Tab 独立 SessionState (sessions dict + sessions_lock)
- [ ] 事件流隔离 (sess.events + sess.event_lock)
- [ ] subprocess 隔离 (sess.active_proc)
- [ ] 日志隔离 (/tmp/bridge-logs/{sid}/)
- [ ] 每会话独立 adapter_state 命名空间
- [ ] 同工具双角色时使用不同 state_key 隔离会话 (如 `claude-code` / `claude-code:reviewer`)

### NR-3: 协商引擎
- [ ] Claude → Codex 交替，每轮一问一答
- [ ] 首轮 first prompt, 后续 revise/review prompt
- [ ] is_approved() 检测: 首行首词 APPROVED (大小写不敏感)
- [ ] 共识后带理由继续 (POST /api/continue + message)
- [ ] max_rounds 后追加轮次继续
- [ ] 续接失败回退到 last_complete_round (裁剪 history, 恢复 current_round, max_rounds)
- [ ] 每轮只发送对方最新回复 (不重发全部历史)

### NR-4: 会话绑定
- [ ] Claude: --session-id (首次) / --resume (续接)，不用 -c
- [ ] Codex: resume --last
- [ ] 对支持 session_resume 的工具，adapter_state[state_key].session_id 创建时生成并全程绑定
- [ ] 首次成功调用后，adapter_state[state_key].has_session 置为 True

### NR-5: 用户反馈注入
- [ ] /api/inject 非 consensus 状态下可注入
- [ ] 注入存入 history (role=user)
- [ ] collect_user_injects() 从 history 末尾收集连续 user 条目
- [ ] 注入分别进入 Claude 修订提示和 Codex 审查提示

### NR-6: 执行阶段
- [ ] --dangerously-skip-permissions 执行
- [ ] Git baseline 捕获 (stash create / HEAD + untracked)
- [ ] 执行后自动触发 run_first_review
- [ ] 执行结果存入 sess.execution_result

### NR-7: 执行后审查/修复
- [ ] Codex 审查带 git diff (capture_execution_diff, 15KB 截断)
- [ ] "任务收口成功" 首行检测 → done
- [ ] 问题 → review_fix 状态, 等待确认
- [ ] 修复循环最多 max_review_rounds=3
- [ ] /api/review_skip 跳过修复 → done
- [ ] 每轮修复结果更新 sess.execution_result

### NR-8: Planner 输出源
- [ ] Claude 协商阶段的 canonical 输出来自 headless `stream-json` 的最终 `result` 文本
- [ ] Planner 输出直接写入 `sess.history[].content`，供 Reviewer / Executor 复用
- [ ] 不依赖 `~/.claude/plans/`、快照差集、关键词校验或 plan_file_lock
- [ ] 通过 prompt 约束保证 Planner 输出完整 Markdown 方案文档，而不是结论/选项/澄清请求

### NR-9: 提示词热更新
- [ ] 11 个键全覆盖 (详见 PROTOCOL.md §4)
- [ ] POST /api/prompts 实时更新 + 持久化到 prompts.json
- [ ] detect_claude_md() 自动读取项目 CLAUDE.md (前 2000 字符)

### NR-10: 前端行为
- [ ] 双面板各有 Process/Result tab
- [ ] 版本历史 R1/R2/R3... 按钮
- [ ] 执行结果独立 "执行结果" tab
- [ ] agent_thinking 自动切 tab
- [ ] MCP stderr 淡化 (is_mcp 样式)
- [ ] Codex command_output 可折叠 (collapsible + chunk_boundary 关闭)
- [ ] 路径浏览器 (目录导航 + git 检测 badge + 搜索)
- [ ] 路径自动补全 (≤15 条)
- [ ] 最近路径 (≤10 条)
- [ ] 页面刷新恢复 (URL ?sid=xxx + /api/state + /api/history)
- [ ] 提示词编辑器 modal (11 个字段)
- [ ] 状态 pill 文本和颜色与 13 种状态一一对应

### NR-11: 事件协议
- [ ] 21 种事件类型全覆盖 (详见 PROTOCOL.md §2.2)
- [ ] add_event 产出 {id, type, data, ts} 结构
- [ ] add_history_event 原子追加 history + 发送事件
- [ ] cli_start 事件发出 (即使前端未处理)
- [ ] agent_result 事件发出 (即使前端 case 为空 break)

---

## 5. 候选技术方案与取舍

| 层 | 决策 | 状态 | 说明 |
|----|------|------|------|
| 桌面壳 | **Tauri v2** | Step 8A: macOS POC 已实施 | Python via `/bin/zsh -c` 启动，进程组级清理，系统托盘 |
| 前端 | **Svelte 5 + TypeScript** | 已实施 | frontend/ 独立 Vite 项目；无 dist 时 server.py 仅返回构建引导页 |
| 持久化 | **Python sqlite3** | 已实施 (Step 6) | 标准库零依赖 |
| 移动端 | 待选 (Tauri v2 Mobile / RN / Flutter) | Step 10 范围 | daemon + remote client 架构 |
