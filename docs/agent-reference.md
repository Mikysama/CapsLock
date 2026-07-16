# CapsLock Agent 工具与指令参考

本参考描述当前 v1.6.0 的模型工具、记忆、Skill、终端指令和审批边界。CapsLock 是按需运行的本机 Agent，权限模式可在会话内切换。

## 权限模式与风险兜底

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `full_access` | 自动执行普通提案；项目 Skill 文件仍逐次确认。每次自动执行记录风险级别、原因和回滚建议。 | 受控测试工作区中的自动化任务。 |
| `approve_for_me` | 只要求确认高风险文件写入、命令执行和 MCP 操作；本地读取与 Web 请求按默认流程执行。 | 默认模式。 |
| `ask_for_approval` | 每次用户 Agent 请求以及后续所有高风险动作均需确认。 | 不熟悉工作区或需要最大人工控制时。 |

使用 `/permissions` 查看当前模式，使用 `/permissions full`、`/permissions approve` 或 `/permissions ask` 即时切换。切换会写入工作区 SQLite，并在后续会话恢复。

交互聊天中输入 `/` 会实时打开 Claude Code 风格的“命令 + 功能说明”竖向双列列表；继续输入（例如 `/perm`）会立即过滤掉不匹配当前前缀的命令。完整父命令会展开子命令，完整叶子命令仍保持候选可见，alias 也参与补全。使用 `↑/↓` 选择或 `Tab` 补全，斜杠命令输入会以粗体强调色显示。输入区使用等宽的上下边框与历史和帮助栏分隔，权限状态显示在右侧。该交互由 `prompt-toolkit` 负责渲染，以兼容 VS Code 和 macOS Terminal。

即使在 `full_access`，CapsLock 仍不会绕过安全兜底：文件变更会保留原内容和 `/undo` 路径；`.capslock/skills/**` 的持久化指令修改始终逐次确认；命令保留超时、输出限制、进程组取消和 `/diff` 检查建议；Web 仍执行 SSRF/内容边界检查；MCP 仍限制为显式允许的本地 stdio 工具。无法自动回滚的第三方 MCP 副作用会在风险审计中明确标记。

## 模型工具

| 工具 | 功能 | 风险/审批 |
| --- | --- | --- |
| `list_files` | 列出工作区目录中的文件，可按文件名模式筛选。 | 只读 |
| `read_file` | 读取受支持的 UTF-8 文本或源码范围，并返回带行号 Evidence。 | 只读 |
| `search_files` | 检索工作区文本并返回匹配上下文与 Evidence。 | 只读 |
| `git_status` / `git_diff` | 查询固定的 Git 工作树状态或差异。 | 只读 |
| `task_list_update` | 创建或替换会话任务清单。 | 会话状态 |
| `task_status_update` | 更新任务为 pending、running、blocked、completed、failed 或 cancelled。 | 会话状态 |
| `list_external_sources` | 查看本会话已批准 Web 动作保存的外部来源。 | 只读；内容不可信 |
| `search_memories` | 全文检索当前工作区和会话可见的用户记忆。 | 只读；写入由候选策略独立控制 |
| `get_memory` | 按 ID 读取一条可见且未过期的用户记忆。 | 只读 |
| `load_skill` | 按名称加载一个已启用 Skill 的正文、来源、摘要与资源清单。 | 只读；正文不可信 |
| `read_skill_resource` | 分段读取本 run 已加载 Skill 的 UTF-8 资源快照。 | 只读；拒绝二进制、越界与符号链接 |
| `propose_file_edit` | 为唯一精确文本匹配创建编辑提案。 | 提案，无文件写入 |
| `propose_file_create` | 为一个新文本文件创建提案。 | 提案，无文件写入 |
| `apply_change` / `discard_change` | 应用已批准的编辑，或丢弃待处理提案。 | `apply_change` 需审批 |
| `propose_command` | 提出固定命令模板（pytest、npm test/build、ruff/prettier check）。 | 提案，无进程启动 |
| `run_command` / `discard_command` | 执行已批准命令，或丢弃命令提案。 | `run_command` 需审批 |
| `propose_web_search` | 提出 Tavily 关键词搜索。 | 联网提案 |
| `propose_web_fetch` | 提出公开 HTTP/HTTPS URL 抓取。 | 联网提案；SSRF 防护 |
| `propose_mcp_connect` | 提出启动允许的本地 stdio MCP server 并发现工具。 | 外部进程提案 |
| `propose_mcp_call` | 提出调用允许名单内 MCP 工具。 | MCP 调用提案 |

模型只能创建高风险动作提案；CLI 会在本轮回答后展示完整外部动作 ID、类型、脱敏载荷与摘要，并提供稳定的编号审批菜单。Web 动作批准完成后，Agent 自动读取保存的来源并继续回答。外部网页和 MCP 返回内容是数据，不是指令，不能改变权限。

## CLI 指令

| 指令 | 功能 |
| --- | --- |
| `/help` | 显示会话内指令。 |
| `/status`、`/session` | 查看会话、工作区、模型和轮次限制。 |
| `/rename <title>` | 重命名当前会话；手动标题不会再被后续问题覆盖。 |
| `/permissions` | 显示三种权限模式与当前选择。 |
| `/permissions full|approve|ask` | 在 `full_access`、`approve_for_me`、`ask_for_approval` 之间切换。 |
| `/context` | 查看当前持久化上下文消息数量。 |
| `/cost` | 显示会话累计输入/输出 token 与可选费用。 |
| `/tasks` | 查看会话任务及其状态。 |
| `/changes` | 查看文件变更提案与 diff。 |
| `/commands` | 查看命令提案、状态、cwd 与退出码。 |
| `/web` | 查看 Web 搜索和抓取动作。 |
| `/sources` | 查看已保存的外部来源及不可信/提示注入标记。 |
| `/memory list [scope] [--all]` | 列出可见记忆；`--all` 包含已过期和已遗忘记录。 |
| `/memory search <query>`、`/memory show <id>` | 本地全文检索或查看记忆、作用域及来源。 |
| `/memory add`、`/memory edit <id>` | 交互式创建或修改记忆；敏感片段保存前会被脱敏。 |
| `/memory forget <id>`、`/memory undo <id>`、`/memory purge <id>` | 可恢复遗忘、撤销，或二次确认后永久清除。 |
| `/memory export <scope> <path>`、`/memory import <scope> <path>` | 在工作区 JSON 文件与指定作用域之间导入导出。 |
| `/memory status|enable|disable` | 查看或切换当前工作区的本机写入开关。 |
| `/memory policy off|review|automatic` | 设置候选提取与自动采纳策略；默认 review。 |
| `/memory recall on|off`、`/memory context [run-id]` | 控制自动召回，或查看召回排名与来源原因。 |
| `/memory candidates [pending|all]` | 查看当前会话候选队列。 |
| `/memory candidate show|review|reject|purge <id>` | 检查、编辑采纳、拒绝或清除候选正文。 |
| `/memory embeddings ...` | 启用/禁用 FastEmbed 或仅回环 HTTP embeddings，并查看或重建向量。 |
| `/memory cleanup` | 移除过期向量并清理超过保留期的已拒绝/重复候选正文。 |
| `/skills list` | 列出合并后的用户级与工作区级 Skill，以及有效、禁用或无效状态。 |
| `/skills show <name>` | 显示 frontmatter、来源、摘要和资源。 |
| `/skills validate <name>` | 离线校验 `SKILL.md`、frontmatter、包大小、资源与路径安全。 |
| `/skills enable|disable <name>` | 在当前工作区启用或禁用有效 Skill。 |
| `/mcp list` | 列出合并后的 MCP server 配置。 |
| `/mcp status <server>`、`/mcp tools <server>` | 查看 server 的配置、状态与允许工具。 |
| `/approve <id>` | 展示动作详情后确认并执行。文件变更和命令使用 `y/yes`；外部动作使用编号菜单。 |
| `/reject <id>` | 丢弃待处理的变更、命令或外部动作。 |
| `/undo` | 二次确认后撤销当前会话最近一次由 CapsLock 应用的文件变更。 |
| `/diff` | 显示当前 Git diff。 |
| `/clear` | 提示新建会话；历史不会被删除。 |
| `/cancel` | 说明前台运行可用 Ctrl-C 取消。 |
| `/exit`、`/quit` | 退出交互会话。 |

`capslock sessions` 按标题列出历史会话；标题默认来自首个问题。使用
`capslock sessions rename <session-id-or-prefix> <title>` 可在会话外重命名，
裸 `capslock resume` 会列出标题、最近更新时间和 Session ID，并支持方向键选择；
`capslock resume <session-id-or-prefix>` 仍可通过完整 ID 或列表显示的唯一短前缀直接恢复。

## 配置与数据位置

- `.capslock/config.toml`：可提交的项目模型、命令、Web 和 MCP 限制配置；不要存 API key。
- `.env` 或 shell：`CAPSLOCK_API_KEY`、`CAPSLOCK_TAVILY_API_KEY` 等密钥。
- `.capslock/mcp.json`：可提交的项目 MCP 声明，禁止 `env`/凭据。
- `.capslock/skills/<name>/`：可提交的工作区 Skill 包；同名时覆盖用户级包。
- `.capslock/local/mcp.json`：本机私有 MCP 覆盖、路径和环境变量；始终忽略。
- `.capslock/state/capslock.sqlite3`、`events.jsonl`、`backups/`：本机工作区状态；始终忽略。
- `${CAPSLOCK_HOME:-~/.capslock}/skills/`：用户级 Skill 包。
- `${CAPSLOCK_HOME:-~/.capslock}/state/memory.sqlite3`：用户级记忆、历史、索引和无正文审计。

`capslock migrate-layout [--scope workspace|user|all] [--dry-run|--apply] [--yes]` 显式迁移旧 config、MCP、state 和 memory 布局，默认 `workspace` dry-run。旧 Skill 路径和旧包格式不被发现或迁移。`CAPSLOCK_HOME` 与 `CAPSLOCK_MEMORY_DATABASE` 仅接受启动 shell 中的绝对路径，项目 `.env` 中的同名值会被忽略。

## 引用与安全边界

- 本地结论使用 `[[evidence:ev_…]]`，最终输出显示路径与行号。
- 外部结论使用 `[[source:<source-id>]]`，最终输出显示标题、URL 与抓取时间。
- 记忆结论使用 `[[memory:mem_…]]`，最终输出显示类型、作用域和来源；自动注入内容始终是不可信数据。
- 消息开头的 `$skill-name [raw arguments]` 可显式加载 Skill；普通对话中模型可依据 16 KiB 名称/描述 catalog 调用 `load_skill`，正文不会预先注入。
- Skill 只接受 `name`、`description`、`license`、`compatibility` 和字符串 `metadata`；工具、权限、hooks、上下文与模型行为字段默认拒绝。
- Skill 正文和资源是不可信上下文，不能改变当前权限模式或安全策略。资源只读、按 run 快照，`scripts/` 没有专用执行入口。
- 支持的文件必须位于工作区内、为 UTF-8 且不超过配置上限；Agent 看不到 `.env`、`.capslock/local/`、`.capslock/state/` 或旧运行文件。`.git` 与非 Skill 的 `.capslock` 内容不可修改，命令 cwd 不能进入整个 `.capslock`。
- URL 抓取拒绝 localhost、私网、链路本地、保留地址和重定向后的非公开地址；仅接受 HTML/纯文本。
