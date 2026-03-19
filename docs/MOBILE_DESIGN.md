# Bridge 移动端设计 / Mobile Architecture Design

## 1. 架构模型

移动端**不是同一个 app 的跨平台编译**，而是一个独立的远程控制客户端。

```
┌──────────────┐          WebSocket          ┌──────────────┐
│  桌面端       │◄──────────────────────────►│  移动端       │
│  (daemon)    │                             │  (client)    │
│              │                             │              │
│  CLI 工具    │                             │  无 CLI      │
│  Python 后端 │                             │  只有 UI     │
│  SQLite      │                             │  缓存状态    │
└──────────────┘                             └──────────────┘
```

- **CLI 只在桌面端运行** — 移动端无法运行 claude/codex 等 CLI
- **移动端只做远程控制** — 查看状态、发送反馈注入、触发执行/停止
- **桌面端作为 daemon** — 持续运行，监听移动端连接

## 2. 配对流程 (Pairing)

### 2.1 首次配对

```
桌面端                                移动端
  │                                    │
  │  1. 生成配对码 (6位数字)              │
  │     + QR code (含 IP:port + token)  │
  │                                    │
  │  ◄──── 2. 扫描 QR 或输入配对码 ─────  │
  │                                    │
  │  3. 验证配对码                       │
  │     生成长期令牌 (JWT/UUID)           │
  │     保存设备信息                      │
  │                                    │
  │  ────► 4. 返回长期令牌 ─────────────► │
  │                                    │
  │  ◄──── 5. 保存令牌，后续自动连接 ────  │
```

### 2.2 配对信息

```json
{
  "pairing_code": "482951",
  "qr_payload": {
    "host": "192.168.1.100",
    "port": 8765,
    "token": "one-time-pairing-token",
    "version": 1
  }
}
```

### 2.3 安全考虑

- 配对码有效期: 5 分钟
- 配对码仅单次使用
- 长期令牌绑定设备指纹
- 支持在桌面端撤销已配对设备

## 3. 通信协议

### 3.1 传输层: WebSocket

选择 WebSocket 而非 HTTP SSE，因为需要双向通信 (移动端发送命令 + 桌面端推送事件)。

### 3.2 消息格式

```json
{
  "id": "msg-uuid",
  "type": "request" | "response" | "event",
  "action": "...",
  "payload": { ... },
  "ts": "ISO-8601"
}
```

### 3.3 移动端 → 桌面端 (请求)

复用现有 HTTP API 语义，映射为 WebSocket 消息:

| action | 对应 API | payload |
|--------|---------|---------|
| `start` | POST /api/start | `{task, project_path, max_rounds}` |
| `execute` | POST /api/execute | `{session_id}` |
| `stop` | POST /api/stop | `{session_id}` |
| `inject` | POST /api/inject | `{session_id, message}` |
| `continue` | POST /api/continue | `{session_id, extra_rounds, message?}` |
| `review_fix` | POST /api/review_fix | `{session_id}` |
| `review_skip` | POST /api/review_skip | `{session_id}` |
| `get_state` | GET /api/state | `{sid?}` |
| `get_sessions` | GET /api/sessions | `{}` |
| `get_history` | GET /api/history | `{sid}` |
| `get_recent_paths` | GET /api/recent_paths | `{}` |

### 3.4 桌面端 → 移动端 (事件推送)

复用现有事件协议 (PROTOCOL.md §2)，所有 20 种事件类型直接推送:

```json
{
  "type": "event",
  "action": "session_event",
  "payload": {
    "session_id": "abc12345",
    "event": {
      "id": 42,
      "type": "agent_chunk",
      "data": {"agent": "claude", "text": "..."},
      "ts": "..."
    }
  }
}
```

## 4. 网络边界

### 4.1 局域网

- **发现**: mDNS/Bonjour 广播桌面端服务
- **连接**: 直连 WebSocket (ws://IP:port)
- **延迟**: <1ms，最佳体验
- **限制**: 同一局域网

### 4.2 公网

| 方案 | 优势 | 劣势 |
|------|------|------|
| **Tailscale/ZeroTier** | 安全隧道，无需公网暴露 | 需要用户额外安装 |
| **中继服务 (relay)** | 用户无需配置网络 | 需要部署和维护中继服务器，增加延迟 |
| **端口转发** | 最直接 | 安全风险高，用户配置复杂 |

**建议**: 初期仅支持局域网，公网方案待用户需求验证后选择。

### 4.3 TLS

- 局域网: 可选 (自签证书)
- 公网: 必须 (wss://)

## 5. 桌面端离线时行为

```
移动端状态机:
  connected ──断连──► reconnecting ──超时──► disconnected
       ▲                    │
       └────── 重连成功 ─────┘
```

| 状态 | 移动端行为 |
|------|---------|
| `connected` | 正常操作，实时事件推送 |
| `reconnecting` | 显示"重连中..."，缓存用户操作队列 |
| `disconnected` | 显示"桌面端离线"，展示最后已知状态 (只读)，操作按钮禁用 |

### 重连策略

- 指数退避: 1s → 2s → 4s → 8s → 16s → 30s (最大)
- 重连成功后: 请求 `get_state` + `get_sessions` 同步最新状态
- 网络切换 (WiFi ↔ 蜂窝) 时立即触发重连

## 6. 状态同步

### 6.1 初始同步 (连接建立后)

```
移动端 ──get_sessions──► 桌面端
移动端 ◄──sessions list──
移动端 ──get_state(sid)──► (对每个活动会话)
移动端 ◄──state snapshot──
移动端 ──get_history(sid)──► (可选，按需加载)
移动端 ◄──history data──
```

### 6.2 增量同步 (连接期间)

桌面端主动推送所有 session_event，移动端实时更新本地缓存。

### 6.3 冲突处理

移动端是 client，桌面端是 authority。如果移动端的缓存状态与桌面端不一致，以桌面端为准。

## 7. 移动端 UI 简化

移动端不需要完整复现桌面端 UI:

| 功能 | 桌面端 | 移动端 |
|------|--------|--------|
| 双面板实时流 | ✓ 完整 | 简化: 单面板，可切换 agent |
| 版本历史 | ✓ R1/R2/R3 按钮 | 简化: 仅显示最新版本 |
| 路径浏览器 | ✓ 完整 | 简化: 仅最近路径列表 |
| 提示词编辑器 | ✓ 完整 | ✗ 不提供 (在桌面端配置) |
| 会话历史 | ✓ 完整 | ✓ 列表 + 详情 |
| 反馈注入 | ✓ 完整 | ✓ 完整 |
| 执行/停止控制 | ✓ 完整 | ✓ 完整 |

## 8. 技术候选

待前面步骤 (Step 1-8) 稳定后选择:

| 方案 | 适用场景 | 注意事项 |
|------|---------|---------|
| Tauri v2 Mobile | 与桌面端共享前端代码 | v2 移动端成熟度待验证 |
| React Native | 原生体验最佳 | 需要单独维护移动端代码 |
| Flutter | 跨平台一致性好 | Dart 语言，学习成本 |
| PWA | 零安装，浏览器即用 | 推送通知受限，无系统集成 |

**决策时机**: Step 10 (桌面端完全稳定后)
