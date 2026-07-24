# CapsLock v2 开发者文档

本目录记录 CapsLock v2 的架构、开发过程、安全边界与发布验证。当前稳定版本为 `2.3.0`，开发协议为 Tool Runtime v2、config 5、workspace schema 8 和 plugin protocol 4。

## 文档入口

- [v2.0 开发过程与迁移](v2.0.md)：从 1.10.1 前置解耦到 2.0.0 接口冻结的实现过程、数据升级、回滚和质量门禁。
- [v2.1 插件 SDK](v2.1.md)：本地工具插件的 manifest、stdio 协议、安装授权、安全边界和测试要求。
- [v2.2 多 Agent 协作](v2.2.md)：任务契约、隔离快照、能力衰减、调度、验证和审计协议。
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

## 当前稳定边界

- 组合根为 `capslock.bootstrap.WorkspaceApplication.open()`；runtime/tooling 通过 `capslock.ports` 使用应用与存储能力。
- 模型、工具、动作、workflow 和记忆接口均为异步；公开 Agent 执行入口只有 `AgentSession.run_stream(RunRequest)`。
- workspace schema 8、memory schema 3、portable archive 3、JSONL schema 3、`config_version = 5` 和 plugin protocol 4 是当前协议。
- 配置依赖图、Memory/Workflow repository、Lifecycle I/O/import merge、Action handler、子 Agent runner 与模型路由均使用显式窄接口；只有组合根可同时装配具体 storage、runtime 与 application。
- fullscreen 中等待模态结果的斜杠命令运行在 Textual worker；根背景使用原生 `ansi_default`，Rich 内容通过只替换背景的渲染适配器保留全部字体样式。
- 支持 Linux/macOS 与 Python 3.12；两个操作系统组合由发布 CI 验证。
- config v3/v4 与 workspace schema v6/v7 使用 backup-first 自动迁移；旧 archive、JSONL、插件协议和删除的 Python 接口不提供兼容别名。
