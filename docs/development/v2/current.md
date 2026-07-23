# 当前运行内核与安全边界

本文描述 CapsLock 2.2.4 的开发边界。产品仍是本机前台运行、固定命令模板和单层子 Agent，不提供任意 Shell、远程控制、后台 daemon 或第三方可执行 Hook。

## 运行内核

公开执行入口只有 `AgentSession.run_stream(RunRequest)`。`RunEngine` 为每个 session 持有串行锁，统一准备、恢复、模型工具循环、取消传播和终止收尾；模型切换不能与活动 run 交叉。`WorkspaceApplication` 向 CLI 暴露 session 和只读 `WorkspaceQueries`，CLI 不穿透 session 访问 repository 聚合对象。

运行内部顺序固定为校验、内置策略、授权、执行、脱敏、持久化和发布。Runtime 与 tooling 只依赖中立 ports；旧聚合 Agent、执行 service、repository bundle 和兼容别名已经删除。

## 工具与上下文

工具通过 `ToolSpec`、`ToolCapabilities` 和 `ExecutionContext` 声明输入/输出 JSON Schema、只读与并发属性、破坏性、副作用、取消行为和结果限制。连续的并发安全只读调用以最多四个任务并行，结果仍按模型 tool-call 顺序进入 checkpoint；写入、审批和上下文变更独占执行。

超过 16 KiB 的工具结果写入 `.capslock/state/artifacts/sha256/`，使用原子写入、0600 权限和 SHA-256 校验，单项上限 5 MiB。模型只收到脱敏预览和 artifact ID；分块读取按 session 隔离。

`ContextBudgetManager` 按模型 context window 和最大输出计算输入预算，并计入 system prompt、Skill catalog、memory、工具 schema 与 checkpoint。达到阈值后先外置大型结果，再保留最近轮次并生成结构化摘要。摘要作为不可变 compaction artifact 持久化并按 source digest 复用；连续失败达到上限后返回稳定的 `context_budget_exceeded`。

## 插件安全

插件 manifest、stdio protocol 和 workspace grant 使用当前协议 3。Grant 只能收窄 manifest 声明的 workspace path、network host、固定 process template 和 credential name；包版本、digest 或 capability 改变会使授权失效。

Linux 使用 Bubblewrap，macOS 使用系统 sandbox profile。插件目录只读、临时目录独立可写，不挂载 workspace/home 且默认断网；没有 sandbox backend 时拒绝执行。插件通过双向 stdio broker 请求能力，宿主重新执行路径、SSRF、真实 diff、ActionCoordinator 审批、脱敏和审计。命名 credential 单独审批，值不写入 action result、事件或错误；插件回显的已交付值会在持久化前递归脱敏。

`--trusted-native --yes` 是逐工作区高风险授权，每次插件调用仍强制人工批准，不能由 `full_access` 自动通过。

## 事件与存储

`RunEventBus` 生成单调 sequence、全局 event ID 和 run trace ID。UI 立即消费；耐久 sink 按 50 ms 或 4 KiB 批量写入，终止事件前强制 flush，失败时停止 run。诊断 sink 使用有界队列，text delta 可合并且不能改变运行结果。

Workspace SQLite 使用一个 writer 和两个只读连接；复合状态转换由 writer transaction 独占。当前 workspace schema 为 6，新增 context compaction、tool artifact 和完整 tool invocation 元数据。Memory schema 保持 3。

## 当前协议与部署

当前外部格式为 config 3、workspace schema 6、memory schema 3、JSONL 3、portable archive 3 和 plugin protocol 3。运行时代码不包含旧格式迁移、转换命令或兼容 host；非当前数据直接拒绝且不修改原文件。

部署 2.2.4 前必须停止 CapsLock 进程并备份工作区状态。已有非当前数据库需要在部署步骤中离线转换并完成逐表行数、外键、事件 ID、artifact digest 和 `PRAGMA integrity_check` 校验；转换逻辑不进入运行时包。新部署直接创建当前 schema。

## 发布门禁

合并前执行 compileall、Ruff、全量 pytest、repository hygiene、确定性 Agent/Memory 评测、依赖审计、wheel/sdist 构建、Twine 检查、版本一致性和隔离 wheel 冒烟。Linux/macOS CI 必须使用 Python 3.12，并验证轻量 CLI 命令不会导入 TUI、OpenAI 或 MCP runtime。
