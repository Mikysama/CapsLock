# CapsLock Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化格式。

## [Unreleased]

### Added

- v1 只读工作区 Agent：会话恢复、SQLite 轨迹、结构化证据与只读工作区工具。
- v1.1 受控编辑：持久化变更提案、逐次审批、统一 diff、哈希冲突检测与可确认撤销。
- v1.2 受控执行：固定命令模板、逐次审批、超时/取消处理、输出上限、任务状态与 token/费用统计。
- v1.3 受控研究与 MCP：Tavily Web 搜索、受 SSRF 保护的抓取、来源审计、双层 MCP 配置和 stdio 工具调用审批。
- 权限策略：`full_access`、`approve_for_me`、`ask_for_approval` 三档运行时切换，以及风险评估、审计和回滚建议。
- CLI UI：CapsLock 启动卡片、透明语义主题、独立输入区域、权限状态、实时斜杠命令双列补全、前缀过滤和退格刷新。


### Security

- 默认拒绝工作区外访问、二进制/超大文件、网络、写入和任意命令执行。
