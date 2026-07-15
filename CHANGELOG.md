# CapsLock Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化格式。

## [Unreleased]

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
