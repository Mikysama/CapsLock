# CapsLock v2 开发者文档

本目录记录 CapsLock v2 的架构、开发过程、安全边界与发布验证。当前源码版本为 `2.7.6.4`；协议为 Tool Runtime v2、permissions v2、config 14、workspace schema 21、memory schema 6 和 plugin protocol 4。

## 文档入口

- [v2.0 开发过程与迁移](v2.0.md)：从 1.10.1 前置解耦到 2.0.0 接口冻结的实现过程、数据升级、回滚和质量门禁。
- [v2.1 插件 SDK](v2.1.md)：本地工具插件的 manifest、stdio 协议、安装授权、安全边界和测试要求。
- [v2.2 多 Agent 协作](v2.2.md)：任务契约、隔离快照、能力衰减、调度、验证和审计协议。
- [v2.6 权限与 Plan Mode](v2.6.md)：权限 v2 判定内核、Planning boundary、可恢复审批、TUI 交互、迁移和并发治理。
- [v2.7 安全加固与模块边界](v2.7.md)：Shell/Web/搜索/产物发布边界，以及 CLI、权限、运行日志和运行时的正式模块所有权。
- [当前运行内核与安全边界](current.md)：RunEngine、工具契约、上下文压缩、artifact、插件 sandbox、事件与存储协议。
- [Agent Reference](../../reference.md)：面向 CLI、工具、事件、权限和持久化协议的完整参考。
- [v2.0.0 发布说明](../../releases/v2.0.0.md)：面向使用者的版本变化和已知限制。
- [v2.1.0 发布说明](../../releases/v2.1.0.md)：本地插件安装、授权、调用和信任模型。
- [v2.2.0 发布说明](../../releases/v2.2.0.md)：本机子 Agent 委派、隔离、验证和恢复边界。
- [v2.2.1 发布说明](../../releases/v2.2.1.md)：默认 inline TUI、保留的 Textual fullscreen、透明主题和安全展示摘要。
- [v2.2.2 发布说明](../../releases/v2.2.2.md)：repository/ports 分层解耦、组合根与运行时提取、共享前台控制器和 inline 命令树修复。
- [v2.2.3 发布说明](../../releases/v2.2.3.md)：fullscreen 模态命令消息泵修复、终端默认背景和字符级透明渲染。
- [v2.2.4 发布说明](../../releases/v2.2.4.md)：运行内核、上下文预算、插件隔离、事件耐久化和当前协议边界。
- [v2.3.0 发布说明](../../releases/v2.3.0.md)：Tool Runtime v2、直接能力工具、Shell/MCP/LSP、可恢复交互与能力包重构。
- [v2.3.1 发布说明](../../releases/v2.3.1.md)：用户消息背景、fullscreen 空闲动画与流式消息重绘优化。
- [v2.4.0 发布说明](../../releases/v2.4.0.md)：类型化斜杠命令、session 导航、维护用量、上下文状态与格式 4 导出。
- [v2.5.0 发布说明](../../releases/v2.5.0.md)：记忆后台作业、混合召回、长期整理、受控指令和子 Agent 记忆提案。
- [v2.6.0 发布说明](../../releases/v2.6.0.md)：权限 v2、Plan Mode、Claude Code 风格审批和并发工具序号修复。
- [v2.7.0 发布说明](../../releases/v2.7.0.md)：Shell AST、Composer/context、IDE Bridge、远程 MCP、local tracing 与 Agent mailbox。
- [v2.7.1 发布说明](../../releases/v2.7.1.md)：只读 Shell 白名单、原子产物发布、有界网络/搜索和全量模块迁移。
- [v2.7.2 发布说明](../../releases/v2.7.2.md)：inline/fullscreen 统一、真实 context、结构化交互以及 resume/Plan 恢复修复。
- [v2.7.3 发布说明](../../releases/v2.7.3.md)：提示词信任分层、开放世界隔离以及受审批的 `/init`。
- [v2.7.4 发布说明](../../releases/v2.7.4.md)：episodic retrieval、无损分层压缩、独立记忆验证和 durability 生命周期。
- [v2.7.5 发布说明](../../releases/v2.7.5.md)：工具参数纠错、副作用状态、目录兜底、shadow 选择评估和并发治理。
- [v2.7.6 发布说明](../../releases/v2.7.6.md)：统一上下文压缩管线、summary v3、引用式工作集、降级与防抖。
- [v2.7.6.1 发布说明](../../releases/v2.7.6.1.md)：集中行为默认值、版本化策略评测、Memory RecallPolicy 与参数修复预算。
- [v2.7.6.2 发布说明](../../releases/v2.7.6.2.md)：Responses-only Provider、统一结构化输出、外部基准评测与数据库维护。

- [v2.7.6.3 发布说明](../../releases/v2.7.6.3.md)：长上下文恢复、预算与路径修复、评测格式兼容和评分隔离。
- [v2.7.6.3 开发与验证](v2.7.6.3.md)：结果哈希兼容规则、grader 协作、上下文指标和发布检查。
- [v2.7.6.4 发布说明](../../releases/v2.7.6.4.md)：运行正确性、工具与通信、压缩、评测及本地接口。
- [v2.7.6.4 开发与验证](v2.7.6.4.md)：实现边界、协议兼容、验证命令与回退。

## 当前稳定边界

- 组合根为 `capslock.bootstrap.WorkspaceApplication.open()`；runtime/tooling 通过 `capslock.ports` 使用应用与存储能力。
- 模型、工具、动作、workflow 和记忆接口均为异步；公开 Agent 执行入口只有 `AgentSession.run_stream(RunRequest)`。
- workspace schema 21、memory schema 6、portable archive 8、session export 8、JSONL schema 3、`config_version = 14`、IDE Bridge protocol 1 和 plugin protocol 4 是当前协议。
- 配置依赖图、Memory/Workflow repository、Lifecycle I/O/import merge、Action handler、子 Agent runner 与模型路由均使用显式窄接口；只有组合根可同时装配具体 storage、runtime 与 application。
- fullscreen 中等待模态结果的斜杠命令运行在 Textual worker；根背景使用原生 `ansi_default`，Rich 内容通过只替换背景的渲染适配器保留全部字体样式。
- 支持 Linux/macOS 与 Python 3.12；两个操作系统组合由发布 CI 验证。
- config v3-v13、workspace schema v6-v20 与 memory schema v3-v5 使用 backup-first 自动迁移；Provider 统一迁移到 OpenAI Responses API。portable archive v3-v7 可读取，旧 JSONL、插件协议和删除的 Python 接口不提供兼容别名。
