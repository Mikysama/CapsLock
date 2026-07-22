# CapsLock v2 Agent Reference

本参考描述 v2 的模型工具、TUI 命令、审批边界与持久化契约。

## 稳定契约

CapsLock 2.1.0 支持 Linux/macOS 与 Python 3.12。CLI 命令和退出码、`config_version = 2`、workspace schema v4、memory schema v3、portable archive v2、JSONL schema v2、Skill manifest、插件协议 v1 和 `ToolResult` 的 `ok/data/error` 模型输入协议在 2.x 系列内保持向后兼容。

1.10.1 的 `repositories=`、`ModelRouter.bind_run()/use_role()/summary(run_id)` 与 `max_turns` 入口已删除。新弃用至少提前一个 minor 版本公告；删除的入口不提供静默兼容。完整映射见 [v2 开发过程与迁移](development/v2/v2.0.md)。

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `full_access` | 自动批准普通动作；Skill 文件写入仍逐次确认。安全校验、状态与审计始终启用。 |
| `approve_for_me` | 默认模式。文件、命令、MCP 和插件等高风险动作需要确认。 |
| `ask_for_approval` | 发送请求和后续动作都要求人工确认。 |

使用 `/permissions` 打开三档权限选择框，或使用 `/permissions full|approve|ask` 直接切换。选择保存在工作区 settings repository。

## 模型工具

所有工具通过同一个异步 registry 调用。文件读取放入工作线程，Git 与命令使用异步子进程，Web 使用 `httpx.AsyncClient`，MCP 保持原生异步。

| 工具 | 功能 | 边界 |
| --- | --- | --- |
| `list_files` | 列出工作区文件。 | 只读；路径限制在工作区。 |
| `read_file` | 分段读取 UTF-8 文件并返回行号 Evidence。 | 只读；拒绝越界、符号链接和超限文件。 |
| `search_files` | 搜索工作区文本并返回 Evidence。 | 只读。 |
| `git_status` / `git_diff` | 查询固定 Git 状态或差异。 | 只读；不接受任意 Git 参数。 |
| `task_list_update` | 替换当前会话任务列表。 | session repository 写入。 |
| `task_status_update` | 更新任务状态。 | 状态枚举校验。 |
| `list_external_sources` | 查看当前会话保存的 Web 来源。 | 来源始终不可信。 |
| `search_memories` | 搜索可见且未过期的记忆。 | 只读；作用域隔离。 |
| `get_memory` | 按 ID 获取可见记忆并记录访问。 | 只读；用于来源失效隔离。 |
| `load_skill` | 加载已启用 Skill 的正文和资源清单。 | 正文是不可信上下文。 |
| `read_skill_resource` | 读取本 run 已加载的 Skill 资源快照。 | 只读；拒绝越界、二进制和符号链接。 |
| `propose_file_edit` | 创建精确文本替换提案。 | 未获批准不写用户文件。 |
| `propose_file_create` | 创建新文件提案。 | 未获批准不写用户文件。 |
| `propose_command` | 创建固定命令模板提案。 | 未获批准不启动进程。 |
| `propose_web_search` | 创建 Tavily 搜索动作。 | 外部请求与来源审计。 |
| `propose_web_fetch` | 创建公开 URL 抓取动作。 | SSRF、重定向、类型和大小限制。 |
| `propose_mcp_connect` | 创建 MCP server 连接与工具发现动作。 | 仅本地 stdio。 |
| `propose_mcp_call` | 创建 allowlist 内 MCP 工具调用动作。 | 第三方副作用不可自动撤销。 |
| `plugin_<plugin>_<tool>` | 调用已安装且获当前工作区授权的本地插件工具。 | 使用高风险 `mcp_call` 审批通道；结果始终不可信。 |

模型只提交提案；统一 `ActionCoordinator` 决定是否等待批准或自动执行。TUI 为 Coordinator 安装阻塞式审批器：越过权限边界时只询问是否执行，不显示动作载荷，用户只能拒绝或执行；最终动作状态返回同一个模型工具调用，run 随后继续。非交互 `exec` 不安装审批器，仍保留 pending action、`waiting_approval` 终止事件和退出码 `3`。动作记录只使用 `request_json` 与 `result_json`，新增动作类型不需要 subtype 表。

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

交互审批在 Action 工具调用内完成，因此批准执行或拒绝后由同一个 run 继续推理，不产生中间终止事件。非交互或导入数据的待审批动作仍可通过 `/approvals` 结算；该兼容路径在同一个 SQLite 事务内更新 action、run、work item 和终止事件。重复结算已完成的 run 返回空结果，不会产生第二个终止事件。

文件执行前重新检查提案哈希；命令取消先向进程组发送 TERM，2 秒后仍未退出则发送 KILL；Web 跟随重定向前重新执行公开地址校验；MCP 在执行时再次检查工具 allowlist；插件在执行时重新检查安装摘要和工作区授权。

## TUI 命令

| 命令 | 功能 |
| --- | --- |
| `/help` | 显示 v2 命令。 |
| `/status` | 汇总 session、workspace、model、permissions、context、usage、tasks 和 queue。 |
| `/permissions [full|approve|ask]` | 无参数时打开权限选择框；带参数时直接切换。 |
| `/approvals` | 处理非交互、导入或旧数据留下的待审批动作。 |
| `/queue` | 查看队列；`start <id>` 显式启动导入队列，另有 `move`、`cancel` 和 `retry`。 |
| `/memory ...` | 管理记忆、候选、召回、导入导出和 embeddings。 |
| `/skills ...` | 列出、查看、校验、启用或禁用 Skill。 |
| `/sources` | 查看当前会话 Web 来源。 |
| `/mcp [list|status <server>|tools <server>]` | 检查 MCP 配置。 |
| `/diff` | 显示当前 Git diff。 |
| `/undo` | 撤销最近一次仍可安全反转的文件动作。 |
| `/rename <title>` | 手工设置会话标题。 |
| `/exit` | 退出 TUI。 |
| `/quit` | 退出 TUI，与 `/exit` 等价。 |

v2 不解析旧 alias，也不提供独立 `/cost`、`/context`、`/tasks`、`/changes`、`/commands` 或 `/web` 页面。

## TUI 输出

启动 banner 恢复 v1.7.1 的 `Welcome back`、CapsLock 字符画和 Tips 布局；窄终端使用纵向布局，宽终端使用双栏布局。模型提供方返回的 reasoning 单独显示在低对比度、暗化斜体的 `◇ Model reasoning` 区域；最终回答显示在高对比度主文本样式的 `◆ CapsLock` 区域，不使用额外的 `Final answer` 标签。

模型请求和工具执行期间，底部活动行在 `Thinking` 或 `Running <tool>` 左侧循环显示 `◐ ◓ ◑ ◒`。产生待审批提案后，输入框暂停并逐条显示完整脱敏载荷与选择框；选择批准即直接进入执行前复检，不再追加 `y/N`。阶段结束后动画消失，并在 scrollback 中留下静态结果：绿色圆点表示成功，红色圆点表示失败，黄色圆点表示等待审批，警告色圆点表示取消。

裸 `capslock resume` 使用方向键和 Enter 选择 session；显式 ID/唯一前缀仍受支持。恢复时重放已完成消息以及中断/失败 run 的用户问题和已产生文本，后续模型请求使用同一份 session 上下文，同时排除当前 run 以避免重复当前问题。

## CLI

```text
capslock
capslock exec [PROMPT] [--json] [--max-tool-rounds N] [--max-tool-calls N]
  [--max-duration-seconds N] [--max-tokens N] [--max-budget-usd N]
capslock resume [SESSION] [--limit N]
capslock sessions|session [--limit N]
capslock sessions|session search <QUERY> [--archived]
capslock sessions|session rename <SESSION> <TITLE>
capslock sessions|session archive|unarchive <SESSION>
capslock sessions|session export <SESSION> <WORKSPACE-RELATIVE-DIRECTORY>
capslock sessions|session delete [SESSION] [--yes]
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

裸入口只允许 TTY。`exec` 可从 stdin 读取 prompt，不进行交互审批；产生待审批动作时保存 session/run/action 并返回退出码 3，预算或循环停止返回退出码 4。

## JSONL 事件

每行字段顺序与含义固定：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定为 `2`。 |
| `sequence` | run 内从 1 递增。 |
| `timestamp` | 带时区的 RFC 3339 时间。 |
| `session_id` | 会话 ID。 |
| `work_item_id` | 前台工作项 ID。 |
| `run_id` | 本次执行 ID。 |
| `event` | 事件枚举。 |
| `status` | 当前或终止状态。 |
| `terminal` | 是否为唯一终止事件。 |
| `data` | 事件载荷。 |

非终止事件：`queued`、`thinking`、`text_delta`、`tool_running`、`tool_completed`、`budget_updated`、`limit_reached`、`budget_extended`。

`thinking.data.text` 是模型提供方显式返回的 reasoning；`text_delta.data.text` 是最终回答的流式正文。TUI 分区渲染二者，`completed.data.answer` 只包含最终回答。

终止事件：

- `completed`：`answer`、`citations`、`memory_recalls`、`usage`、`duration_ms`。
- `waiting_approval`：`action_ids` 与数量。
- `failed` / `cancelled`：`error.code` 与 `error.message`。
- `stopped`：`stop_reason`、`budget` 和已用量；每个 run 仍只有一个终止事件。

## Workflow 与恢复

`work_items` 管理队列状态，`runs.work_item_id` 必填。当前 run 由 runs 查询获得；work item 不保存反向 current-run 外键。`run_events` 只保存 `run_id + sequence + kind + payload`，session/work item 通过 run 关系取得。

ToolLoop 每个模型或工具阶段写 `run_steps`。只有 completed 且带 checkpoint 的步骤可用于恢复。`resume` 创建新的 work item 和 run，记录 `parent_run_id` 与 `resume_from_step_id`，不会修改失败 run 的历史。空回答、模型错误或轮次耗尽会将当前模型 step 标为 failed。

`WorkspaceAgent.ask_stream()` 是唯一 Agent 执行 API。每次流只产生一个终止事件；调用方取消流时，内部执行 task 也会被取消并等待资源清理。

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

应用先读取 application ID 和 schema version，确认兼容后才切换 WAL。v1.10.0 的 workspace schema v4 增加 run 治理快照、工具调用指纹和结构化停止原因；memory schema 保持 v3。正式升级保证 v1.9 schema v3 在备份后迁移到 v4；旧 application ID 和未知版本仍只报错。

portable import 使用 archive ID 幂等记录。相同 ID 与内容跳过，同 ID 不同内容确定性重映射并重写引用。running run 转为 interrupted，approved/running action 转为 pending；导入的历史副作用不能在目标工作区执行 undo。

## 模型路由、预算与外部嵌入

- `reasoning` 用于工具循环与最终回答，`fast` 用于记忆候选提取，`embedding` 用于外部语义检索；`vision` 只保留配置，不接受视觉输入。
- timeout、429 和 5xx 最多重试两次；只有相同 data-policy 的显式后备 profile 可接管，首个流式 delta 后不再重试。
- `/status` 和 JSONL 终止事件保留 run/session token、费用及逐模型摘要。预算预检失败时，TUI 可仅批准下一次模型调用，`exec` 返回 `model_budget_exceeded`。
- `/memory embeddings enable external <model-profile>` 会先展示 `memory.content`、未来 `recall.query`、当前记录数和 UTF-8 字节数；确认记录失效或撤销后不会联网。

canonical 路径与手工迁移步骤见 [v2 开发过程与迁移](development/v2/v2.0.md)。
