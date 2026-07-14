# CapsLock Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化格式。

## [Unreleased]

### Added

- v1 只读工作区 Agent：会话恢复、SQLite 轨迹、结构化证据与只读工作区工具。
- v1.1 受控编辑：持久化变更提案、逐次审批、统一 diff、哈希冲突检测与可确认撤销。

### Changed

- 产品和技术标识从 AgentBuild 统一更名为 CapsLock / `capslock`。

### Security

- 默认拒绝工作区外访问、二进制/超大文件、网络、写入和任意命令执行。
