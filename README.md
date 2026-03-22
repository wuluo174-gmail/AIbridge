<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-blue?logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/zero_dependencies-std_lib_only-orange" alt="Zero Dependencies">
</p>

<h1 align="center">Claude ↔ Codex Bridge</h1>

<p align="center">
  <b>Pit Claude against Codex in adversarial code review — they argue, you ship better code.</b>
</p>

<p align="center">
  <i>让 Claude 和 GPT 互相 Code Review，吵完再写代码。</i>
</p>

---

## What is this?

A single-file orchestration engine that makes **Claude Code** and **OpenAI Codex CLI** negotiate before touching your codebase.

Claude proposes a plan. Codex tears it apart with first-principles review. Claude revises. They go back and forth until Codex can't find anything wrong — then, and only then, Claude executes.

You watch the whole debate live in your browser.

```
Your task
   │
   ▼
┌──────────┐   proposal   ┌──────────┐
│  Claude   │ ──────────→ │  Codex   │
│  Code     │ ←────────── │  CLI     │
│ (designer)│   feedback   │ (critic) │
└──────────┘              └──────────┘
   │  loop until APPROVED
   ▼
Claude executes the final plan
```

### Why?

A single AI agent writes code that *looks* right. Two agents debating produce code that *is* right. The adversarial review catches hallucinations, over-engineering, missed edge cases, and shallow fixes — before they reach your repo.

---

## Quick Start

**Prerequisites:** `python3`, [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude Code CLI), and [`codex`](https://github.com/openai/codex) (Codex CLI) installed and authenticated.

```bash
# Option 1: One-click (macOS)
double-click 一键启动Bridge.command

# Option 2: Command line
python3 bridge.py

# Option 3: Custom port
python3 bridge.py --port 9090
```

Browser opens automatically → enter your project path and task → hit **Start**.

---

## How It Works

### Adversarial Negotiation Loop

Each round follows a strict protocol:

1. **Claude** analyzes the task from first principles — data origin, data destination, transformations in between — and outputs a detailed plan (file-level changes, risk analysis, verification steps).
2. **Codex** reads the actual codebase (not just the plan) and reviews with severity tags: `[Critical]` `[Medium]` `[Suggestion]`. No flattery, no rubber-stamping.
3. **Claude** responds to each point — accept ✓ / partial △ / reject ✗ with reasoning — and revises the plan.
4. Repeat until Codex replies with **APPROVED**, or max rounds reached.
5. After consensus, Claude executes the approved plan in the same session context.

### Live Dual-Panel UI

Each agent gets two tabs — **Process** (real-time streaming of thinking, commands, stderr) and **Result** (clean final output). Tabs auto-switch as the debate unfolds. MCP startup noise is detected and dimmed.

### Human-in-the-Loop

Type feedback into the bottom bar during negotiation. Your input is injected as **"user constraints (must prioritize)"** into both agents' next prompts simultaneously.

### Session Isolation

Every browser tab is an independent negotiation session with its own event stream, subprocess, and history. Open multiple tabs to run parallel negotiations on different tasks. URL carries `?sid=xxx` — refresh to restore.

### Conversation Continuity

Both CLIs maintain persistent conversations across rounds:

- **Claude:** first call via `claude -p`, subsequent rounds via `claude -c -p` (continue mode)
- **Codex:** first call via `codex exec --json`, subsequent rounds via `codex exec --json resume --last`

Each round only sends the other agent's latest reply — no history re-injection.

### Canonical Planner Output

Claude Code's canonical planning output is the final `result` text returned by headless `--output-format stream-json`. Bridge writes that text directly into session history and passes it to the reviewer and executor unchanged.

---

## Project Structure

```
├── bridge.py                  # Everything: orchestration engine + web UI + HTTP server
├── prompts.json               # Editable prompt templates (8 configurable stages)
├── 一键启动Bridge.command       # macOS one-click launcher
└── README.md
```

**Zero external dependencies.** Python standard library only. ~1200 lines total.

---

## Customizing Prompts

Click the **Prompts** button in the UI to edit all 8 prompt templates live:

| Template | Purpose |
|----------|---------|
| `claude_first` | Claude's initial analysis prompt (first-principles + "three core questions") |
| `claude_revise` | Claude's revision prompt (respond to each critique point) |
| `codex_first` | Codex's initial review prompt (first-principles, no flattery) |
| `codex_review` | Codex's follow-up review prompt (verify fixes, find new issues) |
| `execution` | Execution prompt after APPROVED consensus |
| `execution_unapproved` | Execution prompt when max rounds reached without consensus |
| `user_inject_label_claude` | Label for user feedback injected into Claude's prompt |
| `user_inject_label_codex` | Label for user feedback injected into Codex's prompt |

Changes are saved to `prompts.json` and take effect on the next round.

---

## License

MIT

---

<details>
<summary><b>中文文档 / Chinese Documentation</b></summary>

## 这是什么？

一个单文件编排引擎，让 **Claude Code** 和 **OpenAI Codex CLI** 在修改你的代码之前先"吵一架"。

Claude 出方案，Codex 用第一性原理审查挑刺，Claude 思考反馈后修订，循环往复直到 Codex 挑不出问题——然后 Claude 才执行。你在浏览器里实时看全过程。

### 为什么需要这个？

单个 AI 写出的代码"看起来"对。两个 AI 对抗辩论后产出的代码才"真的"对。对抗式审查能在代码进入仓库之前捕获幻觉、过度工程、遗漏的边界情况和表面修复。

### 快速开始

前提：`python3`、`claude`（Claude Code CLI）、`codex`（Codex CLI）已安装且认证。

```bash
# 方式一：双击 .command 文件（macOS）

# 方式二：命令行
python3 bridge.py

# 方式三：自定义端口
python3 bridge.py --port 9090
```

浏览器自动打开 → 填项目路径和任务 → 点「开始」。

### 核心设计

**多会话并发** — 每个浏览器 Tab 是独立的协商会话，可同时处理多个不同任务。会话状态（事件流、进程、历史记录）完全隔离，互不干扰。URL 自动携带 `?sid=xxx` 参数，刷新页面可恢复会话。

**对话连续性** — 两个 CLI 各自维持独立的对话会话。Claude 首次 `claude -p`，后续 `claude -c -p`。Codex 首次 `codex exec --json`，后续 `codex exec --json resume --last`。每轮只传对方最新回复，不重复灌历史。

**双 Tab 面板** — 每个面板（Claude / Codex）提供「过程」和「结果」两个 Tab。过程 Tab 实时流式显示分析思考过程、命令执行、stderr 输出。结果 Tab 显示最终干净的计划文档或审查结论。Tab 自动切换，也可手动切换。

**人工干预** — 协商过程中在 UI 底部输入意见，下一轮会以「用户约束（必须优先考虑）」的形式同时发给双方。

**Planner 输出唯一来源** — Claude Code 在 headless plan 模式下的 canonical 输出就是 `stream-json` 的最终 `result` 文本。Bridge 直接把它写入会话历史并传给审查者/执行者，不再依赖 `~/.claude/plans/` 文件差集。

**项目规范净化注入** — `CLAUDE.md` 不再原样塞进 Planner prompt。Bridge 会先提炼其中的分析原则、协作风格和思考维度，再注入到每轮 Planner 提示词，过滤掉“结论模板”“固定开头”“确认流程”这类输出壳子指令。

**提示词可配置** — 通过 UI 的「提示词」按钮实时编辑 8 个阶段的提示词模板，保存到 `prompts.json`。

**零外部依赖** — 仅使用 Python 标准库，约 1200 行代码。

</details>
