# CapsLock v1 开发记录

## 目标

将最初的本地 Markdown 问答 demo 升级为单机、只读、可恢复的工作区 Agent。

v1 的核心原则：

- 默认只读：不修改用户文件，不执行任意命令。
- 默认离线：不提供联网搜索、抓取或外部自动化。
- 所有本地事实可追溯：答案的文件结论必须关联真实的路径与行号证据。
- 单机可恢复：会话和运行轨迹保存到工作区 SQLite。

## v1 范围

已实现：

- OpenAI Chat Completions 兼容的模型调用层，默认保留 DeepSeek 配置。
- Rich CLI：`chat`、`ask`、`resume`、`sessions`、`doctor`。
- SQLite 会话、消息、运行、工具调用和引用记录。
- 只读工具：`list_files`、`read_file`、`search_files`、`git_status`、`git_diff`、`task_list_update`。
- 工作区路径限制、UTF-8 校验、单文件大小限制和文件扫描上限。
- 稳定 Evidence ID，避免不同文件的相同行号产生错误引用。
- 模拟模型测试：工具参数错误恢复、会话恢复、越界拒绝、配置优先级与文本检索。

明确不在 v1：

- 写文件、删除、任意 Shell、网络访问。
- MCP、插件、Skill、多 Agent、长期记忆、主动推送、Web Dashboard。
- 二进制、PDF、图片和多模态理解。

## 实施过程

### 1. 从问答脚本收敛为运行时

早期实现将模型循环、Markdown 检索、算术工具、引用解析与内存历史放在同一模块。v1 将其替换为 `WorkspaceAgent`：它只负责模型循环、证据收集、运行状态和会话协调。

工具调用改为统一的 `Tool` 协议，包含名称、描述、JSON Schema、风险级别和执行函数。工具参数或路径错误会成为模型可见的结构化失败结果，而不是立即终止整轮任务。

### 2. 划定安全工作区

`WorkspacePolicy` 是所有本地工具的统一入口：绝对路径和相对路径都必须解析后位于工作区根目录内。该策略同时拒绝不可读、非 UTF-8 或超出大小限制的文件。

Git 工具是受控的只读例外，只允许固定的 `git status --short` 和 `git diff` 形式；Agent 没有通用命令执行工具。

### 3. 建立可验证证据链

`Evidence` 由文件绝对路径、起止行与文本组成，其 ID 基于路径和行范围计算。模型返回 `[[evidence:...]]` 标记时，运行时只接受本轮实际工具返回的 Evidence；无效引用不会被伪造成证据。

### 4. 加入会话与可观测性

每个工作区将状态写入 `.capslock/capslock.sqlite3`。数据库保存会话、用户与助手消息、运行结果、工具摘要和引用记录。结构化 JSON 事件写入 `.capslock/events.jsonl`，并自动脱敏常见凭据字段。

### 5. 清理遗留实现

移除了旧的 `DocumentAgent`、`DocumentLibrary`、仅 Markdown 的 `read_path/search_path` 工具与算术工具。现在项目只维护一套 Agent 运行时和一套工具协议，避免旧新两套能力继续分叉。

## v1.1：受控编辑

### 目标

在 v1 的只读分析基础上增加最小、可审计的文本编辑能力：模型只提出修改，用户在终端逐次确认后才会写入工作区。v1.1 仍不支持任意 Shell、网络、MCP、自动记忆或后台运行。

### 实现

- 新增 `changes.py`，负责编辑提案、审批、哈希校验、应用与撤销；提案先持久化到 SQLite，不会直接修改用户文件。
- `propose_file_edit` 仅接受文件中唯一匹配的精确文本替换；`propose_file_create` 只创建新文件提案；`apply_change` 仅接受同一会话中已批准的提案。
- `WorkspacePolicy` 新增写入校验：路径必须在工作区内，拒绝 `.git`、`.capslock`、二进制/不支持后缀、超限文件和符号链接越界；新建文件的父目录必须存在。
- 应用前重新计算原文件哈希；若提案之后被外部修改，安全失败并要求重新提案。创建文件同样会检测是否已被外部创建。
- Rich CLI 新增 `/changes`、`/approve <id>`、`/reject <id>`、`/undo`、`/diff`。批准和撤销都会再次展示 diff 并要求明确的 `y/yes` 确认。
- `changes` 表记录路径、操作类型、原始内容、目标内容、diff、摘要、状态和时间戳；事件日志记录提案、批准、应用、丢弃与撤销。

### 安全与恢复边界

- 编辑提案本身无副作用；`apply_change` 不能绕过审批状态。
- 一次只应用一项变更。重叠提案在应用时由哈希校验拦截，避免覆盖外部或先前变更。
- `/undo` 仅可撤销当前会话最近一次成功应用的 CapsLock 变更；若文件在应用后被修改，则拒绝不安全的恢复。
- 创建文件的撤销仅删除该次由 CapsLock 创建、且内容未被外部修改的文件；用户已有文件不提供删除能力。

### 验证

- 覆盖提案不写盘、未经批准的 `apply_change` 拒绝、批准后应用、拒绝零副作用、外部修改冲突、创建、跨会话隔离、撤销和策略边界。
- v1 与 v1.1 当前共 12 项测试：`pytest -q`。

## v1.2：受控执行与可靠性

### 目标

在不提供任意 Shell、网络或后台运行的前提下，让 Agent 能提出、审批和执行有限的本地验证命令，并让运行、任务、上下文和成本具有可恢复的记录。

### 实现

- 新增 `execution.py` 与命令提案表。命令必须先通过 `propose_command` 创建，再经 CLI `/approve <id>` 二次确认；`run_command` 会拒绝未批准或跨会话的提案。
- 仅提供固定模板：Python `pytest`、Node 的 `npm test`/`npm run build`、`ruff check`、`prettier --check`。模型不能提交自由形式 Shell；npm 模板只在对应脚本存在时可用。
- 执行器使用不启用 shell 的独立进程组，限制 cwd、超时和合并输出字节数；超时或 Ctrl-C 时终止整个进程组，并持久化失败/取消结果。
- `task_list_update` 现在写入 SQLite；`task_status_update` 支持 `pending`、`running`、`blocked`、`completed`、`failed`、`cancelled` 状态。
- 运行记录保存模型输入/输出 token 与可选费用；`/cost` 展示会话汇总。超出上下文窗口的早期消息保存为受限长度的会话摘要并在后续轮次注入。
- CLI 新增 `/commands`、`/tasks`、`/cost`；`/approve` 和 `/reject` 同时支持变更与命令 ID。

### 验证

- 覆盖命令未经批准拒绝、成功执行、超时、输出截断、cwd/会话隔离，以及 Agent 工具不能绕过审批。
- 覆盖任务状态、token/费用归档与上下文压缩；当前测试套件共 17 项。

## 当前架构

```text
CLI
 └─ WorkspaceAgent (runtime.py)
     ├─ ToolRegistry (tools.py)
     │   └─ WorkspacePolicy (policy.py)
     ├─ ChangeService (changes.py)
     ├─ CommandService (execution.py)
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
- `evidence.py`：可定位的文件证据。
- `session.py`：SQLite 状态和轨迹。
- `observability.py`：脱敏事件日志。
- `config.py`：环境变量与 `capslock.toml` 配置加载。
- `cli.py`：命令行交互与结果展示。

## 验证

当前测试覆盖：

- 路径越界、二进制文件与超大文件拒绝。
- 文件读取、全文检索和稳定 Evidence ID。
- 无效工具 JSON 的可恢复处理。
- 证据引用、会话恢复和跨工作区会话拒绝。
- `.env` 加载与环境变量优先于 TOML 配置。

运行：

```bash
pytest -q
capslock doctor
```


## 发布维护建议

- README 保持面向使用者：定位、安装、快速开始、配置、主要命令和安全边界。
- 每个版本的详细目标、设计取舍、迁移说明和验证结果写入 `docs/`。
- 使用 Git tag 与 GitHub Release 标记正式版本，例如 `v1.0.0`；Release Notes 记录用户可见变化、升级步骤与已知限制。
- `CHANGELOG.md` 维护面向用户的版本摘要；大型设计过程保留在版本化文档或 GitHub Issues/Projects 中。
