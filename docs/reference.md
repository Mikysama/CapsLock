# CapsLock Agent Reference

本参考描述当前模型工具、TUI 命令、审批边界与持久化契约。

## 稳定契约

CapsLock 2.7.6.4 支持 Linux/macOS 与 Python 3.12。当前协议为 `permissions_version = 2`、`config_version = 14`、workspace schema 21、memory schema 6、portable archive 8、session export 8、JSONL schema 3、IDE Bridge protocol 1 和插件 manifest/protocol/grant 4。config v3-v13、workspace schema v6-v20 与 memory schema v3-v5 使用 backup-first 自动迁移。模型 Provider 只使用 OpenAI Responses API，不兼容 Chat Completions。

公开运行入口为 `AgentSession.run_stream(RunRequest)`。CLI 通过应用查询面读取状态，不应依赖 repository 聚合对象。

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `full_access` | hard deny、强制安全确认和显式 deny/ask 仍生效；其余允许，不调用 Shell 分类器。 |
| `approve_for_me` | 默认模式。安全本地读取和受参数约束的确定性只读 Shell 自动允许；工作区写入、网络、后台进程、MCP/插件副作用默认询问。 |
| `ask_for_approval` | 显式 allow 可放行，其余每次调用询问；分类器不能自动放行。 |

使用 `/permissions` 打开三档权限选择框，或使用 `/permissions full|approve|ask` 直接切换。选择保存在工作区 settings repository。没有其他模式或别名。

固定判定顺序为：工具参数规范化与 capability 边界、hard deny/hard ask、显式 deny、显式 ask、显式 allow、模式默认。规则行为相同时按具体度、`session > local > project > user` 和稳定规则 ID 选择解释来源。批准不能扩大父 Agent、MCP 或插件已有 capability grant，也不能绕过沙箱。

权限文件使用 `permissions_version = 2` 和 `[[rules]]`。文件规则的 path 是工作区相对 POSIX glob；Shell 规则支持 `command`、`command_prefix`、`cwd`、`sandbox`、`network`，动态展开、不可靠复合命令及危险重定向不命中 allow；Web 使用规范化 IDNA host 和 operation；MCP 使用精确 server 与 `mcp_tool`。未知 constraint、遍历、NUL、无效网络 scope 均关闭授权。项目 ask/deny 立即生效，项目 allow 必须通过 `/permissions trust-project` 信任当前文件 SHA-256，文件变化后自动退化为 ask。

审批提供 `approve_once`、`approve_session`、`approve_local` 和 `reject`。一次性 grant 绑定 session、run、invocation、工具名及规范化参数 SHA-256，并只能消费一次。session 规则随 session 删除；local 规则原子写入 `.capslock/local/permissions.toml`，拒绝符号链接并保留未知 TOML 内容。普通工具请求和 Action 都可在重启后恢复，任何摘要变化、写入失败或规则遮蔽都不会执行。

## Plan Mode

Plan Mode 是独立的 session overlay，不是第四种 `PermissionMode`，也不改变 `RunMode`。`/plan [目标]` 直接进入；自然语言要求只规划时，模型使用 `enter_plan_mode(objective)` 创建持久化确认。状态为 `draft`、`awaiting_approval`、`approved`、`implementing`、`implemented`、`implementation_failed`、`rejected` 或 `cancelled`，数据库中的 plan/revision/request/implementation 记录是权威来源。

激活后，Planning boundary 在普通权限判定和 Shell 分类器之前执行。目录只暴露明确标记为 `local_read` 的本地读取工具、`ask_user` 及 `get_plan`、`update_plan`、`submit_plan`；Shell、Web、MCP、插件、Action、任务/记忆修改、worktree、后台进程和子 Agent 一律返回 `plan_mode_read_only`。`full_access`、显式 allow 和分类器都不能扩大该边界；暂停 invocation 恢复前也会重新查询数据库状态。

计划正文最多 256 KiB，拒绝空内容与 NUL，每次更新和提交都使用 revision SHA-256 乐观锁。提交审批固定为批准实施、反馈继续规划或拒绝退出；Esc 等同继续规划且不执行。批准会幂等创建新的 implementation work item，使用精确 revision Markdown 与 SHA-256，并恢复普通工具目录。计划批准不是权限 grant，实施调用仍经过底层权限模式与 Action 审批。

交互层使用专用的进入和提交对话框，而不是普通权限确认框。进入页解释探索范围并展示目标；提交页以 `Ready to code?` 为标题，内嵌完整 Markdown、revision、SHA-256 与 implementation run 将沿用的权限模式。Fullscreen 的 “No, keep planning” 会聚焦同页反馈框；Esc 返回无反馈的继续规划。状态栏以 `⏸ plan mode on · <status> · <permission>` 同时呈现 overlay 与底层权限。

Markdown 镜像位于 `.capslock/state/plans/<session-id>/<plan-id>.md`。数据库可在 show/open/resume 时重建镜像；`/plan open` 才会把编辑器内容显式导入为新 revision。ToolLoop 每轮将 active plan 的完整 revision 作为结构化快照注入上下文；Plan Mode 结束后，最新 plan（包括 `rejected`）继续以带状态的历史快照保留。历史快照明确是参考数据，不是指令、用户批准或实施权限，普通工具目录由真实 active 状态独立决定。branch/rewind 复制当前 revision 为子 session 的独立 draft。非交互 `exec` 遇到进入或提交审批时保留 `waiting_approval` 并返回退出码 `3`。

## 模型工具

所有工具通过同一个 `ToolRuntime` 调用。`ToolCatalog` 负责稳定元数据、动态发现和 schema 预算，`ToolExecutor` 负责校验、授权、执行、恢复与结果编码。

| 工具 | 功能 | 边界 |
| --- | --- | --- |
| `list_files` / `glob_files` | 浏览目录直接子项（文件和目录），或通过 ripgrep 按 glob 查找文件。 | `list_files` 使用 `offset`/`limit` 分页，默认 100、最大 1000；扫描受 `max_files` 限制。`glob_files` 默认 `*` 遵循 ignore；显式 glob 使用 rg 的覆盖规则，隐藏文件仍需显式启用。 |
| `read_file` / `read_image` | 读取文本或富图片内容。 | 只读；文件大小、类型、符号链接和路径受限。 |
| `search_files` | 使用 ripgrep 搜索文本并返回 Evidence。 | 只读；有结果上限、截断标记和明确的后端错误。 |
| `edit_file` / `write_file` | 精确片段编辑，或创建/完整替换文件。 | `write_file.expected_sha256=null` 断言文件不存在；替换必须携带读取所得 hash。保留 FILE_CREATE/FILE_EDIT 审批、diff 和 undo。 |
| `git_status` / `git_diff` | 查询 Git 状态或差异。 | 只读；不接受任意 Git 参数。 |
| `shell` | 在 OS 沙箱运行命令。 | Tree-sitter Bash AST、默认断网；自动批准只覆盖 Git 查询、`pwd` 与标准输入过滤器并只读挂载工作区，其他命令询问；动态语法/重定向 fail closed，危险命令 hard deny。 |
| `process_output` / `process_stop` | 管理 session 隔离的后台进程。 | 有界输出和 TERM→KILL 取消。 |
| `ask_user` | 创建可持久化结构化问题。 | 暂停同一 invocation，可跨进程回答。 |
| `enter_plan_mode` / `get_plan` / `update_plan` / `submit_plan` | 进入、读取、更新和提交当前 session 的计划。 | 主 Agent 专用；状态、归属、大小与 revision SHA-256 强校验。 |
| `create_task` / `list_tasks` / `update_task` | 管理任务与依赖关系；`list_tasks(task_id=...)` 精确读取单个任务。 | 始终返回 `tasks` 数组；精确查询不存在返回 `task_not_found`，`status` 仅用于列表筛选。session 隔离并拒绝依赖环。 |
| `read_pdf` / `read_notebook` / `edit_notebook` | 读取 PDF/Notebook 或编辑 cell。 | 有界读取；Notebook 编辑使用独立 Action。 |
| LSP 语义工具 | 定义、引用、符号、实现和调用层级查询。 | 仅已安装/配置 server；只读、禁网沙箱。 |
| `list_mcp_resources` / `read_mcp_resource` | 发现和读取 MCP Resources。 | server/URI 权限；二进制写 artifact。 |
| `mcp__<server>__<tool>` | 调用动态发现的 MCP 工具。 | 唯一受管理长连接路径，调用仍经 Action。 |
| `plugin__<plugin>__<tool>` | 调用获授权的本地插件工具。 | capability broker、沙箱、审批和结果脱敏。 |
| `web_search` / `web_fetch` | 搜索或抓取公开 Web 内容。 | SSRF、重定向、类型、大小和来源审计。 |
| Memory / Skill 工具 | 查询记忆或加载 Skill 快照。 | 作用域隔离，只读，不可信上下文。 |
| Worktree / Agent 工具 | 切换 session worktree，或创建 team/worker/DAG task、分配与恢复子 Agent。 | context mutation 独占执行，session ownership、contract digest、claim 和 checkpoint 强校验。 |
| `send_agent_message` / `read_agent_messages` / `ack_agent_message` | 与后台子 Agent 双向通信，或在 team 内点对点/广播。 | 发送显式指定 `target_type=task/agent/team` 和 `target_id`；task 要求 `kind`，team 要求 `broadcast=true`。保留归属、32 KiB、TTL、digest 和交付校验。 |
| `publish_agent_artifact` | 发布子 Agent 提议的单个产物。 | allowlist、大小、SHA-256 与父 snapshot baseline 再校验。 |

内置父 Agent 工具目录在 Shell、Worktree、Agent 功能均开启时有 **49 个公开工具**（不含动态 MCP、插件、LSP）。`agents.enabled=false` 时不注册 Agent 控制与委派工具。`stop_agent` 使用 `target_type=task/agent` 和 `target_id`，分别停止任务或 worker。

`create_file`、`get_task`、`stop_agent_task`、父 Agent 的 `send_team_message` 四个历史执行定义保留，用于旧会话/检查点恢复；它们不进入模型 schema、工具发现或候选列表。旧 `stop_agent(agent_id)` 和 `send_agent_message(task_id, kind, payload)` 参数仍可恢复。子 Agent 的受限 `send_team_message` 邮箱协议保持原状。权限仍按原 Agent 操作匹配；旧允许规则不会扩展到新增目标，相关旧名称上的 deny/ask 仍生效。文件创建与精确任务查询也保留旧入口的限制。

`list_files` 返回 `entries=[{path,type}]`、`count`、`offset`、`next_offset`、`truncated`、`stop_reason`，保留 `files` 字段（当前页的文件路径）。每页按路径排序；扫描最多 `max_files` 个目录项，达到扫描上限时返回 `stop_reason=scan_limit`，只对该有界快照分页。目录发生变化时应从 offset 0 重新读取；递归/模式查询使用 `glob_files`。

模型直接调用业务能力工具；需要副作用的工具由运行时创建 Action，统一 `ActionCoordinator` 决定是否等待批准或自动执行。TUI 为 Coordinator 安装阻塞式审批器：越过权限边界时显示动作类型、风险、目标，以及最多 40 行、4 KiB 的本机脱敏命令或 diff 预览，用户只能拒绝或执行且默认选择拒绝；原始参数、完整输出、文件正文和凭据不会进入展示事件。最终动作状态返回同一个模型工具调用，run 随后继续。非交互 `exec` 不安装审批器，仍保留 pending action、`waiting_approval` 终止事件和退出码 `3`。动作记录只使用 `request_json` 与 `result_json`，新增动作类型不需要 subtype 表。

## 工具契约与 artifact

`ToolContract`、`ToolDefinition` 和 `ResolvedToolPolicy` 声明输入/输出 JSON Schema、参数级只读/并发/破坏性属性、取消行为、capability、alias、intent tag、工具组与结果限制。`ToolCatalog` 保留动态发现的 last-known-good snapshot，单个无效 schema 只隔离对应工具；`ToolExecutor` 固定执行 normalize、validate、authorize、execute、输出校验和 middleware。连续的只读且并发安全调用使用有界并发执行，额度 reservation 与计数原子完成，checkpoint 仍按模型 tool-call 顺序写入。

内置工具的成功结果使用逐工具输出契约，覆盖必需字段、字段类型、数组元素及嵌套记录；不再使用通用 `object | array` 兜底。Action 的持久化结果封装有必需字段，具体 handler 的扩展结果、用户答案和自定义 metadata 保持可扩展；历史允许的空结果仍可读取。新增内置工具必须提供输出契约，缺失时注册报错。契约验证结构，不代表任务目标已达成。

`ToolExecutor.invoke()` 与 `resume()` 共用归一化、输入与业务校验、Plan Mode 边界、当前权限、超时/取消、输出校验和后处理管线；恢复只调用工具的 resume handler，不重新调用 execute。成功输出在后处理前后均校验，失败使用 `invalid_tool_output`，保留原始 data 和实际执行状态。后处理失败不会抹除已确认副作用，也不会触发参数修复重放。审批后 Action 结算仍沿用持久化恢复协议，不重复执行已完成动作。

`ToolOutcome.execution_state` 为 `not_started | committed | unknown`，旧 `executed` 保持兼容。只有确定未执行的名称或参数错误可进行一次模型修复；运行时不改写路径、命令、URL或业务值，第二次失败返回 `argument_repair_exhausted`。默认 `selection_mode=shadow` 仍发送完整工具集合并记录候选召回；`full` 可回滚，`filtered` 需通过评测门槛后启用。携带工具的请求要求 provider 显式设置 `strict_tool_calls=true`；摘要、Memory、Shell 分类和子 Agent 结果优先选择 `json_schema_outputs=true` 的 provider。若没有兼容候选，结构化正文会降级为由同一权威 Schema 生成的 Prompt 约束；返回后仍执行本地 Schema 与业务语义校验。严格工具调用能力缺失时仍返回 `provider_capability_unavailable`。

超过 16 KiB 的结果写入 `.capslock/state/artifacts/sha256/`，单项最多 5 MiB。模型只收到脱敏预览和 artifact ID；`read_tool_artifact` 只能分块读取当前 session 的 artifact，session 删除会级联清理记录与文件。消息、Tool Result 与文本 Artifact 同时写入 session-scoped episodic FTS；每轮自动回填最多 5 条/4 KiB，`search_session_history` 可显式检索最多 20 条。隔离的可疑 Artifact 不索引正文。

## 上下文预算

输入预算由模型 `context_window - max_output_tokens` 计算，并计入 system prompt、memory、episodic recall、显式 attachment、Skill catalog、工具 schema 与 checkpoint。缺少有效 provider usage 或轮次增长样本时，以 80% 比例作为软触发兜底（同时预留安全余量）。观测充分后，软门槛改为 `输入预算 - max(2048, context_window × 2%) - 最近 8 个非负轮次增长样本的最大值`，不再受 80% 限制；工具 schema 只计入输入一次。输出始终预留配置的 `max_output_tokens`，硬上限仍为输入预算。压缩目标不高于 60%，动态门槛降低时按原目标/触发比例同步降低以保持滞回。profile 切换清空增长观测；压缩后从压缩结果重新计算下一轮增长基线。`context/compaction_decision` 诊断记录策略、预计增长、输入估算及软硬门槛；自动、active-run checkpoint 与 `/compact` 共用同一管线。最近历史按完整 user turn/API-safe tool round 从尾部选择，始终保留最新完整 turn，并受 `preserve_recent_turns=6` 与 `preserve_recent_tokens=32768` 双重约束。预算不足时先移除 episodic recall，再减少非最新 recent turn；核心/运行时策略、仓库指令、当前输入、显式附件及最新 turn 不会为达成 target 而删除。

Provider 以结构化错误码或 HTTP 413 报告 context overflow 时，Runtime 仅在尚未输出 delta 的模型调用上强制执行一次 compaction，并用相同逻辑请求重试一次。强制模式跳过本地软触发阈值，但仍遵守 `auto_compact`、硬输入预算和三次失败熔断；第二次 overflow、无可压缩历史、固定上下文本身过大或自动压缩关闭时保留原错误。已经完成的 Tool 调用不会重放。诊断事件包括 `context_overflow_detected`、`context_overflow_recovery_started`、`context_overflow_recovery_succeeded` 和 `context_overflow_recovery_failed`。

旧 Tool Result 超过 `inline_tool_result_bytes=16384` 且 Artifact 持久化成功后才从模型上下文替换；失败保留原文并返回明确错误。摘要 v3 使用 `summary_max_tokens=2048` 作为 provider 输出与最终结果的硬上限，保存用户纠正、当前工作、代码符号、验证状态、逐项 `source_map` 和引用式 `working_set`。文件引用包含路径、SHA、行区间及 invocation ID，已加载 Skill 只记录名称和 digest，不跨 run 恢复正文。v1/v2 读取时仅在内存补齐 v3 默认字段，不重写旧记录。模型输出会校验并纠错一次；仍失败时生成 `degraded=true` 的确定性摘要及 `search_session_history`/`read_tool_artifact` 提示。超过 target 但低于 trigger 标记 `target_unreachable` 并继续，不在相同上下文中循环重压缩；只有超过硬输入预算或命中三次失败熔断才返回 `context_budget_exceeded`。

摘要请求按 entry 使用自适应 token 估算器装箱，估算覆盖 system、focus、history wrapper、JSON Schema 和纠错消息，并为摘要输出预留完整空间。超大 entry 按 token 预算切分且保留 source ID；Provider 仍报告 overflow 时仅二分失败 segment，最多四层，之后使用确定性 fallback。normal 与 slim policy digest 隔离 segment cache。`status=incomplete`、`max_output_tokens` 或等价 length 截断即使返回合法 JSON 也不会被接受。持久 compaction 先以 inactive candidate 生成，完整最终请求通过硬预算和进展校验后，才在一个 SQLite 事务中写入结果并切换 active pointer。

Composer 的 `@path[:line[-line]]` 仅在用户显式引用时读取工作区文本，最多四项、合计 64 KiB，并标记为不可信数据。启用 IDE Bridge 后，编辑器使用权限 `0600` 的 Unix socket descriptor 与随机 token 调用 JSON-RPC protocol 1；只有提示中的 `@selection` / `@diagnostics` 会展开最近上下文，路径仍受工作区私有文件边界限制。`CAPSLOCK_IDE=1` 可临时启用，持久配置使用 `[bridge]`。

## 远程 MCP 与本地追踪

MCP server 的 `transport` 可为 `stdio`、`streamable_http` 或 `sse`。远程 transport 必须使用解析到公开地址的 HTTPS URL，并受 `mcp.remote_enabled` 总开关控制；项目 `.capslock/mcp.json` 不允许 `env`/`headers`，私有 `.capslock/local/mcp.json` 中的 Authorization/Proxy-Authorization 只能写 `env:NAME` 或 `keyring:NAME` 引用。只有 `readOnlyHint=true` 的 stdio 调用可在断线后重连重试一次；写调用和远程调用发生不确定失败时返回 `unknown`，不自动重放。

本地 observability 只记录 category、name、status、duration 和经过脱敏的标量属性，不记录 prompt、工具参数或结果正文。`capslock trace list` 查看近期 span，`trace show <trace-id>` 查看单次 run，`trace summary` 聚合均值/最大值，`trace prune --days N` 清理；启动时还按 `[observability]` 的天数和总量上限裁剪。

## 插件隔离

插件 manifest、stdio protocol 与 workspace grant 都使用协议 4。grant 只能收窄 manifest capability；版本、digest 或 capability 改变会使授权失效。Linux 使用 Bubblewrap，macOS 使用系统 sandbox profile，默认不挂载 workspace/home 且断网。插件通过双向 stdio 向宿主 broker 请求文件、网络、固定命令和命名 credential；宿主重新执行路径、SSRF、审批、脱敏和审计策略。

没有 sandbox backend 时插件默认拒绝。`--trusted-native --yes` 是逐工作区高风险授权，不受 `full_access` 自动批准，每次调用仍需要人工确认。

## 动作状态

合法转换：

```text
pending -> approved -> running -> completed
   |           |          |-----> failed
   |           |          |-----> cancelled
   |           |---------> cancelled
   |---------> rejected
   |---------> cancelled
```

交互审批在 Action 工具调用内完成，因此批准执行或拒绝后由同一个 run 继续推理，不产生中间终止事件。非交互产生的待审批动作可通过 `/approvals` 结算；同一个 SQLite 事务更新 action、run、work item 和终止事件。重复结算已完成的 run 返回空结果，不会产生第二个终止事件。

文件执行前重新检查提案哈希；命令取消先向进程组发送 TERM，2 秒后仍未退出则发送 KILL；Web 跟随重定向前重新执行公开地址校验；MCP 在执行时再次检查工具 allowlist；插件在执行时重新检查安装摘要和工作区授权。

## TUI 命令

| 命令 | 功能 |
| --- | --- |
| `/help` | 显示命令。 |
| `/plan [goal\|show\|open\|submit\|exit]` | 进入、查看、编辑、提交或退出当前 session 的 Plan Mode。 |
| `/status` | 汇总 session、workspace、model、permissions、context、usage、tasks 和 queue。 |
| `/resume [session-id-prefix\|query]` | 选择或解析当前 workspace 的历史 session，并在关闭当前 Application 后切换。 |
| `/btw <question>` | 使用隔离的 FAST 工具循环回答临时问题；正文不进入 transcript 或 memory。 |
| `/compact [focus instructions]` | 压缩较早历史并原子更新当前 session 的 active compaction。 |
| `/new` | 关闭当前 Application 并创建空白 session。 |
| `/copy [N]` | 将最近第 N 条可见 assistant 回答复制到剪贴板。 |
| `/export [workspace-relative-path]` | 将当前 session 导出为 JSON 与 Markdown。 |
| `/branch [title]` | 从当前逻辑上下文创建并切换到派生 session。 |
| `/context` | 展示稳定上下文快照、预算分类和 active compaction。 |
| `/worktree [list\|create <name>\|exit ...]` | 查看或通过审批管理当前 session 的 worktree。 |
| `/rewind [run-id-prefix]` | 从早期 run 创建派生 session，并在安全校验与确认后恢复文件。 |
| `/stats [workspace\|session]` | 汇总主运行指标，并单列 maintenance 用量。 |
| `/doctor [--network]` | 在 TUI 中运行只读诊断；默认不联网。 |
| `/model [profile-id]` | 查看或切换当前 session 的模型；无参数时打开选择器。 |
| `/permissions [full|approve|ask]` | 无参数时打开权限选择框；带参数时直接切换。`rules|recent|doctor|trust-project|add|remove` 管理与诊断规则。 |
| `/approvals` | 处理非交互运行留下的 Plan、Action 或普通工具权限请求。 |
| `/queue` | 查看队列；`start <id>` 显式启动导入队列，另有 `move`、`cancel` 和 `retry`。 |
| `/memory ...` | 管理记忆、独立 capture/recall 开关、候选、整理、导入导出和 embeddings。 |
| `/instructions ...` | 列出、解释或重新加载受控仓库指令。 |
| `/skills ...` | 列出、查看、校验、启用或禁用 Skill。 |
| `/agents [inspect|cancel|cleanup <id>]` | 查看、取消或清理本机会话的子 Agent。 |
| `/sources` | 查看当前会话 Web 来源。 |
| `/mcp [list|status <server>|tools <server>]` | 检查 MCP 配置。 |
| `/diff` | 显示当前 Git diff。 |
| `/undo` | 撤销最近一次仍可安全反转的文件动作。 |
| `/rename <title>` | 手工设置会话标题。 |
| `/exit` | 退出 TUI。 |
| `/quit` | 退出 TUI，与 `/exit` 等价。 |

命令目录不提供额外 alias；`/continue`、`/clear`、`/fork`、`/cost`、`/tasks`、`/changes`、`/commands` 或 `/web` 不解析。

`/model` 显示实际配置的 profile、Provider、模型与可用状态。选择会同时切换窗口、输出上限、计价及缓存身份，并持久化到 session。旧模型名称仅在唯一匹配配置时接受；缺失或重名的历史 profile 必须明确重选后才能运行。活跃 run 期间禁止切换。

## TUI 输出

交互入口支持 `--ui inline|fullscreen`；当前默认是 `inline`，也可用
`CAPSLOCK_UI` 选择。inline UI 使用 prompt-toolkit/Rich 在普通终端主缓冲区
输出，可靠保留原生 scrollback；fullscreen UI 是保留的第一版 Textual 全屏界面，
运行在 alternate screen。
两个界面的根层使用终端默认背景；用户 prompt 使用浅灰背景、深色文字和左侧焦点色
边线，CapsLock 回答保持透明。fullscreen 的 App 根层输出原生 `ansi_default`，其余容器保持
透明；Markdown、Syntax 和 Composer 的字符级背景被清除，但前景色与粗体、斜体、
下划线、删除线和链接样式保持不变。需要模态结果的 fullscreen 斜杠命令通过
Textual worker 执行，避免阻塞界面消息泵。

fullscreen 的 `/` 命令和 `$` Skill 候选使用纵向滚动列表，不截断完整候选集；
`↑/↓` 循环选择时列表自动滚动到当前项。窄终端保持单列布局，终端小于
48×14 时只显示尺寸提示且审批直接拒绝。

两套界面共用选择与结构化问题模型。超过 8 个选项时提供即时过滤；`ask_user`
单选使用选项列表，多选用 Space 切换，并始终可选择 Other。问题逐步校验且提交前
显示答案摘要。队列条压缩为最近预览，空 Composer 按 `↑` 召回最新未开始项；旧项
立即持久化为 cancelled，重新提交创建新 ID 并恢复队列位置。

启动 banner 保留 v1.7.1 的 `Welcome back`、CapsLock 字符画和 Tips 布局；窄终端使用纵向布局，宽终端使用双栏布局。原 full-screen UI 的语义左边框消息卡、QueueBar、Composer、ActivityBar、响应式 StatusBar 和审批 Dialog 均由 Rich/prompt-toolkit 在 inline 动态区实现。动态区不使用 `bottom_toolbar`，每次上下文输出后都会在新光标位置重画，因此 Composer 跟随上下文向下移动而不固定在窗口底部。模型提供方返回的 reasoning 默认折叠为一行 `◇ Reasoning` 摘要，开启 details 时以低对比度、暗化斜体显示；最终回答在 `◆ CapsLock` 下按 Markdown 渲染，不使用额外的 `Final answer` 标签。连续读取和搜索工具合并为一条 `Explored` 摘要，修改、命令与失败结果单独显示。

模型请求和工具执行期间，底部活动行在 `Thinking` 或 `Running <tool>` 左侧循环显示 `◐ ◓ ◑ ◒`。产生待审批提案后，输入框暂停并逐条显示有界脱敏预览与选择框；选择批准即直接进入执行前复检，不再追加 `y/N`。阶段结束后动画消失，并在 scrollback 中留下静态结果：绿色圆点表示成功，红色圆点表示失败，黄色圆点表示等待审批，警告色圆点表示取消。

裸 `capslock resume` 使用方向键和 Enter 选择 session；显式 ID/唯一前缀仍受支持。恢复视图按 run 的持久化插入顺序分组，每轮固定先显示用户提示、再显示 CapsLock 回答，不以可能冲突或倒退的时间戳跨轮混排。恢复时重放已完成消息以及中断/失败 run 的用户问题和已产生文本，后续模型请求使用同一份 session 上下文，同时排除当前 run 以避免重复当前问题。

## CLI

```text
capslock [--ui inline|fullscreen]
capslock exec [PROMPT] [--json] [--max-tool-rounds N] [--max-tool-calls N]
  [--max-duration-seconds N] [--max-tokens N] [--max-budget-usd N]
capslock resume [SESSION] [--limit N] [--ui inline|fullscreen]
capslock sessions|session [--limit N]
capslock sessions|session search <QUERY> [--archived]
capslock sessions|session rename <SESSION> <TITLE>
capslock sessions|session archive|unarchive <SESSION>
capslock sessions|session export <SESSION> <WORKSPACE-RELATIVE-DIRECTORY>
capslock sessions|session delete [SESSION] [--yes]
capslock input list
capslock input answer <REQUEST-ID> --answers-json <JSON>
capslock input cancel <REQUEST-ID>
capslock init [--non-interactive ...] [--update] [--check-provider]
  [--strict-tool-calls] [--json-schema-outputs]
capslock config validate|migrate
capslock credentials status|set|delete
capslock backup create|list|verify|restore
capslock database compact --scope workspace|memory|all [--yes]
capslock export <ARCHIVE> [--include-global-memory]
capslock import <ARCHIVE> [--yes]
capslock plugin|plugins install|upgrade <PATH> [--yes]
capslock plugin|plugins list|show|verify <NAME>
capslock plugin|plugins enable|disable|uninstall <NAME> [--yes]
capslock doctor [--json] [--strict] [--network] [--fix] [--yes]
capslock trace list [--limit N]
capslock trace show <TRACE-ID>
capslock trace summary
capslock trace prune [--days N]
```

裸入口只允许 TTY。`exec` 可从 stdin 读取 prompt，不进行交互审批或问答；产生待审批动作时保存 session/run/action 并返回退出码 3，预算或循环停止返回 4，等待用户输入返回 5。`capslock input answer|cancel` 结算请求后恢复原 run。

## JSONL 事件

每行字段顺序与含义固定：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定为 `3`。 |
| `sequence` | run 内从 1 递增。 |
| `event_id` | 全局唯一事件 ID。 |
| `trace_id` | run 级追踪 ID。 |
| `timestamp` | 带时区的 RFC 3339 时间。 |
| `session_id` | 会话 ID。 |
| `work_item_id` | 前台工作项 ID。 |
| `run_id` | 本次执行 ID。 |
| `event` | 事件枚举。 |
| `status` | 当前或终止状态。 |
| `terminal` | 是否为唯一终止事件。 |
| `data` | 事件载荷。 |

非终止事件：`queued`、`context_updated`、`thinking`、`text_delta`、`tool_queued`、`tool_running`、`tool_progress`、`tool_permission`、`tool_completed`、`tool_cancelled`、`budget_updated`、`limit_reached`、`budget_extended`。

`context_updated.data.context` 固定包含 `used_tokens`、`limit_tokens`、
`remaining_tokens`、`used_percent` 和 `source`。context build 后先发
`source=estimate`；每次 provider usage 返回后发 `source=provider`，usage 缺失时继续
使用 estimate。该实时状态事件由 `exec --json` 输出，但不写入 run journal；终止
事件仍保持唯一。成功减少 token 的 active-run 压缩还附带可选 `data.compaction`，包含 `before_tokens`、`after_tokens`、`saved_tokens` 和 `forced`。

`thinking.data.text` 是模型提供方显式返回的 reasoning；`text_delta.data.text` 是最终回答的流式正文。TUI 分区渲染二者，`completed.data.answer` 只包含最终回答。

`tool_running.data.presentation` 与 `tool_completed.data.presentation` 是可选的
版本化展示摘要，当前 `version=1`，包含 `category`、`title` 以及可选的
`detail`、`target`、`outcome`。它只从工具 allowlist 字段生成并经过脱敏与长度
限制，不承载原始参数、完整输出或文件正文。

终止事件：

- `completed`：`answer`、`citations`、`memory_recalls`、`usage`、`duration_ms`。
- `waiting_approval`：`action_ids` 与数量。
- `waiting_input`：持久化 input request ID 与结构化问题摘要。
- `failed` / `cancelled`：`error.code` 与 `error.message`。
- `stopped`：`stop_reason`、`budget` 和已用量；每个 run 仍只有一个终止事件。

## Workflow 与恢复

`work_items` 管理队列状态，`runs.work_item_id` 必填。当前 run 由 runs 查询获得；work item 不保存反向 current-run 外键。`run_events` 只保存 `run_id + sequence + kind + payload`，session/work item 通过 run 关系取得。

ToolLoop 每个模型或工具阶段写 `run_steps`。只有 completed 且带 checkpoint 的步骤可用于恢复。`resume` 创建新的 work item 和 run，记录 `parent_run_id` 与 `resume_from_step_id`，不会修改失败 run 的历史。空回答、模型错误或轮次耗尽会将当前模型 step 标为 failed。

`AgentSession.run_stream(RunRequest)` 是唯一 Agent 执行 API。每次流只产生一个终止事件；同一 session 串行执行，调用方取消流时，内部执行 task 也会被取消并等待资源清理。
`RunRequest.response_format` 可携带 strict Provider JSON Schema，并会在 complete、streaming、工具循环和暂停恢复链路中保持不变；普通用户请求留空，子 Agent Runtime 根据任务契约设置。

## 多 Agent 契约

`AgentTaskContract` 固定记录父 run、目标、输入数据、允许路径、能力、模型 profile、限制和验证要求，并以 SHA-256 绑定持久任务。能力缺省为空，子运行仍仅装配工作区只读工具；写入、命令、Web 与 MCP 工具按显式 grant 加入，插件和二次委派不自动加入。兼容的 `delegate_agents` 仍支持批量一次性任务；team 控制面额外提供 session-owned 命名 worker、显式任务依赖、优先级、原子 claim 和 persistent follow-up。

worker workspace 支持 `snapshot`、`worktree`、`shared_read`。前两者记录父工作区 baseline；已验证产物先完整暂存，再与普通文件 Action 共用规范化工作区写锁，锁内复验全部父文件基线、创建备份并批量替换，失败时回滚已替换文件并在恢复失败时保留备份路径。worktree 要求干净 Git 父仓库并记录 base commit；shared-read 通过 capability policy 强制只读。该锁协调 CapsLock 管理的写入；外部进程只能通过替换前最终复验尽力检测，不构成绝对 CAS。

team task 只有在全部依赖成功后才从 blocked 转为 ready，同一 worker 同时只运行一个任务；claim token 与 attempt ordinal 防止重复领取。每次 attempt 记录预算 reserve/settle/release ledger、child approval link 和 checkpoint。恢复必须复用原 attempt 与 child session/workspace，并重新验证 contract digest；只有显式 resumable checkpoint 可以恢复，未知副作用不得自动重放。子快照排除 `.git`、`.capslock`、环境文件和符号链接，并使用自己的 workspace/memory 数据库。后台任务通过 `agent_mailbox` 交换 instruction/question/response/progress/artifact offer/cancel，team message 可点对点或广播；消息先脱敏并限制为 32 KiB，读取时复验 SHA-256，状态为 queued/delivered/acknowledged/expired，正文始终视为不可信数据。`AgentOutputVerifier` 校验输出对象、allowlist 路径、必需检查、文件大小和 SHA-256；未通过的输出只返回失败诊断。

workspace schema 21 使用 Agent team/worker/task/dependency/attempt/checkpoint/budget/approval/workspace/mailbox、performance span、Tool invocation、input request、session lineage、active compaction、episodic document、session worktree 与 Plan Mode 表保存可恢复状态、审计与验证结果。Agent capability 以校验过 digest 的 task contract 为权威来源，citation 以终态 run event 为权威来源。portable archive 默认不包含 artifact 正文，也不包含可重建的 episodic 与摘要分段索引。

## 记忆契约

记忆 identity 与内容 revision 分离：

- `memories`：作用域、状态、来源与 current revision。
- `memory_revisions`：不可变正文、类型、置信度、过期时间、来源和操作。
- `memory_candidates` / `memory_extractions`：候选提取与审核。
- `memory_sources`：来源有效性。
- `memory_embeddings`：revision 绑定的向量。
- `memory_recalls` / `memory_recall_items`：run 级召回解释。
- `memory_relations` / `memory_jobs`：重复、冲突、替代关系与持久后台作业。
- `memory_audit`：包括 purge 后仍保留的操作轨迹。
- `memory_fts`：仅索引当前 active revision。

默认策略为 `automatic`。提取分数只保留作诊断，独立验证器在不接收该分数的情况下检查逐字来源，再使用与模型 profile、验证 Prompt 版本绑定的 `memory-verifier-v1` 校准文件；不存在精确匹配的校准时保持 review-only。直接来源自动采纳阈值为 0.95，多来源推断为 0.98 且至少需要两个独立用户来源；global、冲突、指令、验证失败与无来源内容要求审核。项目事实不会仅因类型为 `project` 被标记为指令。

`temporary` 缺省在 7 天后 purge，`session` 按 lifecycle owner 清理，`project` 绑定 workspace `project_instance_id`，`durable` 长期保留。提取作业读取完整用户 transcript，按 40k 字符预算分段并保留重叠来源；未变化 map 分段按 digest 复用，并由 reduce 阶段合并跨轮偏好。

记忆 context 最多 5 条、合计最多 4 KiB，并标记为不可信 JSON 数据。召回要求 lexical top-10 或 cosine ≥ 0.45 且最终分数 ≥ 0.50；超长内容按 UTF-8 截断。`purge` 删除全部 revision 正文、FTS、向量、来源和作业关联正文。导入接受 `capslock-memory-export` version 3/4。

## 数据库与布局

工作区数据库使用 application ID `0x434C4B32`、schema 21，记忆数据库使用 `0x434C4D32`、schema 6。两者开启 foreign keys、WAL 和 5 秒 busy timeout；记忆库额外开启 secure delete 并设置文件权限 `0600`。迁移均先 checkpoint 和备份，失败恢复原数据库；`VACUUM` 只由显式 `database compact` 命令执行。

应用先读取 application ID 和 schema version，确认是当前格式或可迁移格式后才切换 WAL。workspace schema 为 21，memory schema 为 6；其他 application ID 或 schema 只报错，不修改原数据库。episodic 索引属于派生数据，导入、branch 和 rewind 后可幂等重建。

portable import 使用 archive ID 幂等记录。相同 ID 与内容跳过，同 ID 不同内容确定性重映射并重写引用。running run 转为 interrupted，approved/running action 转为 pending；导入的历史副作用不能在目标工作区执行 undo。

## 模型路由、预算与外部嵌入

- `reasoning` 用于工具循环与最终回答，`fast` 用于记忆候选提取，`embedding` 用于外部语义检索；`vision` 只保留配置，不接受视觉输入。
- timeout、429 和 5xx 最多重试两次；只有相同 data-policy 的显式后备 profile 可接管，首个流式 delta 后不再重试。
- `/status` 和 JSONL 终止事件保留 run/session token、费用及逐模型摘要。预算预检失败时，TUI 可仅批准下一次模型调用，`exec` 返回 `model_budget_exceeded`。
- `/memory embeddings enable external <model-profile>` 会先展示 `memory.content`、未来 `recall.query`、当前记录数和 UTF-8 字节数；确认记录失效或撤销后不会联网。

canonical 路径见项目 README。

新增自动化与授权接口见 [优化交付说明](reliability-optimization.md) 和 [stdio 协议](app-server.md)。
