# Bridge 架构设计 / Architecture Design

## 1. 架构原则

1. **Python 是唯一业务真相源** — 编排、状态、持久化、CLI 调用全在 Python 内。壳层只做 UI 渲染和 IPC 透传。
2. **先固化协议，再拆内核，最后换壳** — 顺序不可调换。
3. **CLIAdapter 只在 Python 内实现** — 前端消费只读元数据 (名称、能力矩阵)，不维护适配逻辑。
4. **移动端 = 桌面 daemon + 远程 client** — 独立设计，非同一 app 跨平台编译。
5. **技术选型推迟** — 候选方案 + 取舍分析，待内核稳定后确定。
6. **bridge.py 不复制** — 唯一 canonical 入口，原地模块化拆分。
7. **可执行验证优于文档验证** — contract tests 保障协议不漂移。

---

## 2. 仓库结构

```
bridge/                          ← Python 后端模块
├── protocol.py                    事件类型 + 协议常量
├── adapters/                      CLI 适配器 (claude, codex)
├── orchestration/                 编排引擎 + 提示词构建
├── persistence/                   SQLite store + schema
├── server.py                      HTTP Server + no-dist bootstrap 页
└── session.py                     SessionState + 事件管理

frontend/                        ← Svelte 5 + TypeScript 前端 (Vite 构建)
├── src/
│   ├── components/                11 个 Svelte 组件
│   ├── lib/                       store, event-handler, hydrator, types, api
│   └── App.svelte                 根组件
└── dist/                          构建产物 (gitignored，server.py 优先伺服)

src-tauri/                       ← Tauri v2 桌面壳 (Rust)
├── src/main.rs                    进程管理 + webview 导航
└── tauri.conf.json                构建/dev 配置

bridge.py                        ← 薄 facade + main() 入口
tests/test_contract.py           ← 合同测试 (197 cases)
```

### 数据流

```
用户输入 ──POST /api/start──► SessionState (内存会话)
                                    │
                                    ├── _persist_session() ──► SQLite 会话账本
                                    │                           ├ sessions
                                    │                           ├ session_events
                                    │                           ├ session_history
                                    │                           └ review_history
                                    │
                              ┌─────▼─────┐
                              │ 编排引擎    │
                              │ (线程)     │
                              └─────┬─────┘
                                    │
                         ┌──────────┼──────────┐
                         ▼                     ▼
                   call_claude_streaming  call_codex_streaming
                         │                     │
                         ▼                     ▼
                   add_event / add_history_event
                         │
                         ├── sess.events[] ──► GET /api/events
                         └── SQLite 统一账本 ─► GET /api/state /history /sessions
                                                   │
                                                   ▼
                                              前端 hydrate / handle(e)
```

### 关键耦合点

1. **CLI Wrapper ↔ 编排引擎**: call_claude_streaming 的 canonical 输出就是 CLI `result` 文本，wrapper 保持纯 I/O
2. **前端 ↔ 事件协议**: handle(e) 的事件分支与 add_event 调用一一对应
3. **前端 ↔ HTTP API**: 历史查看、恢复、轮询都建立在 /api/state, /api/history, /api/events, /api/sessions 上
4. **会话绑定 ↔ CLI flags**: adapter_state 决定首次会话与 resume 行为

---

## 3. 目标架构

```
┌───────────────────────────────────────────────┐
│ Python Backend (唯一业务真相源)                  │
│                                               │
│  ┌──────────┐  ┌─────────────┐  ┌──────────┐ │
│  │ Adapters │  │ Orchestration│  │ Protocol │ │
│  │ ├ claude │  │ ├ negotiation│  │ ├ events │ │
│  │ ├ codex  │  │ ├ execution  │  │ ├ state  │ │
│  │ └ (扩展) │  │ ├ review     │  │ └ api    │ │
│  └────┬─────┘  │ └ session    │  └────┬─────┘ │
│  subprocess    └──────────────┘  HTTP/IPC     │
│       │                               │       │
│  ┌────▼─────┐                  ┌──────▼─────┐ │
│  │ CLI 工具  │                  │ 壳层(可替换) │ │
│  │ claude   │                  │ 当前: HTTP  │ │
│  │ codex    │                  │ 未来: 待选  │ │
│  └──────────┘                  └────────────┘ │
│                                               │
│  ┌──────────────────────────────────────────┐ │
│  │ Persistence (Python sqlite3)             │ │
│  │ 统一会话账本 / 提示词 / 最近路径 / 工具注册 │ │
│  │ ※纯内存运行态不可持久化，逻辑状态可恢复     │ │
│  └──────────────────────────────────────────┘ │
└───────────────────────────────────────────────┘
```

### 模块边界

| 模块 | 职责 | 外部依赖 |
|------|------|---------|
| `bridge/session.py` | SessionState 类 + 事件管理 | protocol (EVENT_TYPES) |
| `bridge/protocol.py` | 事件类型常量 + 状态枚举 | 无 |
| `bridge/adapters/base.py` | CLIAdapter ABC + run() 进程生命周期 | session (add_event) |
| `bridge/adapters/claude_adapter.py` | Claude Code CLI 封装 | subprocess |
| `bridge/adapters/codex_adapter.py` | Codex CLI 封装 | subprocess |
| `bridge/orchestration/engine.py` | 协商/执行/审查循环 | adapters, session, protocol |
| `bridge/orchestration/prompts.py` | 提示词构建 | protocol (键常量) |
| `bridge/persistence/store.py` | SQLite 持久化 | sqlite3 (标准库) |

### CLIAdapter 抽象

```python
class CLIAdapter(ABC):
    id: str                    # "claude-code", "codex"
    display_name: str
    cli_name: str              # 可执行文件名: "claude", "codex"
    agent_name: str            # 事件流代理名: "claude", "codex"
    log_raw_stdout: bool       # Codex=True (记录 raw lines), Claude=False (仅 text chunks)

    @property
    def capabilities(self) -> dict:
        """能力矩阵 — 前端只读消费"""
        ...

    # ── 生命周期 ──
    def check_installed(self) -> bool: ...          # 具体: shutil.which(cli_name)
    def run(self, prompt, cwd, sess, **kw) -> str:  # 具体: Template Method (进程生命周期)

    # ── 抽象钩子 (子类必须实现) ──
    @abstractmethod
    def build_command(self, prompt, cwd, **kwargs) -> list[str]: ...
    @abstractmethod
    def parse_stream_line(self, line: str) -> dict | None: ...

    # ── 可选钩子 (子类可重写) ──
    def get_env_overrides(self) -> dict | None: ...       # 默认 None
    def extract_result(self, stream_display, result_text) -> str: ...  # 默认 result_text or join
    def format_process_error(self, returncode, log_file) -> str: ...   # 非零退出消息
    def format_not_found_error(self) -> str: ...           # FileNotFoundError 消息

    # ── 协议检测 ──
    def detect_approval(self, text: str) -> bool: ...
    def detect_closure(self, text: str) -> bool: ...

    # ── 共享工具 ──
    @staticmethod
    def stderr_reader(proc, agent, log_file, log_lock, sess): ...
```

### 持久化边界

| 类别 | 可持久化 | 说明 |
|------|---------|------|
| 会话全生命周期快照 | ✓ | 从 start 创建即写入，持续更新 status / phase / updated_at / finished_at |
| 事件日志 | ✓ | `session_events` 用于状态与历史回放 |
| 协商/审查历史 | ✓ | `session_history` / `review_history` |
| 提示词模板 | ✓ | 11 个键 + 未来按工具覆盖 |
| 最近路径 | ✓ | ≤10 条 |
| CLI 工具注册 | ✓ | 安装路径、版本、能力快照 |
| **纯内存运行态** | ✗ | stop_flag, active_proc, active_pgid, event_lock 等。重启后不恢复对象本身，但依据账本重建可恢复会话 |

---

## 4. 候选技术选型与取舍

### 4.1 桌面壳

| 方案 | 优势 | 劣势 | 适用场景 |
|------|------|------|---------|
| **Tauri v2** | 包体小 (5-10MB)，原生 sidecar 管理，v2 支持移动 | Python sidecar 打包复杂，移动端成熟度待验证 | 重视包体和跨平台原生体验 |
| **Electron** | 生态最成熟，调试工具完善，社区大 | 包体大 (150MB+)，内存占用高 | 重视开发效率和生态 |
| **保持 HTTP+浏览器** | 零打包成本，当前方案直接可用 | 非原生体验，无系统集成 (通知/托盘等) | 优先快速迭代，暂不关注分发 |

**Step 8A 决策 (macOS POC)**: Tauri v2。Python 后端通过 `/bin/zsh -c` 启动（复制 `.command` 的 PATH + zshrc 语义）。
进程管理：`start_new_session=True` 隔离 CLI 进程组，`proc_lock` 保护 pgid 原子读写，
二段式清理 (SIGTERM → 3s → SIGKILL)。Tauri webview 加载 `http://localhost:PORT/`。
Linux 可从此 POC 延伸（同为 POSIX），Windows 为独立后续步骤。

### 4.2 前端框架

**已决策: Svelte 5 + TypeScript** (Step 9 实施完成)

- 编译时优化，包体极小，Svelte 5 runes 提供细粒度响应式
- frontend/ 为独立 Vite 项目，Tauri beforeBuildCommand / beforeDevCommand 自动编译
- server.py 在无 dist 时只返回构建引导页，不再维护第二套前端快照

### 4.3 持久化

| 方案 | 优势 | 劣势 |
|------|------|------|
| **Python sqlite3** | 标准库零依赖，查询能力强，单文件可移植 | 并发写入需注意 WAL 模式 |
| **JSON 文件** | 最简单，当前已使用 (prompts.json, recent_paths.json) | 查询能力弱，大数据量性能差 |

**倾向**: sqlite3 (标准库，零外部依赖，与项目"零依赖"理念一致)

### 4.4 移动端

详见 MOBILE_DESIGN.md。核心决策: 桌面 daemon + 远程 client 架构，技术方案待前面步骤稳定后选。

---

## 5. 迁移策略

### 核心约束

1. `python bridge.py` **必须在每个迁移步骤后仍可完整运行**
2. 每步只迁移一个模块边界
3. 每步跑 contract tests 验证不回归
4. 骨架文件在 Step 2 开始接入运行路径

### 迁移顺序

```
Step 1: 固化协议 + 骨架 (本次)
    │
Step 2: bridge/session.py (SessionState + 事件管理)
    │
Step 3: bridge/adapters/ (CLI 适配器)
    │
Step 4: bridge/orchestration/engine.py (编排引擎)
    │
Step 5: bridge/orchestration/prompts.py + HTTP Server (提示词 + 路径 + Server)
    │
Step 6: bridge/persistence/ (SQLite 持久化)
    │
Step 7: adapter 扩展 + 角色配置 UI
    │
Step 8: 桌面壳选型 + 集成
    │
Step 9: 前端框架迁移 ✅ (Svelte 5 + Vite + TypeScript)
    │
Step 10: 移动端 daemon + remote client
```

### 为什么这个顺序

- **Session 先拆**: 它是所有模块的基础依赖，且最独立
- **Adapter 第二**: 依赖 Session (add_event)，被 Engine 依赖
- **Engine 第三**: 依赖 Adapter + Session，是最大最复杂的模块
- **Server 最后拆**: 它是"壳层"，依赖所有上层模块，也是将来被替换的部分
- **桌面壳和前端框架放最后**: 协议固化 + 内核稳定后换壳才有意义
