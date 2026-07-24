# CapsLock Agent Reference

本参考描述当前模型工具、TUI 命令、审批边界与持久化契约。

## 稳定契约

CapsLock 2.3.1 支持 Linux/macOS 与 Python 3.12。当前开发协议为 `config_version = 5`、workspace schema 8、memory schema 3、portable archive 3、JSONL schema 3 和插件 manifest/protocol/grant 4。config v3/v4 与 workspace schema v6/v7 使用 backup-first 自动迁移；删除的 Python 接口不提供兼容入口。

公开运行入口为 `AgentSession.run_stream(RunRequest)`。CLI 通过应用查询面读取状态，不应依赖 repository 聚合对象。

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `full_access` | 自动批准普通动作；Skill 文件写入仍逐次确认。安全校验、状态与审计始终启用。 |
| `approve_for_me` | 默认模式。文件、命令、MCP 和插件等高风险动作需要确认。 |
| `ask_for_approval` | 发送请求和后续动作都要求人工确认。 |

使用 `/permissions` 打开三档权限选择框，或使用 `/permissions full|approve|ask` 直接切换。选择保存在工作区 settings repository。

## 模型工具

所有工具通过同一个 `ToolRuntime` 调用。`ToolCatalog` 负责稳定元数据、动态发现和 schema 预算，`ToolExecutor` 负责校验、授权、执行、恢复与结果编码。

| 工具 | 功能 | 边界 |
| --- | --- | --- |
| `list_files` / `glob_files` | 列出或按 glob 查找工作区文件。 | 只读；遵循路径边界和 `.gitignore`。 |
| `read_file` / `read_image` | 读取文本或富图片内容。 | 只读；文件大小、类型、符号链接和路径受限。 |
| `search_files` | 使用 ripgrep 搜索文本并返回 Evidence。 | 只读；有稳定排序和结果上限。 |
| `edit_file` / `create_file` / `write_file` | 通过 Action 修改文件。 | 审批、hash revalidate、diff 和 undo。 |
| `git_status` / `git_diff` | 查询 Git 状态或差异。 | 只读；不接受任意 Git 参数。 |
| `shell` | 在 OS 沙箱运行命令。 | 工作区可写、默认断网；危险命令 hard deny。 |
| `process_output` / `process_stop` | 管理 session 隔离的后台进程。 | 有界输出和 TERM→KILL 取消。 |
| `ask_user` | 创建可持久化结构化问题。 | 暂停同一 invocation，可跨进程回答。 |
| `create_task` / `list_tasks` / `get_task` / `update_task` | 管理任务与依赖关系。 | session 隔离并拒绝依赖环。 |
| `read_pdf` / `read_notebook` / `edit_notebook` | 读取 PDF/Notebook 或编辑 cell。 | 有界读取；Notebook 编辑使用独立 Action。 |
| LSP 语义工具 | 定义、引用、符号、实现和调用层级查询。 | 仅已安装/配置 server；只读、禁网沙箱。 |
| `list_mcp_resources` / `read_mcp_resource` | 发现和读取 MCP Resources。 | server/URI 权限；二进制写 artifact。 |
| `mcp__<server>__<tool>` | 调用动态发现的 MCP 工具。 | 唯一受管理长连接路径，调用仍经 Action。 |
| `plugin__<plugin>__<tool>` | 调用获授权的本地插件工具。 | capability broker、沙箱、审批和结果脱敏。 |
| `web_search` / `web_fetch` | 搜索或抓取公开 Web 内容。 | SSRF、重定向、类型、大小和来源审计。 |
| Memory / Skill 工具 | 查询记忆或加载 Skill 快照。 | 作用域隔离，只读，不可信上下文。 |
| Worktree / Agent 工具 | 切换 session worktree 或控制子 Agent。 | context mutation 独占执行，跨 session 拒绝。 |

模型直接调用业务能力工具；需要副作用的工具由运行时创建 Action，统一 `ActionCoordinator` 决定是否等待批准或自动执行。TUI 为 Coordinator 安装阻塞式审批器：越过权限边界时显示动作类型、风险、目标，以及最多 40 行、4 KiB 的本机脱敏命令或 diff 预览，用户只能拒绝或执行且默认选择拒绝；原始参数、完整输出、文件正文和凭据不会进入展示事件。最终动作状态返回同一个模型工具调用，run 随后继续。非交互 `exec` 不安装审批器，仍保留 pending action、`waiting_approval` 终止事件和退出码 `3`。动作记录只使用 `request_json` 与 `result_json`，新增动作类型不需要 subtype 表。

## 工具契约与 artifact

`ToolContract`、`ToolDefinition` 和 `ResolvedToolPolicy` 声明输入/输出 JSON Schema、参数级只读/并发/破坏性属性、取消行为、capability 与结果限制。`ToolCatalog` 只负责稳定 schema、动态发现和 fingerprint；`ToolExecutor` 固定执行 normalize、validate、authorize、execute、输出校验和 middleware。连续的只读且并发安全调用使用有界并发执行，checkpoint 仍按模型 tool-call 顺序写入。

超过 16 KiB 的结果写入 `.capslock/state/artifacts/sha256/`，单项最多 5 MiB。模型只收到脱敏预览和 artifact ID；`read_tool_artifact` 只能分块读取当前 session 的 artifact，session 删除会级联清理记录与文件。

## 上下文预算

输入预算由模型 `context_window - max_output_tokens` 计算，并计入 system prompt、memory、Skill catalog、工具 schema 与 checkpoint。达到触发比例后先外置大型工具结果，再保留最近六轮并由 fast 角色生成结构化摘要。摘要作为不可变 compaction artifact 保存来源边界、token、profile 与 digest；恢复时复用最近的有效记录。摘要失败使用确定性兜底，连续失败达到上限后返回 `context_budget_exceeded`，不会继续调用模型。

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
| `/status` | 汇总 session、workspace、model、permissions、context、usage、tasks 和 queue。 |
| `/model [deepseek-v4-flash\|deepseek-v4-pro]` | 查看或切换当前 session 的模型；无参数时打开选择器。 |
| `/permissions [full|approve|ask]` | 无参数时打开权限选择框；带参数时直接切换。 |
| `/approvals` | 处理非交互运行留下的待审批动作。 |
| `/queue` | 查看队列；`start <id>` 显式启动导入队列，另有 `move`、`cancel` 和 `retry`。 |
| `/memory ...` | 管理记忆、候选、召回、导入导出和 embeddings。 |
| `/skills ...` | 列出、查看、校验、启用或禁用 Skill。 |
| `/agents [inspect|cancel|cleanup <id>]` | 查看、取消或清理本机会话的子 Agent。 |
| `/sources` | 查看当前会话 Web 来源。 |
| `/mcp [list|status <server>|tools <server>]` | 检查 MCP 配置。 |
| `/diff` | 显示当前 Git diff。 |
| `/undo` | 撤销最近一次仍可安全反转的文件动作。 |
| `/rename <title>` | 手工设置会话标题。 |
| `/exit` | 退出 TUI。 |
| `/quit` | 退出 TUI，与 `/exit` 等价。 |

命令目录不提供额外 alias，也不提供独立 `/cost`、`/context`、`/tasks`、`/changes`、`/commands` 或 `/web` 页面。

`/model` 只接受 `deepseek-v4-flash` 和 `deepseek-v4-pro`。选择写入当前
session，恢复后继续生效；新 session 使用配置默认模型。运行中的模型会话保持
不可变，因此活跃 run 期间的切换请求会被拒绝，避免一个 run 在工具轮次之间
更换模型。

## TUI 输出

交互入口支持 `--ui inline|fullscreen`；当前默认是 `inline`，也可用
`CAPSLOCK_UI` 选择。inline UI 使用 prompt-toolkit/Rich 在普通终端主缓冲区
输出，可靠保留原生 scrollback；fullscreen UI 是保留的第一版 Textual 全屏界面，
运行在 alternate screen。
两个界面的根层都使用终端默认背景；上下文中的用户 prompt 行使用浅灰背景，并与
左侧蓝色标记对齐。fullscreen 的 App 根层输出原生 `ansi_default`，其余容器保持
透明；Markdown、Syntax 和 Composer 的字符级背景被清除，但前景色与粗体、斜体、
下划线、删除线和链接样式保持不变。需要模态结果的 fullscreen 斜杠命令通过
Textual worker 执行，避免阻塞界面消息泵。

fullscreen 的 `/` 命令和 `$` Skill 候选使用纵向滚动列表，不截断完整候选集；
`↑/↓` 循环选择时列表自动滚动到当前项。窄终端保持单列布局，终端小于
48×14 时只显示尺寸提示且审批直接拒绝。

启动 banner 保留 v1.7.1 的 `Welcome back`、CapsLock 字符画和 Tips 布局；窄终端使用纵向布局，宽终端使用双栏布局。原 full-screen UI 的语义左边框消息卡、QueueBar、Composer、ActivityBar、响应式 StatusBar 和审批 Dialog 均由 Rich/prompt-toolkit 在 inline 动态区实现。动态区不使用 `bottom_toolbar`，每次上下文输出后都会在新光标位置重画，因此 Composer 跟随上下文向下移动而不固定在窗口底部。模型提供方返回的 reasoning 默认折叠为一行 `◇ Reasoning` 摘要，开启 details 时以低对比度、暗化斜体显示；最终回答在 `◆ CapsLock` 下按 Markdown 渲染，不使用额外的 `Final answer` 标签。连续读取和搜索工具合并为一条 `Explored` 摘要，修改、命令与失败结果单独显示。

模型请求和工具执行期间，底部活动行在 `Thinking` 或 `Running <tool>` 左侧循环显示 `◐ ◓ ◑ ◒`。产生待审批提案后，输入框暂停并逐条显示有界脱敏预览与选择框；选择批准即直接进入执行前复检，不再追加 `y/N`。阶段结束后动画消失，并在 scrollback 中留下静态结果：绿色圆点表示成功，红色圆点表示失败，黄色圆点表示等待审批，警告色圆点表示取消。

裸 `capslock resume` 使用方向键和 Enter 选择 session；显式 ID/唯一前缀仍受支持。恢复时重放已完成消息以及中断/失败 run 的用户问题和已产生文本，后续模型请求使用同一份 session 上下文，同时排除当前 run 以避免重复当前问题。

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
capslock config validate|migrate
capslock credentials status|set|delete
capslock backup create|list|verify|restore
capslock export <ARCHIVE> [--include-global-memory]
capslock import <ARCHIVE> [--yes]
capslock plugin|plugins install|upgrade <PATH> [--yes]
capslock plugin|plugins list|show|verify <NAME>
capslock plugin|plugins enable|disable|uninstall <NAME> [--yes]
capslock doctor [--json] [--strict] [--network] [--fix] [--yes]
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

非终止事件：`queued`、`thinking`、`text_delta`、`tool_queued`、`tool_running`、`tool_progress`、`tool_permission`、`tool_completed`、`tool_cancelled`、`budget_updated`、`limit_reached`、`budget_extended`。

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

## 多 Agent 契约

`AgentTaskContract` 固定记录父 run、目标、输入数据、允许路径、能力、模型 profile、限制和验证要求。能力缺省为空，子运行仍仅装配工作区只读工具；写入、命令、Web 与 MCP 工具按显式 grant 加入，插件和二次委派不自动加入。

调度器按契约顺序返回结果，兄弟任务失败不会互相取消，父运行取消会传播到全部未完成子任务。子快照排除 `.git`、`.capslock`、环境文件和符号链接，并使用自己的 workspace/memory 数据库。`AgentOutputVerifier` 校验输出对象、allowlist 路径、必需检查、文件大小和 SHA-256；未通过的输出只返回失败诊断。

workspace schema 8 使用 Agent、Tool invocation、input request、task dependency 和 session worktree 表保存可恢复状态、审计与验证结果。portable archive 默认不包含 artifact 正文。

## 记忆契约

记忆 identity 与内容 revision 分离：

- `memories`：作用域、状态、来源与 current revision。
- `memory_revisions`：不可变正文、类型、置信度、过期时间、来源和操作。
- `memory_candidates` / `memory_extractions`：候选提取与审核。
- `memory_sources`：来源有效性。
- `memory_embeddings`：revision 绑定的向量。
- `memory_recalls` / `memory_recall_items`：run 级召回解释。
- `memory_audit`：包括 purge 后仍保留的操作轨迹。
- `memory_fts`：仅索引当前 active revision。

默认策略为 `review`。`automatic` 只接受用户直接陈述、无风险、workspace/session 作用域的新候选；global、冲突与推断仍要求审核。外部网页或 MCP 内容不会直接成为记忆。

记忆 context 最多 5 条、合计最多 4 KiB，并标记为不可信 JSON 数据。`purge` 删除全部 revision 正文、FTS、向量和来源。导入只接受 `capslock-memory-export` version 3。

## 数据库与布局

工作区数据库使用 application ID `0x434C4B32`，记忆数据库使用 `0x434C4D32`。两者开启 foreign keys、WAL 和 5 秒 busy timeout；记忆库额外开启 secure delete 并设置文件权限 `0600`。

应用先读取 application ID 和 schema version，确认是当前格式后才切换 WAL。workspace schema 为 6，memory schema 为 3；其他 application ID 或 schema 只报错，不修改原数据库。

portable import 使用 archive ID 幂等记录。相同 ID 与内容跳过，同 ID 不同内容确定性重映射并重写引用。running run 转为 interrupted，approved/running action 转为 pending；导入的历史副作用不能在目标工作区执行 undo。

## 模型路由、预算与外部嵌入

- `reasoning` 用于工具循环与最终回答，`fast` 用于记忆候选提取，`embedding` 用于外部语义检索；`vision` 只保留配置，不接受视觉输入。
- timeout、429 和 5xx 最多重试两次；只有相同 data-policy 的显式后备 profile 可接管，首个流式 delta 后不再重试。
- `/status` 和 JSONL 终止事件保留 run/session token、费用及逐模型摘要。预算预检失败时，TUI 可仅批准下一次模型调用，`exec` 返回 `model_budget_exceeded`。
- `/memory embeddings enable external <model-profile>` 会先展示 `memory.content`、未来 `recall.query`、当前记录数和 UTF-8 字节数；确认记录失效或撤销后不会联网。

canonical 路径见项目 README。
