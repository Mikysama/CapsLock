# CapsLock

CapsLock 是一个本机工作区 Agent，用于读取和修改代码、检索证据、运行受沙箱保护的 Shell、查询代码语义，以及按审批策略访问 Web、MCP 和本地插件。Tool Runtime v2 将工具契约、参数级策略、可恢复暂停、调度、富结果与审计统一到异步执行链。

当前源码版本为 `2.3.1`。本次补丁统一优化 inline/fullscreen 上下文用户消息样式，并修复 fullscreen 空闲动画持续重绘造成的终端高 CPU 与卡顿。Tool Runtime v2、config 5、workspace schema 8、memory schema 3 和 plugin protocol 4 均保持兼容。架构与部署边界见 [当前开发者文档](docs/development/v2/current.md)，发布摘要见 [2.3.1 发布说明](docs/releases/v2.3.1.md)。

正式支持矩阵：Linux/macOS，Python 3.12。发布 CI 会在两个操作系统组合中执行测试、构建、依赖审计和安装冒烟。

## 安装

需要 Python 3.12 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

运行时依赖包括 `aiosqlite`、`openai`、`httpx`、`rich`、`prompt-toolkit`、`textual`、`mcp`、`PyYAML`、`tomlkit` 和 `keyring`。本地 FastEmbed 是可选能力：

```bash
python -m pip install -e '.[local-embeddings]'
```

首次使用建议运行初始化向导；脚本和 CI 使用非交互模式：

```bash
capslock init
capslock init --non-interactive --base-url https://api.deepseek.com \
  --model deepseek-v4-flash --credential env:CAPSLOCK_API_KEY
```

凭据可引用启动环境或操作系统安全存储。`credentials set` 从隐藏输入读取，也可通过 stdin 自动化；密钥不会写入配置：

```bash
export CAPSLOCK_API_KEY=your_api_key
capslock credentials set primary
```

## 使用

TTY 中直接启动 TUI：

```bash
capslock
capslock resume
capslock resume <session-id-or-prefix>
# 保留的第一版 Textual 全屏界面
capslock --ui fullscreen
```

默认 `inline` 界面使用 prompt-toolkit/Rich 在普通命令行主缓冲区中输出，
不进入 alternate screen；终端原生 scrollback 可查看完整对话，退出时保留已
渲染内容，背景始终沿用终端默认值。第一版 Textual 全屏界面通过
`--ui fullscreen` 或 `CAPSLOCK_UI=fullscreen` 启用；命令行参数优先于环境变量。

脚本和 CI 使用 `exec`。无 prompt 时从 stdin 读取：

```bash
capslock exec "检查当前发布状态"
printf '%s\n' "总结最近的改动" | capslock exec --json
```

裸 `capslock` 在非 TTY 环境会返回错误并提示使用 `capslock exec`。

主要顶层命令：

- `capslock [--ui inline|fullscreen] [--no-spinner|--quiet]`：启动 TUI；默认 `inline`。
- `capslock exec [PROMPT] [--json] [--no-spinner|--quiet] [--max-tool-rounds N] [--max-tool-calls N] [--max-duration-seconds N] [--max-tokens N] [--max-budget-usd N]`：按硬预算执行一次请求。
- `capslock resume [SESSION] [--ui inline|fullscreen]`：恢复 TUI 会话。
- `capslock sessions ...`（也可写作 `capslock session ...`）：列出、搜索、重命名、归档、导出或删除会话。运行 `capslock session delete` 时可用方向键选择会话、回车确认选择，再输入 `y` 永久删除；输入 `n` 返回会话列表。
- `capslock input list|answer|cancel`：列出、回答或取消非交互运行留下的持久化用户输入请求。
- `capslock init`、`config validate`、`credentials status|set|delete`：初始化、配置和凭据治理。
- `capslock backup create|list|verify|restore`：本机状态回滚快照。
- `capslock export` / `capslock import`：创建或安全合并 portable 数据包。
- `capslock doctor [--json|--strict|--network|--fix]`：检查配置、凭据、数据库、MCP、Skill 与生命周期 journal。

TUI 保留以下命令：

```text
/help /status /permissions /approvals /queue /memory /skills /agents
/model /sources /mcp /diff /undo /rename /exit /quit
```

队列、任务、上下文和费用汇总到 `/status`。动作越过当前权限边界时，TUI 会在同一个 run 内阻塞，显示动作类型、风险、目标及最多 40 行/4 KiB 的脱敏命令或 diff 预览，然后给出默认拒绝的选择框；原始参数、完整文件内容和凭据不会进入预览。批准或拒绝的最终状态会返回模型继续推理，不再先结束为 `waiting_approval`。取消选择、EOF 和 Ctrl-C 均按拒绝处理。`/approvals` 仅处理非交互 `exec`、portable import 或旧数据留下的待审批动作。裸 `/permissions` 使用方向键选择权限模式，显式 `/permissions approve|ask|full` 仍可快捷切换。portable import 恢复的 queued work 只会在 `/queue start <id>` 后进入前台 worker，旧批准必须重新确认。旧的 `/cost`、`/context`、`/tasks`、`/changes`、`/commands`、`/web`、`/approve` 和 `/reject` 不再解析。`/exit` 与 `/quit` 均可退出 TUI。

输入 `/model` 可用方向键在 `deepseek-v4-flash` 与 `deepseek-v4-pro` 之间选择，也可直接执行 `/model <name>`。选择仅作用于当前 session 并随 session 恢复；新 session 仍采用配置默认模型。活跃 run 期间不能切换，队列中尚未开始的请求会使用切换后的模型。

inline TUI 将原 full-screen 设计系统映射到终端主缓冲区：用户、回答和系统消息保留语义化左边框，Queue、Activity、会话元数据、用量和 Composer 组成一个带完整边框的普通 inline prompt block。该输入块不锚定窗口底部，而是始终出现在最新上下文之后，并随新输出向下移动。reasoning 默认折叠成一行摘要，`Ctrl-O` 可切换当前及后续活动的详细显示；`◆ CapsLock` 下的回答使用透明背景的 Rich Markdown、代码高亮、表格和终端链接。连续读取/搜索工具合并为一条 `Explored` 摘要，编辑、命令和失败结果单独突出。状态信息按 `>=100`、`72–99`、`<72` 三档隐藏次要元数据；模型思考、读取文件和工具执行期间显示动态状态，回答开始流出后不再重复显示 Thinking。`Ctrl-J` 插入换行，`Enter` 提交，`Ctrl-C` 取消当前 run 或在空闲时退出。可通过 `--no-spinner`、`--quiet`、`CAPSLOCK_NO_SPINNER=1` 或 `CI=true` 禁用动态状态。启动 banner 保留 v1.7.1 的 `Welcome back`、CapsLock 字符画和 Tips 布局。

保留的 fullscreen TUI 使用 Textual 的固定输入区和可滚动 transcript，并进入
终端 alternate screen。最终回答按
Markdown 渲染，reasoning 与连续只读/搜索工具在完成后折叠；`Ctrl-O` 展开详情，
`Ctrl-R` 搜索输入历史，`Tab` 补全 `/` 命令或 `$` Skill，`Ctrl-J` 插入换行。
输入 `/` 后，命令候选按纵向逐行显示，完整命令集可滚动浏览，并自动跟随
`↑/↓` 当前选中项。
`/model` 等需要模态选择的命令在独立 Textual worker 中等待结果，不会阻塞
界面消息泵。
活跃 run 中按 `Ctrl-C` 取消，空闲时按 `Ctrl-C` 退出。审批面板展示经过脱敏和
截断的动作摘要、命令或 diff，并始终默认拒绝。终端小于 48×14 时只显示尺寸
提示，不允许进行审批。

inline 与 fullscreen 的根层仍使用终端原生默认背景，仅上下文中的用户 prompt 行
使用浅灰背景并与左侧蓝色标记对齐；其余容器、消息、输入区和模态框保持透明。
Markdown、代码高亮和输入光标行只移除字符背景，不改变前景色、粗细、斜体、
下划线或链接样式。

`capslock resume` 使用方向键选择历史 session 并重放完整可见对话；也可显式传入完整 session ID 或唯一前缀。已完成消息以及中断/失败 run 的用户问题和已产生文本都会进入恢复视图与后续模型上下文。

## JSONL

`capslock exec --json` 每行输出一个事件，字段固定为：

```json
{
  "schema_version": 3,
  "sequence": 1,
  "event_id": "evt_...",
  "trace_id": "trace_...",
  "timestamp": "2026-01-01T00:00:00+00:00",
  "session_id": "...",
  "work_item_id": "...",
  "run_id": "...",
  "event": "completed",
  "status": "completed",
  "terminal": true,
  "data": {}
}
```

每个 run 只产生一个终止事件。终止类型为 `completed`、`waiting_approval`、`waiting_input`、`failed`、`cancelled` 或 `stopped`。完成事件携带 answer、citations、memory recalls、usage 和 duration；等待审批事件携带 action IDs；等待输入事件携带 input request ID；失败、取消与停止事件携带稳定的 error code 和 message。

`thinking.data.text` 只承载模型提供方返回的 reasoning；`text_delta.data.text` 与 `completed.data.answer` 承载面向用户的最终回答。两类文本不会互相拼接。

退出码：成功 `0`，运行错误 `1`，调用或状态错误 `2`，等待审批 `3`，预算/循环停止 `4`，等待用户输入 `5`，用户取消 `130`。

## 权限与动作

默认模式为 `approve_for_me`：高风险文件、命令和 MCP 动作需要确认，Web 动作仍经过校验与审计。另有 `full_access` 和 `ask_for_approval`：

```text
/permissions full
/permissions approve
/permissions ask
```

直接输入 `/permissions` 会打开三档权限选择框。交互 TUI 中需要确认的动作会原地等待选择；批准后立即执行并把结果返回当前 run，拒绝后不会产生副作用。非交互 `exec` 无法弹出选择框，因此仍以 `waiting_approval` 和退出码 `3` 结束。

所有动作共用 `pending -> approved -> running -> completed|failed|cancelled` 状态机。拒绝从 `pending` 进入 `rejected`。Coordinator 负责风险、审批、状态和审计；handler 负责文件、命令、Web 或 MCP 的校验与执行。

- 文件动作在提案和执行时校验路径、内容与哈希，且支持安全 `/undo`。
- Shell 通过确定性规则、结构化权限和 OS 沙箱执行；默认断网，越界、联网或宿主执行必须单独授权，取消使用 TERM→KILL 收尾。
- Web 只访问公开 HTTP/HTTPS 地址，拒绝私网、重定向越界和非文本响应；来源始终是不可信数据。
- MCP 只使用显式配置的本地 stdio server 和工具 allowlist。
- 本地工具插件必须显式安装和逐工作区启用；安装、升级、权限变化和卸载均展示内容摘要与 capability 并记录审计。插件默认在 OS sandbox 中运行，通过宿主 broker 请求受限能力；没有 sandbox backend 时拒绝执行。

## 本地工具插件

插件包是包含 `capslock-plugin.toml` 和包内入口程序的本地目录。manifest、stdio protocol 与 workspace grant 使用协议 4，capability 必须声明工作区路径、网络 host、进程模板或 credential 名称。CapsLock 不支持在线源、自动依赖安装、市场、UI、模型 Provider、用户可执行 Hook 或退出后常驻 daemon。

```text
capslock plugin install ./my-plugin --yes
capslock plugin enable my-plugin --yes
capslock plugin enable my-plugin --trusted-native --yes  # 仅在明确接受无 sandbox 风险时
capslock plugin list
capslock plugin show my-plugin
capslock plugin verify my-plugin
capslock plugin disable my-plugin --yes
capslock plugin uninstall my-plugin --yes
```

插件安装到 `${CAPSLOCK_HOME:-~/.capslock}/plugins/`，工作区授权保存在 `.capslock/local/plugins.json`。模型可见工具名使用 `plugin__<plugin>__<tool>`，每次调用继续经过外部动作审批、超时、取消、脱敏和审计链。`trusted-native` 必须逐工作区显式确认，且每次调用仍要求人工审批。

## 多 Agent 协作

父 Agent 可通过 `delegate_agents` 一次委派最多四个本机子任务，默认最多并行两个。子 Agent 只有一层，使用排除 `.git`、`.capslock`、环境凭据和符号链接的私有快照；子数据库、session 和记忆上下文不会与父运行共享。

子任务默认只有只读类工具，文件访问仍必须命中任务契约的路径 allowlist，空 allowlist 不授予文件访问。文件写入、Shell、Web 和 MCP 必须在任务契约中逐项声明；子工具目录不包含 `delegate_agents`，也不自动包含工作区插件。自由文本、证据和产物均是不可信数据，只有通过路径、schema、实际检查状态和 SHA-256 校验的输出才返回父 Agent。

```text
/agents
/agents inspect <task-id>
/agents cancel <task-id>
/agents cleanup <task-id>
```

`/status` 同时显示子任务总数、并发占用、聚合用量、等待审批数和失败/中断数。成功子任务会先以快照基线保护父工作区并复制已验证产物，再清理私有工作区；失败、取消和验证失败的目录保留到显式 cleanup。

## 记忆

记忆分为 `global`、`workspace` 和 `session` 作用域。identity 保存在 `memories`，内容写入不可变的 `memory_revisions`。`forget` 与 `undo` 通过新 revision 完成；`purge` 删除正文、FTS、向量和来源，仅保留无正文 identity 与审计。

常用命令：

```text
/memory list [global|workspace|session]
/memory search <query>
/memory show <id>
/memory add
/memory forget <id>
/memory undo <id>
/memory purge <id>
/memory candidates [--all]
/memory candidate accept|reject|purge|show <id>
/memory export <scope> <path.json>
/memory import <scope> <path.json>
/memory policy off|review|automatic
/memory embeddings enable fastembed
/memory embeddings enable local-http <endpoint> <model>
/memory embeddings enable external <model-profile>
/memory embeddings disable
/memory embeddings rebuild
```

召回保持 4 KiB、最多 5 条的限制，并按词法/语义相关性、作用域、置信度、时效和来源排序。FastEmbed 在工作线程执行；本地 HTTP embedding 使用 `AsyncClient` 且只允许回环地址。

记忆导入导出格式固定为 `capslock-memory-export` version 3，只接受 version 3。会话导出格式为 version 3，并包含 run 治理快照与停止原因。v1/v2 记忆导出和旧会话导出不提供导入或转换。

## Skill

工作区 Skill 位于 `.capslock/skills/<name>/`，用户 Skill 位于 `${CAPSLOCK_HOME:-~/.capslock}/skills/`。每个包必须包含与目录同名的 `SKILL.md`：

```markdown
---
name: workspace-summary
description: Summarize a workspace area using local evidence.
---

Inspect relevant files and return an evidence-backed summary.
```

可附带 `references/`、`assets/` 和 `scripts/` 只读资源。Skill 不能声明额外权限、hook 或任意脚本执行入口。使用 `$skill-name [arguments]` 显式调用；管理命令为 `/skills list|show|validate|enable|disable`。

## 配置

配置根必须包含 `config_version = 5`。config v3/v4 会在原子备份后自动迁移；其他非当前格式拒绝加载。多模型使用 provider、credential reference、profile 和角色路由：

```toml
config_version = 5

[providers.primary]
kind = "openai_compatible"
base_url = "https://api.deepseek.com"
credential = "env:CAPSLOCK_API_KEY"
timeout_seconds = 60
data_policy = "primary-provider"

[providers.backup]
kind = "openai_compatible"
base_url = "https://api.example.com/v1"
credential = "keyring:backup-model"
data_policy = "primary-provider" # 只有相同策略才允许自动降级

[models.main]
provider = "primary"
model = "deepseek-v4-flash"
context_window = 128000
max_output_tokens = 8192
input_cost_per_million = 0
output_cost_per_million = 0

[models.backup]
provider = "backup"
model = "compatible-chat-model"
context_window = 128000
max_output_tokens = 8192
input_cost_per_million = 1
output_cost_per_million = 2

[routing]
reasoning = ["main", "backup"]
fast = ["main"]
embedding = ["backup"]
vision = []

[budget]
max_run_tokens = 100000
max_run_usd = 2.0
max_session_usd = 10.0

[runtime]
max_tool_rounds = 32
permission_mode = "approve_for_me"

[tools]
schema_budget_tokens = 8000
max_read_concurrency = 4
aggregate_result_bytes = 65536

[shell]
enabled = true
default_timeout_seconds = 120
max_timeout_seconds = 600
classifier_enabled = true
classifier_threshold = 0.95
background_enabled = true
output_bytes = 100000

[context]
auto_compact = true
trigger_ratio = 0.80
target_ratio = 0.60
preserve_recent_turns = 6
inline_tool_result_bytes = 16384
summary_max_tokens = 2048
max_compaction_failures = 3

[agents]
enabled = true
max_children = 4
max_concurrency = 2
max_depth = 1
max_child_tool_rounds = 16
background_enabled = true

[lsp]
enabled = true
startup_timeout_seconds = 10
request_timeout_seconds = 15
idle_timeout_seconds = 300

[documents]
max_pdf_bytes = 52428800
max_pdf_pages = 10
max_notebook_bytes = 10485760
max_notebook_cells = 50
max_cell_output_bytes = 65536

[worktree]
enabled = true
max_per_session = 4

[loop_detection]
consecutive_repeats = 3
failed_retries = 3
cycle_repetitions = 3
max_cycle_length = 4

[command]
command_timeout_seconds = 120
command_output_bytes = 100000

[web]
web_timeout_seconds = 20
web_max_bytes = 500000
web_max_redirects = 3

[mcp]
mcp_timeout_seconds = 30
mcp_output_bytes = 100000

[memory]
enabled = true
```

`CAPSLOCK_HOME` 与 `CAPSLOCK_MEMORY_DATABASE` 必须是 shell 中的绝对路径。

## 数据与升级边界

CapsLock 只接受 canonical 布局：

- 工作区配置：`.capslock/config.toml`
- 工作区 MCP：`.capslock/mcp.json`
- 本机 MCP：`.capslock/local/mcp.json`
- 工作区数据库：`.capslock/state/capslock.sqlite3`
- 事件日志：`.capslock/state/events.jsonl`
- 用户记忆：`${CAPSLOCK_HOME:-~/.capslock}/state/memory.sqlite3`

工作区库和记忆库使用不同的 SQLite `application_id`。当前 workspace schema 为 8，memory schema 为 3；workspace schema v6/v7 在 WAL checkpoint 和 SQLite backup 后事务升级。旧 application ID、其他非当前 schema 或未知已有表均拒绝启动。

## 架构

运行内核以 `capslock.bootstrap` 为组合根，以 `capslock.ports` 隔离 runtime/tooling 与具体应用、SQLite 实现。每个 session 由 `RunEngine` 串行执行；token budget、tool artifact、事件耐久化、审批和收尾使用独立组件。

- `domain/`：session、workflow、action 和 memory 领域类型。
- `configuration/`：当前配置文档读取、校验与 typed settings。
- `storage/async_database.py`、`storage/schema.py`：单 writer、只读连接池与两套当前 schema。
- `storage/repositories/`、`storage/memory_repositories/`：组合式异步 repositories。
- `application/workflow.py`：原子 workflow 转换与审批结算。
- `application/action_system/`：动作协调器和四类 async handler。
- `runtime/`：异步模型协议、context、ToolLoop、event stream 与 Agent。
- `memory/`：生命周期、召回、候选、embedding、传输与校验服务。
- `tooling/`：统一 async tool registry 与 adapters。
- `plugins/`、`plugin_sdk/`：本地插件 manifest、注册、生命周期、stdio client 与公开 SDK。
- `cli/`、`cli/views/`：默认 inline TUI、JSONL 控制器和 typed view 渲染。
- `cli/fullscreen_tui/`：保留的 Textual App、状态 reducer、widgets、screens 和安全展示适配。

公开运行时入口只有 `AgentSession.run_stream(RunRequest)`；没有旧式聚合 Agent、同步包装或兼容别名。

## 验证

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
python scripts/check_repository.py
```

测试使用模拟模型、HTTP、MCP 和本地子进程，不需要真实 API 密钥。完整工具与 TUI 参考见 [Agent reference](docs/reference.md)。
