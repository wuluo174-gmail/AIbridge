# Claude ↔ Codex Bridge

让 Claude Code 和 Codex 自动协商方案的编排工具。

Claude 出方案，Codex 审查挑刺，Claude 思考反馈后修订，循环直到 Codex 挑不出问题，然后 Claude 执行。你在浏览器里实时看全过程。

## 工作流

```
你的任务描述
     │
     ▼
┌─────────┐    方案    ┌─────────┐
│ Claude  │ ────────→ │  Codex  │
│ Code    │ ←──────── │  CLI    │
│ (设计者) │    反馈    │ (审查者) │
└─────────┘           └─────────┘
     │  循环直到 APPROVED
     ▼
 Claude 执行最终方案
```

## 快速开始

前提：`python3`、`claude`、`codex` 已安装且认证。

双击 `一键启动Bridge.command` → 浏览器自动打开 → 填项目路径和任务 → 点「开始」。

## 项目结构

```
├── bridge.py                  # 核心程序（编排引擎 + Web UI + HTTP Server）
├── prompts.json               # 可编辑的提示词模板
├── 一键启动Bridge.command       # macOS 双击启动脚本
└── README.md
```

## 核心设计

### 对话连续性

两个 CLI 各自维持独立的对话会话：
- Claude：首次 `claude -p`，后续 `claude -c -p`（-c = continue）
- Codex：首次 `codex exec --json`，后续 `codex exec --json resume --last`

每轮只传对方最新回复，不重复灌历史。

### 双 Tab 面板

每个面板（Claude / Codex）提供「过程」和「结果」两个 Tab：
- **过程 Tab**：实时流式显示分析思考过程、命令执行、stderr 输出
- **结果 Tab**：显示最终干净的计划文档（Claude）或审查结论（Codex）

Tab 自动切换：分析开始时切到「过程」，出结果时切到「结果」，也可手动点击切换。

### 结构化输出

- Claude Code 使用 `--output-format stream-json` 输出 NDJSON 事件流，从中提取 `text_delta`（过程）和 `result`（结果）
- Codex 使用 `--json` 输出 JSONL 事件流，解析 `item.completed` (agent_message / command_execution) 等事件

### stderr 并发读取

两个 CLI 的 stderr 由独立后台线程实时读取，解决了 pipe buffer 填满导致子进程死锁的问题。MCP 启动噪音（如 `mcp: MCP_DOCKER starting...`）会被识别并以暗淡样式显示在过程 Tab 中。

### 提示词

- Claude 首轮自动读取项目中的 `CLAUDE.md`，按「核心三问」分析（数据从哪来、到哪去、中间经历什么）
- Codex 审查：第一性原理阅读代码，不逢迎讨好，实事求是
- 从根因着手，不做最小可行修复
- 提示词可通过 UI 的「提示词」按钮实时编辑，保存到 `prompts.json`

### 人工干预

协商过程中在 UI 底部输入意见，下一轮会以「用户约束（必须优先考虑）」的形式同时发给双方。

### 执行

Codex 回复 APPROVED 后，点「执行」，Claude 在同一会话中用 `--dangerously-skip-permissions` 执行方案。

## 启动方式

```bash
# 方式一：双击 .command 文件

# 方式二：命令行
python3 bridge.py

# 方式三：tmux 双窗格（左Claude / 右Codex / 下控制台）
python3 bridge.py --tmux

# 自定义端口
python3 bridge.py --port 9090
```
