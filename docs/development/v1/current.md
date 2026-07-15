## 当前架构

```text
CLI
 └─ WorkspaceAgent (runtime.py)
     ├─ ToolRegistry (tools.py)
     │   └─ WorkspacePolicy (policy.py)
     ├─ ChangeService (changes.py)
     ├─ CommandService (execution.py)
     ├─ WebService (external.py)
     ├─ McpService (mcp.py)
     ├─ Evidence (evidence.py)
     ├─ SessionStore / SQLite (session.py)
     └─ EventSink (observability.py)
```

模块职责：

- `runtime.py`：模型工具循环、引用校验、错误与运行生命周期。
- `tools.py`：工具定义、输入 Schema、工具执行与标准化结果。
- `policy.py`：工作区受控读写安全边界。
- `changes.py`：变更提案、审批、应用、冲突保护与撤销。
- `execution.py`：固定命令模板、审批、进程执行、超时与输出限制。
- `external.py`：Tavily 研究、公开 URL 校验、来源与外部动作审计。
- `mcp.py`：双层 MCP 配置、stdio 生命周期和工具调用审批。
- `evidence.py`：可定位的文件证据。
- `session.py`：SQLite 状态和轨迹。
- `observability.py`：脱敏事件日志。
- `config.py`：环境变量与 `capslock.toml` 配置加载。
- `cli.py`：命令行交互与结果展示。

## 验证

当前 56 项测试覆盖：

- 路径越界、二进制文件与超大文件拒绝。
- 文件读取、全文检索和稳定 Evidence ID。
- 无效工具 JSON 的可恢复处理。
- 证据引用、会话恢复和跨工作区会话拒绝。
- CLI 版本、启动、`doctor` 脱敏、工作区 `.env` 加载与配置优先级。
- Web/MCP 权限、真实 stdio server、超时/崩溃恢复和子进程清理。
- v1.0–v1.3 的路径、证据、会话、编辑、命令、来源、权限和主题回归。

运行：

```bash
python -m ruff check .
python -m pytest -q
python scripts/check_repository.py
python scripts/verify_release.py --tag v1.3.1
capslock doctor
```


## 发布维护建议

- README 保持面向使用者：定位、安装、快速开始、配置、主要命令和安全边界。
- 每个版本的详细目标、设计取舍、迁移说明和验证结果写入 `docs/`。
- 使用 Git tag 与 GitHub Release 标记正式版本，例如 `v1.0.0`；Release Notes 记录用户可见变化、升级步骤与已知限制。
- `CHANGELOG.md` 维护面向用户的版本摘要；大型设计过程保留在版本化文档或 GitHub Issues/Projects 中。
