# CapsLock v1 开发计划与实施记录

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

## 当前架构

```text
CLI
 └─ WorkspaceAgent (runtime.py)
     ├─ ToolRegistry (tools.py)
     │   └─ WorkspacePolicy (policy.py)
     ├─ Evidence (evidence.py)
     ├─ SessionStore / SQLite (session.py)
     └─ EventSink (observability.py)
```

模块职责：

- `runtime.py`：模型工具循环、引用校验、错误与运行生命周期。
- `tools.py`：工具定义、输入 Schema、工具执行与标准化结果。
- `policy.py`：只读工作区安全边界。
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

## 后续版本路线

### v1.1：受控编辑

- 文件编辑前展示 diff。
- 每次写入需要用户确认。
- 增加 Git diff、回滚提示和写入审计。

### v1.2：受控执行与扩展

- 固定命令白名单与更细粒度权限。
- MCP / Skill 注册、加载与每工具权限。
- 更完善的上下文压缩和离线评测集。

### v2：长期运行产品能力

- 长期记忆、混合检索和用户可管理的记忆写入。
- Web Dashboard、远程渠道、定时任务和主动推送。
- 子 Agent 与复杂任务编排。

## 发布维护建议

- README 保持面向使用者：定位、安装、快速开始、配置、主要命令和安全边界。
- 每个版本的详细目标、设计取舍、迁移说明和验证结果写入 `docs/`。
- 使用 Git tag 与 GitHub Release 标记正式版本，例如 `v1.0.0`；Release Notes 记录用户可见变化、升级步骤与已知限制。
- `CHANGELOG.md` 维护面向用户的版本摘要；大型设计过程保留在版本化文档或 GitHub Issues/Projects 中。
