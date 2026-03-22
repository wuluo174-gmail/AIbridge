# Bridge 需求文档

## 1. 产品定位

Bridge 是一个 AI CLI 编排器，不是通用 IDE。

它的职责是：

- 组织多个 CLI 工具按固定四角色工作流协商、执行、校验
- 提供统一账本、统一恢复、统一输入与双模式视图
- 让专业用户和非专业用户都能在同一事实流上工作

它不负责：

- 文件树
- 代码编辑器
- 真 PTY 透传
- 多角色任意图编排

但是，和旧版本不同，Bridge v4 明确提供角色终端工作台。
这不意味着它变成 IDE，而是说明“角色终端”已经是编排体验本身的一部分。

## 2. 核心用户

### 2.1 专业用户

需要：

- 看见各角色实时过程
- 直接对当前角色/当前阶段输入约束
- 用命令驱动暂停、继续、执行、修复

对应视图：`terminal`

### 2.2 非专业用户

需要：

- 像看两个或多个 agent 对话一样理解流程
- 不必理解终端细节
- 仍能在关键节点插入约束或控制动作

对应视图：`scene`

## 3. 固定工作流模板

当前版本固定四个逻辑角色：

- `planner`
- `reviewer`
- `executor`
- `validator`

支持三种绑定形态：

1. 单工具包办四角
2. 双工具对抗分工
3. 四角色多工具独立分工

固定模板流程：

1. planner 产出方案
2. reviewer 审查方案
3. 达成可执行状态后 executor 执行
4. validator 校验是否收口
5. 如未收口，executor 修复，validator 复检

## 4. 必须满足的基础约束

### R1. 数据真相必须统一

系统的业务真相只能来自：

- `sessions`
- `workflow_roles`
- `role_lanes`
- `role_events`
- `artifacts`
- `interventions`

禁止：

- 从日志推导 artifact
- 从 artifact 反推事件
- 从前端局部状态反推业务状态

### R2. 用户输入必须是一等公民

用户输入不能再混进 history 尾部连续 user 项。

必须：

- 独立持久化为 `intervention`
- 有明确生命周期
- 刷新后仍能恢复
- 能回答“谁在什么时候输入了什么，被谁消费了”

### R3. 双模式必须共享同一账本

`terminal` 和 `scene` 必须只是不同投影。

禁止：

- 一种模式一套状态树
- 一种模式一套独立 API
- 一种模式直接读日志，另一种模式直接读结果

### R4. 角色模型必须显式化

禁止：

- 只存 `planner_tool_id / reviewer_tool_id`
- 再由后端临时推导 executor/validator

必须：

- 四角色都显式出现在 workflow config 中
- 同一工具绑定多个角色时仍保持 lane 隔离

## 5. 功能需求

### F1. 工具注册与能力发现

系统必须提供：

- 工具探测
- 版本信息
- 能力矩阵
- 是否已安装

工具列表通过 `/api/tools` 统一暴露。

### F2. 工作流配置

系统必须提供：

- `view_mode`
- `workflow_template`
- `max_rounds`
- `max_review_rounds`
- `roles[]`

配置通过 `/api/workflow_config` 读写。

### F3. 协商引擎

系统必须支持：

- planner/reviewer 多轮协商
- 共识达成
- 达到最大协商轮次
- 带理由继续协商

### F4. 执行引擎

系统必须支持：

- 从 `consensus` / `max_rounds` 进入执行
- executor 读取最终 `plan` artifact
- 产生 `execution_summary` artifact

### F5. 校验与修复闭环

系统必须支持：

- validator 读取执行结果和 git diff
- 产生 `validation_report`
- 触发 `done` 或 `repairing/review_fix`
- 达到最大修复轮次后进入 `review_max_rounds`

### F6. 统一输入入口

系统必须通过 `POST /api/input` 统一接收：

- terminal 文本
- scene 文本
- slash command

并满足：

- `consensus` 状态下普通文本被拒绝
- `paused` / `interrupted` / 终态普通文本被拒绝
- 控制命令仍可走统一入口

### F7. Terminal 模式

系统必须提供：

- 四角色工作区
- 每个角色自己的过程视图
- 每个角色自己的输入入口
- artifact 快速查看
- 允许对当前会话切换到 terminal，而不是只能创建时选择

### F8. Scene 模式

系统必须提供：

- 基于 artifact 和 intervention 的时间线
- 基于统一事件流的高信号叙事卡片
- 友好的底部输入框
- 与 terminal 相同的控制能力
- 允许对当前会话切换到 scene，而不是只能创建时选择

### F9. 恢复与历史

系统必须支持：

- 页面刷新恢复
- 后端重启后恢复会话账本
- `paused` / `interrupted` 继续推进
- 刷新恢复时直接依赖 ledger snapshot，而不是从 0 重放全量 SSE

### F10. 文档与边界一致性

代码、协议、需求、架构文档必须一致描述：

- 四角色
- 双模式
- 统一账本
- 非 IDE 边界

## 6. 数据生命周期要求

### 6.1 创建

创建会话时必须同时写入：

- `sessions`
- `workflow_roles`
- `role_lanes`

### 6.2 过程

CLI 输出必须先进入 `role_events`，再由投影层渲染。

### 6.3 结果

结构化结果必须显式发布为 `artifact`。

### 6.4 输入

用户输入必须显式写入 `intervention` 或控制动作账本。

### 6.5 恢复

恢复只依赖持久化账本，不依赖旧 UI 状态。

## 7. 验收清单

- [ ] 四角色配置可保存和恢复
- [ ] 同一工具可绑定多个角色
- [ ] 协商阶段会产生 `plan` 和 `review`
- [ ] 执行阶段会产生 `execution_summary`
- [ ] 校验阶段会产生 `validation_report`
- [ ] `Process` 只读事件
- [ ] `Result` 只读 artifact
- [ ] 用户输入刷新后不丢失
- [ ] `consensus` 状态下普通文本被拒绝
- [ ] terminal 模式工作区填满可视区域
- [ ] scene 模式可回放 artifact/intervention 时间线
- [ ] 后端重启后活动会话会标记为 `interrupted`

## 8. 非目标

为了避免范围膨胀，当前版本明确不做：

- 任意 N 角色编排图
- 文件树
- 代码编辑器
- 真 PTY 透传
- 旧协议兼容层
