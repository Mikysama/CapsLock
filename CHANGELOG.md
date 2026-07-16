# CapsLock Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化格式。

## [Unreleased]

## [1.4.0] - 2026-07-16

### Added

- 新增用户级 SQLite 记忆库、global/workspace/session 作用域、FTS5 检索、来源、置信度和过期时间。
- 新增 `/memory list/search/show/add/edit/forget/undo/purge/export/import/status/enable/disable` 手动工作流。
- 新增只读模型记忆检索工具和 `[[memory:...]]` 引用；不自动提取、写入或注入记忆。

### Changed

- 工作区 schema 升至 v2，消息关联 run；记忆失效或 revision 变化后，相关历史 run 不再进入模型上下文。
- `doctor` 报告用户记忆库、FTS5 与当前工作区写策略。

### Security

- 记忆写入和导入统一脱敏密钥、Bearer token、私钥及常见 token；模型工具审计不保存查询或记忆正文。
- `capslock.toml` 可禁止当前项目写记忆，本机开关不能覆盖项目禁令；永久清除擦除正文、索引和历史版本。

## [1.3.2] - 2026-07-15

### Changed

- 引入统一动作主表、版本化 SQLite 迁移和迁移前自动备份，规范文件、命令、Web 与 MCP 的状态和结果类型。
- 增加应用层 `ActionCoordinator`，让 CLI 与模型工具共享审批、拒绝、执行和撤销流程。
- 拆分存储 repositories、模型适配器、工具目录以及 CLI 命令、输入和渲染模块。
- 将 CLI 转换为 package，并拆分组合根、聊天循环、命令分发、动作适配、诊断和 Rich 渲染。
- 将模型、命令、Web、MCP 和运行时配置分组，并集中管理文件类型、风险判断和敏感信息脱敏。

### Fixed

- 每次回答仅返回当前 run 的事件，不再混入先前运行事件。
- SQLite、OpenAI client 和内部 HTTP client 现在具有明确的关闭路径。
- 输入区增加下边框；完整命令、父命令子树和 `/quit`、`/session` 等 alias 的实时补全保持可见且可分发。

## [1.3.1] - 2026-07-15

### Changed

- 使用 `capslock/_version.py` 统一包元数据、CLI、启动界面和 Git tag 的版本。
- 增加 Linux/macOS、Python 3.11/3.12 的测试、lint、构建和安装验证。
- 从实际 `--workspace` 加载环境文件，并固定环境变量、兼容变量、TOML 和默认值的优先级。

### Fixed

- wheel 和 editable 冒烟测试在仓库外运行，避免误用旧的 `build/`、egg-info 或源码目录。
- 为 CLI 启动、`doctor`、真实 MCP 子进程和配置优先级补充稳定回归测试。

### Security

- 清除误提交的编辑器临时文件和 CapsLock 运行数据，扩大 Git 忽略与 CI 禁止名单。
- 轮换可能暴露的凭据，重写包含敏感产物的 Git 历史，并对完整历史执行密钥扫描。

## [1.3.0]

### Added

- v1 只读工作区 Agent：会话恢复、SQLite 轨迹、结构化证据与只读工作区工具。
- v1.1 受控编辑：持久化变更提案、逐次审批、统一 diff、哈希冲突检测与可确认撤销。
- v1.2 受控执行：固定命令模板、逐次审批、超时/取消处理、输出上限、任务状态与 token/费用统计。
- v1.3 受控研究与 MCP：Tavily Web 搜索、受 SSRF 保护的抓取、来源审计、双层 MCP 配置和 stdio 工具调用审批。
- 权限策略：`full_access`、`approve_for_me`、`ask_for_approval` 三档运行时切换，以及风险评估、审计和回滚建议。
- CLI UI：CapsLock 启动卡片、透明语义主题、独立输入区域、权限状态、实时斜杠命令双列补全、前缀过滤和退格刷新。

### Security

- 默认拒绝工作区外访问、二进制/超大文件、网络、写入和任意命令执行。
