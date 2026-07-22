# CapsLock v2 开发者文档

本目录记录 CapsLock v2 的架构、开发过程、兼容边界与发布验证。当前稳定版本为 `2.1.0`。

## 文档入口

- [v2.0 开发过程与迁移](v2.0.md)：从 1.10.1 前置解耦到 2.0.0 接口冻结的实现过程、数据升级、回滚和质量门禁。
- [v2.1 插件 SDK](v2.1.md)：本地工具插件的 manifest、stdio 协议、安装授权、安全边界和测试要求。
- [Agent Reference](../../agent-reference.md)：面向 CLI、工具、事件、权限和持久化协议的完整参考。
- [v2.0.0 发布说明](../../releases/v2.0.0.md)：面向使用者的版本变化和已知限制。
- [v2.1.0 发布说明](../../releases/v2.1.0.md)：本地插件安装、授权、调用和信任模型。

## 当前稳定边界

- 组合根为 `capslock.bootstrap.open_workspace_application()`；runtime/tooling 通过 `capslock.ports` 使用应用与存储能力。
- 模型、工具、动作、workflow 和记忆接口均为异步；公开 Agent 执行入口只有 `WorkspaceAgent.ask_stream()`。
- workspace schema v4、memory schema v3、portable archive v2、JSONL schema v2 和 `config_version = 2` 是 2.0 稳定协议。
- 支持 Linux/macOS 与 Python 3.12；两个操作系统组合由发布 CI 验证。
- 新弃用至少提前一个 minor 版本公告；2.0 已删除的接口不再提供静默兼容。
