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

### v1 新增功能

- 本地工作区问答、文件证据引用、Git 状态与 diff 检索。
- 可恢复的 CLI 会话、SQLite 运行轨迹与脱敏 JSONL 事件。
- 面向 OpenAI 兼容接口的工具调用循环和项目级模型配置。

### v1 新增目录与文件

```text
capslock/
  runtime.py         # 模型循环与证据校验
  tools.py           # 只读工具注册表
  policy.py          # 工作区边界与文件校验
  evidence.py        # 行号证据
  session.py         # SQLite 会话/运行记录
  observability.py   # 事件日志脱敏
  config.py          # 环境变量与 TOML 配置
  cli.py             # Rich CLI
tests/
  test_runtime.py    # 运行时、引用与恢复
  test_workspace.py  # 工作区与配置边界
```

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

### v1.1 新增功能

- 精确文本替换与新文件创建提案，审批前不写入用户文件。
- 统一 diff 预览、逐次确认、冲突检测、同会话隔离和安全撤销。
- CLI 变更查看、批准、拒绝、撤销和 Git diff 工作流。

### v1.1 目录与文件更新

```text
capslock/
  changes.py         # 新增：提案、审批、应用与撤销服务
  policy.py          # 更新：受控写路径、内部目录和文件类型限制
  session.py         # 更新：changes 持久化表与查询
  tools.py           # 更新：propose_file_* / apply_change / discard_change
  runtime.py         # 更新：编辑提案指令与 ChangeService 上下文
  cli.py             # 更新：/changes、/approve、/reject、/undo、/diff
tests/
  test_changes.py    # 新增：编辑审批、冲突、撤销与隔离
```

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

### v1.2 新增功能

- 受控测试、构建和只读格式检查命令模板；禁止模型传入自由 Shell。
- 命令逐次审批、cwd/超时/输出限制、进程组取消和运行审计。
- 会话任务状态、上下文压缩、token/费用统计与诊断中的命令模板发现。

### v1.2 目录与文件更新

```text
capslock/
  execution.py       # 新增：命令模板、审批和进程执行
  config.py          # 更新：命令超时、输出及费用配置
  session.py         # 更新：commands、tasks、费用与摘要存储
  tools.py           # 更新：命令与任务状态工具
  runtime.py         # 更新：usage、成本和上下文摘要注入
  cli.py             # 更新：/commands、/tasks、/cost、命令审批
tests/
  test_execution.py  # 新增：命令审批、超时、截断和成本
```

### 验证

- 覆盖命令未经批准拒绝、成功执行、超时、输出截断、cwd/会话隔离，以及 Agent 工具不能绕过审批。
- 覆盖任务状态、token/费用归档与上下文压缩；当前测试套件共 17 项。

## v1.3：受控 Web 研究、MCP、权限系统与 CLI UI

### 版本目标与边界

v1.3 在 v1.2 的受控文件编辑、固定命令执行、会话恢复和审计能力上，补齐 Agent 获取外部信息和接入本地工具生态的能力。核心原则仍然是：外部动作必须可识别、可评估、可追溯，并由当前权限模式决定是否需要用户确认。

本版本支持 Tavily 搜索、公开网页抓取和本地 stdio MCP；不支持任意 URL/请求头、远程 MCP、OAuth、浏览器自动化、插件自动安装、任意 Shell、后台常驻连接或无人值守任务。第三方 MCP 工具可能产生无法自动撤销的外部副作用，因此即使允许执行，也必须保留风险说明和审计轨迹。

### 开发结果概览

- 建立统一外部动作状态流：`pending → approved/rejected → running → completed/failed`，Web 与 MCP 共用提案、审批、会话隔离和事件审计机制。
- 增加 Tavily 搜索、受 SSRF 保护的 URL 抓取、来源归档、提示注入标记以及 `[[source:<id>]]` 外部来源引用。
- 增加项目共享与本机私有双层 MCP 配置，通过 Python MCP SDK 按需启动 stdio server、发现工具并调用允许名单中的工具。
- 增加 `full_access`、`approve_for_me`、`ask_for_approval` 三档权限，统一评估读、写、执行、网络和 MCP 风险，并保存工作区级权限选择。
- 重构 CLI UI：加入启动卡片、语义化透明主题、独立输入区域、实时斜杠命令补全、权限状态展示、编号审批菜单和跨终端降级。
- 测试从早期 23 项扩展到当前 45 项，覆盖 v1.0–v1.3 回归、真实临时 MCP server、权限、CLI 补全、主题色深和退格刷新。

### 新增功能详解

#### 1. 通用外部动作与恢复闭环

`ExternalActionService` 将 Web 搜索、网页抓取、MCP 连接和 MCP 调用统一保存为 `external_actions`。动作包含稳定 ID、会话和运行 ID、类型、脱敏载荷、摘要、状态、结果或错误以及各阶段时间。

模型只能调用 `propose_web_search`、`propose_web_fetch`、`propose_mcp_connect` 和 `propose_mcp_call` 创建动作。执行前再次校验动作属于当前会话且状态允许，重复执行、跨会话 ID 和未批准执行都会被拒绝。Web 动作完成后，CLI 会让 Agent 读取已保存来源并自动继续原问题；拒绝、失败或选择稍后处理不会伪造外部结果。

```text
模型提出外部动作
  └─ 风险评估与权限判定
      ├─ 需要确认 → CLI 展示载荷 → 批准 / 拒绝 / 稍后
      └─ 可自动批准 → 写入风险审计
           └─ running → completed / failed
                ├─ Web：保存来源并继续 Agent 回答
                └─ MCP：显示安全结果摘要
```

#### 2. Web 研究与外部来源

- Tavily 使用固定 `https://api.tavily.com/search` endpoint，密钥来自 `CAPSLOCK_TAVILY_API_KEY` 或 `TAVILY_API_KEY`，模型不能指定自定义 endpoint、请求头或授权信息。
- 搜索最多保存 8 条结果，记录排名、标题、URL、摘要、抓取时间和稳定 `source_id`。
- URL 抓取只允许带 hostname 的公开 `http/https` 地址；请求前解析 DNS，并拒绝 localhost、私网、链路本地、保留地址和未指定地址。
- 每次重定向都重新执行公开地址校验；重定向次数、响应字节数、超时和内容类型均受配置限制。
- 只接受 `text/html` 与 `text/plain`。HTML 会移除脚本、样式和 noscript 内容后提取正文，超限响应被截断并明确标记。
- 外部正文始终以不可信数据处理；检测 `ignore previous instructions`、`system prompt`、伪造 tool call 等典型提示注入特征并标记 `suspicious`，但不会把页面内容解释为授权。
- `/sources` 可回查来源，最终回答可同时包含本地 `[[evidence:<id>]]` 和外部 `[[source:<id>]]` 引用。

#### 3. 本地 stdio MCP

- `capslock.mcp.json` 是可提交的项目声明，包含 server 名称、命令、参数、相对 cwd、描述、启用状态和 `allowed_tools`，禁止写入 `env` 或凭据。
- `.capslock/mcp.local.json` 是本机私有覆盖，可保存本机路径、环境变量和启用状态。项目层和本机层同时声明允许工具时取交集，本机层不能扩大项目权限。
- server cwd 必须通过 `WorkspacePolicy` 校验；子进程使用参数数组而非 shell，环境只保留最小 `PATH` 和本机私有层显式声明的变量。
- MCP 连接与工具调用都是外部动作。执行时通过 Python MCP SDK 完成 `initialize`、`tools/list` 或 `tools/call`，并限制初始化/调用超时和序列化输出大小。
- `mcp_connect` 只返回允许名单中的工具；`mcp_call` 在提案和执行两个阶段都重新验证工具白名单。
- 初始化异常、协议错误、超时、崩溃或超限输出会转为失败动作并写入审计，不会把异常进程留作后台常驻服务。

#### 4. 三档权限与风险兜底

| 模式 | 自动执行范围 | 需要确认 | 兜底策略 |
| --- | --- | --- | --- |
| `full_access` | 所有已注册动作按策略自动批准 | 无逐次确认 | 仍保留风险事件、文件快照与 `/undo`、命令超时/取消、SSRF、MCP 白名单和输出限制 |
| `approve_for_me` | 只读和低/中风险动作；Web 网络动作按当前风险规则处理 | 文件写入、命令和 MCP 等高风险动作 | 默认模式；每个高风险动作单独展示并确认 |
| `ask_for_approval` | 无 | 每条用户请求以及随后产生的每个动作 | 拒绝后请求不发送或动作不执行 |

`permissions.py` 为动作提供风险等级、原因和回滚建议。文件变更可以使用原始内容快照和 `/undo`；命令使用固定模板、独立进程组、超时、输出限制和 `/diff` 检查；Web 不修改本地文件但保留来源与请求审计；MCP 对无法撤销的第三方副作用明确提示。`/permissions` 提供编号选择，`/permissions full|approve|ask` 支持直接切换，结果保存到 SQLite `workspace_settings` 并在后续会话恢复。

#### 5. CLI UI 更新

v1.3 后期对 CLI 做了独立的一轮交互与视觉重构，目标是让输入区、历史输出、审批动作和系统状态形成稳定层级，并在 VS Code Terminal 与 macOS Terminal 中保持一致。

- 启动界面：新增 CapsLock 符号字标、版本、模型、权限模式、工作区和快速提示卡片；窄终端自动使用紧凑布局。
- 输入区域：使用整行分隔线将当前输入和历史记录分开，提示符简化为 `❯`，当前权限在右侧实时显示，底部显示 `/help`、方向键和 Tab 提示。
- 实时命令字典：输入 `/` 后立即显示“命令 + 功能说明”双列候选；继续输入按完整前缀过滤，候选固定在终端左侧，不跟随光标横向漂移。
- 键盘交互：支持 `↑/↓` 选择、Tab 补全；Backspace、Ctrl-H 和 Delete 修改输入后会显式重新计算候选，删除到非斜杠输入时关闭菜单。
- 输入高亮：斜杠命令由 lexer 以粗体命令色渲染，普通用户输入使用独立语义样式，不再依赖手写 ANSI 重绘。
- 审批界面：权限选择和高风险动作使用稳定的编号菜单；外部动作展示 ID、种类、脱敏载荷和摘要，避免把未显示的操作当作已授权。
- 结果展示：变更、命令、任务、来源和 MCP 状态统一使用透明表格、语义色和状态文本；不覆盖终端背景。
- 主题系统：新增透明蓝灰语义主题，Rich 与 prompt-toolkit 共用同一组颜色 token；支持 truecolor、ANSI-256 和 `NO_COLOR`，不同终端实例使用独立主题以避免色深缓存污染。

CLI 斜杠指令新增或扩展如下：

| 指令 | v1.3 行为 |
| --- | --- |
| `/permissions` | 打开三档权限编号菜单；切换后提示符立即更新并持久化 |
| `/web` | 查看当前会话 Web 搜索和抓取动作 |
| `/sources` | 查看标题、URL、来源 ID 和不可信/可疑标记 |
| `/mcp list` | 查看合并后的项目/本机 MCP server |
| `/mcp status <server>` | 查看 server scope、cwd、启用状态和工具白名单 |
| `/mcp tools <server>` | 查看允许调用的工具 |
| `/approve <id>` | 批准文件变更、命令或外部动作 |
| `/reject <id>` | 拒绝待处理动作且不产生执行副作用 |

### 开发流程记录

1. **抽象动作状态**：先从 Web/MCP 的共同需求提取 `ExternalActionService`，在 SQLite 中建立动作和来源表，完成会话隔离、状态迁移和事件记录。
2. **实现 Web 安全边界**：接入 Tavily 固定请求，随后实现 URL 协议、DNS/IP、重定向、内容类型、超时和大小限制，再加入正文提取与提示注入标记。
3. **实现 MCP 配置合并**：先完成项目/本机配置解析与权限交集，再接入 Python MCP SDK，并用临时 stdio server 验证初始化、工具发现和调用。
4. **接入 Agent 工具循环**：将 Web、MCP 和来源查询加入 ToolRegistry，扩展运行时指令与引用验证，并实现 Web 审批完成后的自动续答。
5. **统一权限策略**：把文件、命令、Web、MCP 动作映射为风险等级，加入自动批准、逐次批准、风险事件和回滚说明，并持久化工作区权限模式。
6. **重构 CLI UI**：先补齐 `/web`、`/sources`、`/mcp` 和 `/permissions`，再由基础 Rich 输出迁移到 prompt-toolkit 输入层，逐步解决实时补全、前缀过滤、退格刷新、菜单定位、主题透明和跨终端色深问题。
7. **补齐回归测试与文档**：增加 HTTP mock、临时 MCP server、权限自动应用、CLI 恢复、命令补全、主题和启动界面测试，并同步工具与指令参考。

### 配置与持久化变化

`Settings` 新增 Tavily key、Web 超时、抓取字节上限、重定向上限、MCP 超时、MCP 输出上限和默认权限模式。环境变量继续高于 `capslock.toml`，密钥不写入项目配置。

SQLite 新增或扩展：

- `workspace_settings`：保存工作区权限模式。
- `external_actions`：保存 Web/MCP 提案、审批、执行状态、结果摘要和错误。
- `sources`：保存外部 URL、标题、摘要、抓取时间和提示注入标记。
- 既有 runs/messages/events：继续记录模型运行、自动续答、风险评估和脱敏事件。

### v1.3 目录与文件更新

```text
capslock/
  external.py         # 新增：Tavily、URL 抓取、来源、SSRF 与外部动作
  mcp.py              # 新增：双层 MCP 配置、stdio 初始化、发现与调用
  permissions.py      # 新增：三档权限、风险等级与回滚建议
  theme.py            # 新增：Rich/prompt-toolkit 共享透明语义主题
  config.py           # 更新：Tavily、Web、MCP 和权限配置
  session.py          # 更新：workspace_settings、external_actions、sources
  tools.py            # 更新：Web、MCP、来源工具与权限自动审批
  runtime.py          # 更新：不可信外部内容、来源引用与权限上下文
  observability.py    # 更新：外部动作与风险事件脱敏
  cli.py              # 更新：启动卡片、实时命令列表、权限/Web/MCP 指令与审批 UI
capslock.toml.example # 更新：Web/MCP/权限运行时配置示例
capslock.mcp.json     # 用户可选：项目共享 MCP 配置
.capslock/
  mcp.local.json      # 用户可选：私有 MCP 覆盖、路径与环境变量
tests/
  test_external.py    # 新增：Web、SSRF、来源、MCP 配置与真实 stdio server
  test_cli_external.py # 新增：外部动作 CLI 审批与 Web 自动续答
  test_permissions.py # 新增：权限、菜单、命令补全、退格刷新与启动 UI
  test_theme.py       # 新增：透明主题、色深降级与 NO_COLOR
docs/
  agent-reference.md  # 新增：当前 Agent 工具、权限和 CLI 指令参考
```

`pyproject.toml` 新增 `mcp` 与 `prompt-toolkit` 运行时依赖；前者负责 stdio MCP 协议，后者负责稳定的实时输入、补全与高亮。

### 安全边界与已知限制

- Web 只支持 Tavily 搜索和公开 HTTP(S) 文本抓取；DNS rebinding 的完整网络层隔离、浏览器渲染和下载文件不在 v1.3 范围。
- MCP 只支持显式配置的本地 stdio server；不支持远程 transport、OAuth、自动安装或跨项目全局授权。
- `full_access` 取消交互确认，但不关闭路径、SSRF、命令模板、MCP 白名单、超时、输出上限、审计或回滚保护。
- Web 来源和 MCP 输出始终是不可信数据，不能通过提示注入改变系统指令、权限模式或工具白名单。
- MCP 工具的外部副作用无法保证撤销；日志只提供动作、参数摘要、结果和错误追踪。

### 测试与验收

当前 45 项测试覆盖：

- Tavily 无 key 拒绝、请求归档、来源保存和 Web 工具只提案。
- URL 私网/localhost/链路本地拒绝、HTML/文本提取、截断、重定向和提示注入标记。
- MCP 项目/本机配置合并、项目凭据拒绝、工具白名单、未批准调用拒绝和真实临时 stdio server。
- 外部动作批准、拒绝、跨会话隔离、失败状态和 Web 完成后的 Agent 自动续答。
- 三档权限、别名解析、工作区持久化、`full_access` 自动应用、风险与审计事件。
- CLI 命令前缀过滤、双列菜单、左侧定位、斜杠高亮、Backspace/Delete 候选刷新、权限右侧状态和启动卡片。
- Rich truecolor/ANSI-256/`NO_COLOR` 降级、透明背景，以及 v1.0–v1.2 的路径、证据、会话、编辑和命令执行回归。

手工验收路径：启动 `capslock chat`，输入 `/` 检查实时命令列表；通过 `/permissions` 切换并确认右侧状态；让 Agent 提出 Web 搜索并检查来源续答；配置临时 stdio MCP server，批准连接与调用；最后使用 `/sources`、`/web`、`/mcp status` 和事件日志核对完整轨迹。

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
