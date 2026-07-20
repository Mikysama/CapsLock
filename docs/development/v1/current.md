## 当前架构

```text
CLI package
 ├─ app.py                    # 参数解析、依赖装配与资源生命周期
 ├─ context.py                # Console 与 Agent 的显式上下文
 ├─ tui.py / exec.py          # 行内工作流 TUI 与 JSONL 非交互入口
 ├─ commands.py / dispatch.py # 声明式命令目录、alias 与路由
 ├─ actions.py                # ActionCoordinator 的 CLI 适配器
 ├─ diagnostics.py            # sessions、doctor 与 provider 脱敏诊断
 ├─ prompt.py                 # 输入、补全、按键与输入区边框
 └─ views/                    # typed Rich 输出
      │
      ▼
WorkspaceApplication / WorkspaceAgent
 ├─ ModelRouter + OpenAI-compatible adapters + ToolLoop
 ├─ role routing + retry/fallback + budget gate + model telemetry
 ├─ work item queue + run events + stable checkpoints
 ├─ ActionCoordinator
 │   ├─ file handler
 │   ├─ command handler
 │   └─ Web / MCP handlers
 ├─ ToolRegistry (tooling/)
 └─ WorkspaceRepositories
     └─ async database + versioned SQLite migrations (storage/)
User MemoryRepositories
 ├─ global / workspace / session scope
 ├─ FTS5 + revision history + content-free audit
 ├─ reviewed/automatic candidate extraction + provenance
 ├─ hybrid recall + local/external embeddings + outbound consent audit
 └─ MemoryService + read-only model tools
SkillRegistry / progressive loading
 ├─ user + workspace package discovery
 ├─ SKILL.md frontmatter + package validation
 └─ 16 KiB catalog + per-run read-only snapshots
```

模块职责：

- `application/`：工作区资源生命周期与统一动作协调。
- `runtime/`：provider-neutral 模型协议、确定性路由、重试/降级、预算、流式工具循环、检查点和运行事件。
- `tooling/`：工具注册表，以及工作区/Git、任务/来源和动作工具。
- `storage/`：异步 SQLite 所有权、顺序迁移和领域 repositories。
- `memory/`：用户级记忆作用域、候选审核、召回排序、外发同意、脱敏、导入导出和上下文失效隔离。
- `memory/embeddings.py`：FastEmbed、仅回环 HTTP、确认后的外部 OpenAI-compatible embedding、向量缓存和语义排序。
- `skills/`：Agent Skills 包、双层注册表、catalog 和单次 run 只读快照。
- `policy.py`：工作区受控读写安全边界。
- `changes.py`：变更提案、审批、应用、冲突保护与撤销。
- `execution.py`：固定命令模板、审批、进程执行、超时与输出限制。
- `external.py`：Tavily 研究、公开 URL 校验、来源与外部动作审计。
- `mcp.py`：双层 MCP 配置、stdio 生命周期和工具调用审批。
- `evidence.py`：可定位的文件证据。
- `session_management.py`、session repository：会话全文搜索、归档、导出和两阶段删除。
- `observability.py`：脱敏事件日志。
- `layout.py`：canonical 项目/用户目录契约和冲突检测。
- `config.py`：旧单模型兼容配置、provider/profile/role 路由和预算配置。
- `cli/`：组合根、显式上下文、TUI/exec、命令与 alias 路由、动作适配、诊断、输入和 Rich 展示。

## 验证

测试覆盖：

- 路径越界、二进制文件与超大文件拒绝。
- 文件读取、全文检索和稳定 Evidence ID。
- 无效工具 JSON 的可恢复处理。
- 证据引用、会话恢复和跨工作区会话拒绝。
- CLI 版本、启动、`doctor` 脱敏、工作区 `.env` 加载与配置优先级。
- CLI 输入区上下边框、父命令子树、完整叶子命令候选以及 `/quit`、`/session` alias。
- Web/MCP 权限、真实 stdio server、超时/崩溃恢复和子进程清理。
- fresh-v2 工作区与用户记忆 schema v1 到 v2 的一致性备份、迁移、失败回滚和幂等启动。
- 动作状态转换、跨会话访问、模型 adapter、当前 run 事件作用域和资源关闭。
- v1.0–v1.3.2 的路径、证据、会话、编辑、命令、来源、权限和主题回归。
- v1.4.0 的跨工作区记忆作用域、FTS、脱敏、生命周期、导入导出、模型引用和失效上下文隔离。
- v1.4.1 的默认聊天入口、会话标题、重命名、空会话清理、方向键恢复选择和中英文列对齐。
- v1.5.1 的 `SKILL.md` 校验、双层覆盖、禁用、catalog、按需加载、资源快照和普通 run 审计。
- v1.5.1 的 `capslock` 与 `python -m capslock` 入口、PyYAML 依赖和旧 editable 环境升级路径。
- v1.6.0 的候选策略、审核/自动采纳、来源失效、混合召回、排名解释和本地嵌入降级。
- v1.7.0 的流式事件、取消、工作项、稳定点恢复、审批风险、会话治理、JSONL 和响应式状态视图。
- v1.8.0 的多 provider 路由、上下文筛选、重试/同策略降级、预算、逐模型统计、外部 embedding 同意和固定评测。
- `.capslock` 新旧布局冲突、dry-run、幂等迁移、目录合并、符号链接拒绝、shell-only 用户路径、state/local 读取隔离和 Skill 强制确认。

运行：

```bash
python -m ruff check .
python -m pytest -q
python -m pip check
python -m pip_audit
python scripts/check_repository.py
python -m build
python -m twine check dist/*
python scripts/evaluate_agent.py --mode deterministic
python scripts/verify_release.py --tag v1.8.0
capslock doctor
python -m capslock --version
```


## 发布维护建议

- README 保持面向使用者：定位、安装、快速开始、配置、主要命令和安全边界。
- 每个版本的详细目标、设计取舍、迁移说明和验证结果写入 `docs/`。
- 使用 Git tag 与 GitHub Release 标记正式版本，例如 `v1.0.0`；Release Notes 记录用户可见变化、升级步骤与已知限制。
- `CHANGELOG.md` 维护面向用户的版本摘要；大型设计过程保留在版本化文档或 GitHub Issues/Projects 中。
