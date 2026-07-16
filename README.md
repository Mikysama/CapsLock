# CapsLock

一个单机、可恢复、受控编辑、执行与研究的工作区 Agent。它可分析本地文本和代码、检索带行号的证据、读取 Git 状态/diff，并管理本地记忆和可复用 Skill；文件修改、命令执行、Web 与 MCP 调用始终受现有权限与审计约束。默认不执行任意 Shell，也不自动写入记忆。

开发计划、架构决策与实施记录见 [v1 开发文档](docs/development/v1/)；当前工具与 CLI 指令见 [Agent 工具与指令参考](docs/agent-reference.md)。

CapsLock 默认使用 `approve_for_me` 权限模式；可在聊天中通过 `/permissions full|approve|ask` 切换为全自动、高风险确认或每次请求确认。即使全自动模式仍保留风险审计、文件撤销、命令超时/取消和外部访问边界。

## 安装

需要 Python 3.11 或 3.12。以下流程适用于 Linux 和 macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

安装会同时获取运行时依赖，包括 `PyYAML`（`SKILL.md` frontmatter 解析）。本项目使用 OpenAI 兼容 Chat Completions 接口（默认 DeepSeek）和 Rich CLI；测试还需要 `pytest`。若安装器不识别可选测试依赖，请执行：

```bash
pip install -e . pytest
```

程序启动时只读取 `--workspace` 指定目录中未提交的 `.env`；`.env.example` 仅作为模板，不参与运行时加载。shell 中已设置的同名变量优先级最高。请将真实密钥放在 `.env`（已被 `.gitignore` 排除），不要写入 `.env.example`：

```bash
cp .env.example .env
# 编辑 .env，将 DEEPSEEK_API_KEY 替换为真实密钥
```

也可以通过 shell 临时覆盖：

```bash
export DEEPSEEK_MODEL='deepseek-v4-pro'
```

## 使用

```bash
capslock --version
python -m capslock --version
capslock
```

直接运行 `capslock` 会进入新会话；原有的 `capslock chat` 仍然可用。输入 `/exit` 或 `/quit` 结束会话；退出时不会保留完全为空的会话。会话标题默认取首个问题，也可在聊天中执行 `/rename 新标题` 修改。会话和执行轨迹保存在工作区的 `.capslock/state/capslock.sqlite3`，可用 `capslock sessions` 查看。执行 `capslock resume` 后可根据标题用方向键选择会话，也可继续通过完整 ID/唯一 ID 前缀执行 `capslock resume <session-id>`。

历史会话可通过完整 ID 或列表中显示的唯一 ID 前缀重命名：

```bash
capslock sessions rename a14c92ef "修复登录接口超时"
```

在自然语言中直接指定文件或目录（路径相对于工作区）：

```text
你> 阅读 examples/company-handbook.md，员工每周可以远程办公几天？
你> 在 examples 目录中查找报销需要审批的规则。
```

输出包含答案、证据位置和耗时。可使用 `capslock --debug`（或 `capslock --debug chat`）查看工具执行摘要；`capslock doctor` 检查配置和工作区状态。

### 本地记忆

记忆只能由用户通过 `/memory` 命令写入。`global` 记忆跨工作区可见，`workspace` 仅在当前工作区可见，`session` 仅在当前会话可见：

```text
/memory add
/memory list workspace
/memory search Python 版本偏好
/memory show mem_...
/memory forget mem_...
/memory undo mem_...
/memory export workspace memories.json
```

模型可在相关任务中调用只读记忆检索工具，但 1.5.1 不自动提取或自动注入记忆。`forget` 可撤销；`purge` 会在二次确认后永久移除正文、索引和历史版本。导入导出仅允许工作区内的 JSON 路径。

### 本地 Skill

工作区 Skill 放在 `.capslock/skills/<name>/`，用户级 Skill 放在 `${CAPSLOCK_HOME:-~/.capslock}/skills/`。同名工作区包覆盖用户包。每个包以带 YAML frontmatter 的 `SKILL.md` 为入口，可附带 `references/`、`assets/` 和 `scripts/` 只读资源：

```markdown
---
name: workspace-summary
description: Summarize a workspace when the user asks for a project overview.
---

Inspect the relevant files and return a concise evidence-backed summary.
```

消息开头使用 `$skill-name [raw arguments]` 显式调用；普通对话中，模型只先看到有效 Skill 的名称和描述，匹配任务时再按需加载正文：

```text
/skills list
/skills validate workspace-summary
/skills show workspace-summary
/skills disable workspace-summary
$workspace-summary focus on release readiness
```

Skill 输出是普通文本并沿用现有 citations。Skill 不能声明额外工具、权限、hooks 或执行模型，`scripts/` 也没有专用执行入口；所有动作继续服从当前权限模式。Skill 文件可以由 Agent 提出修改，但三种权限模式下都必须逐次人工确认。旧 TOML/Schema 包和旧 Skill 目录不会被发现，也不提供自动转换命令；需要保留的工作流应重新编写为目录名一致的 `SKILL.md` 包。

无充分本地证据时，Agent 会明确说明信息不足。一次性提问可使用：

```bash
capslock ask "公司总部在哪里？"
```

## 配置与边界

环境变量优先于 `.capslock/config.toml`。支持 `CAPSLOCK_API_KEY`、`CAPSLOCK_BASE_URL`、`CAPSLOCK_MODEL`、`CAPSLOCK_TIMEOUT_SECONDS` 和 `CAPSLOCK_MAX_TURNS`；为兼容现有使用方式，`DEEPSEEK_*` 变量仍然有效。

需要项目级配置时，创建 `.capslock/config.toml` 并填写模型、命令和权限策略；不要把 API key 写入该文件。项目 MCP 声明位于 `.capslock/mcp.json`，本机私有覆盖位于 `.capslock/local/mcp.json`。

项目可用 `[memory] enabled=false` 禁止记忆变更；本机还可通过 `/memory disable` 关闭写入。项目禁令优先，两种禁用都不影响读取已有记忆。用户级记忆库默认为 `${CAPSLOCK_HOME:-~/.capslock}/state/memory.sqlite3`。`CAPSLOCK_HOME` 和 `CAPSLOCK_MEMORY_DATABASE` 只能在启动 shell 中设置且必须是绝对路径，项目 `.env` 不能重定向用户数据。

v1.x 仍只读兼容 `capslock.toml`、`capslock.mcp.json`、旧 `.capslock` 运行路径及旧用户记忆目录，并在使用时显示迁移提示；这些兼容路径计划在 v2.0 移除。Skill 不参与旧布局兼容或迁移。迁移默认只预览，不加载模型或数据库：

```bash
capslock migrate-layout
capslock migrate-layout --scope user
capslock migrate-layout --scope all --apply --yes
```

Agent 可调用本地工作区、会话任务、固定命令、Web 研究与本地 stdio MCP 工具。Agent 在对话中提出外部请求后，CLI 会展示完整请求 ID、类型、脱敏载荷和摘要，并用编号菜单选择批准、拒绝或稍后处理。Web 请求获批完成后，Agent 会自动读取来源并继续回答。稍后也可通过 `/web`、`/approve <id>`、`/reject <id>` 处理，或用 `/sources` 回查不可信网页来源。Tavily 搜索需要在环境中设置 `CAPSLOCK_TAVILY_API_KEY`（也兼容 `TAVILY_API_KEY`）。v1.3 仅支持公开 `http/https` URL 和显式配置的 stdio MCP，拒绝私网 URL、远程 MCP、OAuth 与任意 Shell。

## 架构

- `application/`：工作区资源装配与统一动作审批、执行和撤销流程。
- `runtime.py` / `model.py`：模型循环、提供方无关协议、证据校验与普通 run 状态。
- `skills/`：`SKILL.md` 校验、双层注册表、catalog 与单次 run 资源快照。
- `tooling/`：工具注册表，以及工作区/Git、任务/来源和动作适配器。
- `storage/`：SQLite 连接、版本化迁移和领域 repositories。
- `memory.py`：作用域、生命周期、脱敏、导入导出和模型访问隔离。
- `policy.py`：工作区路径、文件大小和受控读写安全边界。
- `changes.py`：编辑提案、审批、哈希校验、应用与撤销。
- `execution.py`：固定命令模板、审批、执行、超时与输出控制。
- `external.py`：Tavily 搜索、URL 抓取、来源审计与外部动作。
- `mcp.py`：双层 MCP 配置和受控 stdio 调用。
- `evidence.py`：稳定、可定位的文件证据。
- `session.py`：面向运行时的持久化 facade。
- `cli/`：命令行装配、声明式命令、prompt-toolkit 输入和 Rich 展示。

## 验证

```bash
python -m pytest -q
python -m ruff check .
python scripts/check_repository.py
```

测试不需要 API 密钥：它们使用模拟客户端验证工具循环、证据 ID、会话恢复、越界路径拒绝和只读工具结果。

v1.5.1 的 Skill 契约、目录迁移、数据库清理和发布步骤见 [发布说明](docs/releases/v1.5.1.md)；未发布的 v1.5.0 契约已被取代，记忆模型设计见 [v1.4.0 发布说明](docs/releases/v1.4.0.md)。
