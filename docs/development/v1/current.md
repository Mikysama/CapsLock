## 当前架构

```text
CLI package
 ├─ app.py                    # 参数解析、依赖装配与资源生命周期
 ├─ context.py                # Console 与 Agent 的显式上下文
 ├─ chat.py                   # 输入循环与 Web 自动续答
 ├─ commands.py / dispatch.py # 声明式命令目录、alias 与路由
 ├─ actions.py                # ActionCoordinator 的 CLI 适配器
 ├─ diagnostics.py            # sessions、doctor 与 endpoint 脱敏
 ├─ prompt.py                 # 输入、补全、按键与输入区边框
 └─ render.py                 # 集中的 Rich 输出
      │
      ▼
WorkspaceApplication / WorkspaceAgent
 ├─ ChatModel adapter + ToolLoop
 ├─ ActionCoordinator
 │   ├─ file handler
 │   ├─ command handler
 │   └─ Web / MCP handlers
 ├─ ToolRegistry (tooling/)
 └─ SessionStore facade
     └─ repositories + versioned SQLite migrations (storage/)
User MemoryStore
 ├─ global / workspace / session scope
 ├─ FTS5 + revision history + content-free audit
 └─ MemoryService + read-only model tools
```

模块职责：

- `application/`：工作区资源生命周期与统一动作协调。
- `runtime.py`、`runtime_support.py`、`model.py`：模型适配、工具循环、引用校验和运行记录。
- `tooling/`：工具注册表，以及工作区/Git、任务/来源和动作工具。
- `storage/`：SQLite 连接、顺序迁移和领域 repositories。
- `memory.py`：用户级记忆作用域、写策略、脱敏、导入导出和上下文失效隔离。
- `policy.py`：工作区受控读写安全边界。
- `changes.py`：变更提案、审批、应用、冲突保护与撤销。
- `execution.py`：固定命令模板、审批、进程执行、超时与输出限制。
- `external.py`：Tavily 研究、公开 URL 校验、来源与外部动作审计。
- `mcp.py`：双层 MCP 配置、stdio 生命周期和工具调用审批。
- `evidence.py`：可定位的文件证据。
- `session.py`：面向运行时的持久化兼容 facade。
- `observability.py`：脱敏事件日志。
- `config.py`：环境变量与 `capslock.toml` 配置加载。
- `cli/`：组合根、显式上下文、聊天循环、命令声明与 alias 路由、动作适配、诊断、输入和 Rich 展示。

## 验证

当前 91 项测试覆盖：

- 路径越界、二进制文件与超大文件拒绝。
- 文件读取、全文检索和稳定 Evidence ID。
- 无效工具 JSON 的可恢复处理。
- 证据引用、会话恢复和跨工作区会话拒绝。
- CLI 版本、启动、`doctor` 脱敏、工作区 `.env` 加载与配置优先级。
- CLI 输入区上下边框、父命令子树、完整叶子命令候选以及 `/quit`、`/session` alias。
- Web/MCP 权限、真实 stdio server、超时/崩溃恢复和子进程清理。
- schema v0 到 v3 的备份与迁移、旧会话标题回填、重复 ID 拒绝、事务回滚和幂等启动。
- 动作状态转换、跨会话访问、模型 adapter、当前 run 事件作用域和资源关闭。
- v1.0–v1.3.2 的路径、证据、会话、编辑、命令、来源、权限和主题回归。
- v1.4.0 的跨工作区记忆作用域、FTS、脱敏、生命周期、导入导出、模型引用和失效上下文隔离。
- v1.4.1 的默认聊天入口、会话标题、重命名、空会话清理、方向键恢复选择和中英文列对齐。

运行：

```bash
python -m ruff check .
python -m pytest -q
python -m pip check
python -m pip_audit
python scripts/check_repository.py
python -m build
python -m twine check dist/*
python scripts/verify_release.py --tag v1.4.1
capslock doctor
```


## 发布维护建议

- README 保持面向使用者：定位、安装、快速开始、配置、主要命令和安全边界。
- 每个版本的详细目标、设计取舍、迁移说明和验证结果写入 `docs/`。
- 使用 Git tag 与 GitHub Release 标记正式版本，例如 `v1.0.0`；Release Notes 记录用户可见变化、升级步骤与已知限制。
- `CHANGELOG.md` 维护面向用户的版本摘要；大型设计过程保留在版本化文档或 GitHub Issues/Projects 中。
