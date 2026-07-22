# CapsLock Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化格式。

## [Unreleased]

## [2.1.0] - 2026-07-21

### Added

- 新增版本化本地工具插件 manifest、`capslock.plugin_sdk` stdio 协议和独立进程运行边界。
- 新增 `capslock plugin install|upgrade|list|show|verify|enable|disable|uninstall`，以及用户级安装注册表、工作区授权和追加审计日志。
- 已启用插件工具通过现有高风险外部动作审批链调用，并在安装摘要或工作区授权变化后拒绝执行。

### Security

- 插件包拒绝符号链接、特殊文件、路径逃逸和超限内容；安装使用内容 SHA-256、暂存校验和原子发布。
- 插件进程只继承最小环境并受超时、输出上限和进程终止约束。插件仍属于用户主动安装的受信本地代码，不构成恶意代码沙箱。

### Changed

- 保持 workspace schema v4、memory schema v3、portable archive v2、JSONL v2 和 config v2 不变。
- 将 `aiosqlite` 约束收紧为 `<0.22`；0.22.1 在发布验证环境中会阻塞连接初始化，完整测试使用 0.20.0 通过。

## [2.0.0] - 2026-07-21

### Changed

- 完成稳定兼容里程碑：删除 1.10.1 的 Python 与 `max_turns` 兼容入口，统一使用窄端口、`ModelRunSession` 和 `max_tool_rounds`。
- 保持 workspace schema v4、memory schema v3、portable archive v2、JSONL v2、权限模型和 canonical 布局兼容。
- 将最低 Python 版本提升到 3.12，并在 Linux/macOS 上执行升级、回滚和 v2 端到端发布评测。

### Removed

- `SessionStore`、`MemoryStore`、同步 Agent/ToolLoop、Classic UI、旧 `chat`/`ask`/`migrate-layout` 及旧 export import 兼容入口。

## [1.10.1] - 2026-07-21

### Changed

- 将应用装配收口到独立 bootstrap，并以中立 ports、共享运行交互状态和显式模型会话消除 runtime/tooling 对具体应用与存储实现的依赖。
- 将 workflow 状态策略、Agent/ToolLoop 步骤、生命周期归档与导入协调、记忆 repository/embedding policy、配置管线和数据库迁移规格拆为独立边界；现有 CLI、配置、schema 与归档格式保持不变。
- 为将在 2.0.0 删除的旧 Python 构造参数和模型路由上下文方法增加默认隐藏的 `DeprecationWarning`。
- 将历史记录中的 `You` 标签改为独立的加粗强调色样式，与 prompt 正文区分。

## [1.10.0] - 2026-07-21

### Added

- 新增交互式 32 工具轮次软限制、固定增量续期和停止总结。
- 新增 `capslock exec` 的工具轮次、工具调用数、时长、token 和美元硬预算参数。
- 新增规范化脱敏工具指纹、连续重复/短周期/失败重试循环检测和结构化 `stopped` 事件。
- 新增 run 治理快照、lineage 累计预算、schema v4 迁移和 portable archive v2 兼容导入。
- TUI 在需要审批的工具调用内阻塞，只弹出是否执行的拒绝/执行选择框且不输出动作载荷，决策结果返回同一个 run；裸 `/permissions` 弹出三档权限选择框。

### Changed

- `max_turns` 语义改为 `max_tool_rounds`；旧配置键和环境变量保留弃用兼容。
- JSONL schema 保持 v2，新增预算事件与稳定停止原因；预算或循环停止退出码为 4。

### Fixed

- 审批选择器改在终端工作线程运行，避免活动 TUI 事件循环中触发 `asyncio.run() cannot be called from a running event loop`。

## [1.9.0] - 2026-07-20

### Added

- 新增交互/非交互 `capslock init`、版本化配置校验迁移和环境变量/keyring 凭据引用。
- 新增本机 `backup create/list/verify/restore`，以及可脱敏、校验、幂等合并的 portable `export/import`。
- 新增结构化 `doctor --json/--strict/--network/--fix`，覆盖配置、凭据、数据库、MCP、Skill 和未完成生命周期操作。
- 新增 import 批次、实体映射和确定性 ID 冲突记录，以及 `/queue start` 显式恢复导入队列。

### Changed

- workspace 与 memory fresh-v2 schema 升至 v3；v1.8 schema v2 在一致性备份后自动迁移。
- provider 配置使用 `credential = "env:NAME"` 或 `credential = "keyring:NAME"`；旧 `api_key_env` 自动迁移。
- 导入的 approved/running action 重置为 pending，并在执行前重新通过当前安全策略和人工确认。

### Security

- portable export 不含模型配置、keyring、`.env`、MCP env 值、embedding 同意或向量，并拒绝路径穿越、符号链接、特殊文件、超限和校验失败的归档。
- 导入的已完成副作用仅作为历史记录，不能在目标工作区执行 undo；任何未完成工作都不会自动启动。

## [1.8.0] - 2026-07-20

### Added

- 新增 OpenAI-compatible 多 provider/model profile、`reasoning`/`fast`/`embedding`/`vision` 角色和确定性路由轨迹。
- 新增 run/session 模型 token 与美元预算、TUI 单次调用确认、模型级延迟/错误/费用统计。
- 新增经过外发字段、记录数和字节数预览确认的真实外部 embedding，以及请求费用与失败审计。
- 新增覆盖工作区问答、编辑、执行、Web、MCP、记忆和 Skill 的固定评测套件与 CI 基线。

### Changed

- workspace 和 memory fresh-v2 schema 升至 v2；相同 application ID 的 schema v1 数据库会在一致性备份后事务迁移。
- 模型 timeout、429 和 5xx 最多重试两次，并只向相同 data-policy 的显式候选降级；流式输出开始后禁止重放。
- JSONL 继续使用 schema version 2，在终止事件中增加逐模型统计；预算停止使用稳定的 `model_budget_exceeded` 错误。

### Security

- 未确认、已撤销或 provider/model/data-policy 不匹配的外部 embedding 配置不会发送网络请求。
- 模型路由不会扩大工具权限，也不会静默切换到数据策略不同的提供方。

### Fixed

- 取消命令 action 时，即使取消落在 `approved -> running` 持久化窗口内，也会先完成进程组和 action 状态清理再向调用方传播取消。

## [1.7.2] - 2026-07-20

### Added

- 新增 `capslock session` 作为 `capslock sessions` 的兼容别名。
- 裸 `capslock session delete` 会打开方向键会话选择器，按 Enter 选择后显示会话标题和短 ID 并请求 `y/n` 二次确认。

### Changed

- 交互删除确认输入 `n` 或直接按 Enter 后返回会话列表，便于重新选择；输入 `y` 后永久删除所选会话。
- 显式 Session ID 或唯一前缀以及 `--yes` 跳过二次确认的原有删除方式保持兼容。

## [1.7.0] - 2026-07-17

### Added

- 新增保留终端 scrollback 的流式工作流 TUI、计划/队列状态区和集中审批中心。
- 新增版本化运行事件、工作项、工具轮次稳定检查点，以及从最近稳定步骤恢复的运行契约。
- 新增 `capslock exec [PROMPT] [--json]` 非交互入口；`ask` 保持兼容，待审批时返回退出码 3。
- 新增会话全文搜索、归档、JSON+Markdown 导出和两阶段永久删除。

### Changed

- workspace schema 升至 v6，增加工作流、步骤、事件、会话 FTS、动作风险快照和 run 级任务顺序。
- TTY 中裸 `capslock` 与 `capslock chat` 默认启动 TUI；`--classic` 保留 v1.6 交互界面。
- OpenAI-compatible 模型适配改为流式优先；不支持流式的模型自动降级为单次完成事件。
- 将工具调用轮次的默认上限从 6 提高到 32；达到上限后仍保留一次不可继续调用工具的最终答案合成机会。

### Security

- 会话导出默认脱敏密钥和敏感载荷，不包含系统提示、原始检查点或其他作用域记忆正文。
- 取消运行会将关联的待审批、已批准或运行中动作标记为 cancelled；已完成副作用不会在恢复时重放。
- 会话永久删除同时清理 session 级记忆、候选、召回与索引，并保留无正文删除审计。

### Fixed

- `max_turns` 现在限制工具调用轮次，并始终保留一次不可继续调用工具的最终答案合成机会。
- TUI 不再同时渲染失败事件和其对应异常，避免同一错误重复显示。
- `capslock resume` 会重放完整消息以及失败或取消 run 的可见部分输出，不再显示为空会话。
- 流式正文通过 prompt-toolkit 的安全终端通道刷新，模型或传输异常不会再终止后台工作队列。
- `Thinking...` 状态增加动态字符指示，并继续保持隐藏思维链不可见。

## [1.6.0] - 2026-07-16

### Added

- 新增成功 run 后的受控记忆候选提取、审核队列，以及 `off`、`review`、`automatic` 三档策略。
- 新增 FTS/语义混合召回、每轮记忆上下文摘要和 `/memory context` 排名解释。
- 新增可选 FastEmbed 与仅回环 OpenAI-compatible HTTP 嵌入后端，失败时自动退化到 FTS。
- 新增候选、提取、多来源、向量与召回审计数据；记忆导出格式升级到 v2。

### Changed

- 用户记忆 schema 升至 v2，升级前自动备份；工作区 schema 保持 v5。
- `automatic` 只采纳用户直接陈述的低风险 workspace/session 候选，global、冲突、敏感与推断内容继续人工审核。

### Security

- 自动记忆必须追溯到提取记录与已完成源 run；失效的自动来源不会进入上下文。
- 记忆和提取输入均作为不可信数据，不能授予工具权限或覆盖文件、命令、Web、MCP 与 Skill 策略。

## [1.5.1] - 2026-07-16

### Added

- 新增 Agent Skills 兼容的 `SKILL.md` 包、16 KiB 描述 catalog，以及模型只读工具 `load_skill` 和 `read_skill_resource`。
- 新增消息开头的 `$skill-name [raw arguments]` 显式调用和交互式 `$` 补全。
- 新增 `python -m capslock` 模块入口，与 `capslock` console script 行为一致。
- 新增 `capslock migrate-layout`，支持 workspace/user/all 的无副作用预览、显式校验迁移和中断恢复。

### Changed

- 项目配置、MCP、Skill 与运行状态统一收口到 `.capslock/`，用户级 Skill 和记忆统一收口到 `${CAPSLOCK_HOME:-~/.capslock}`。
- Skill 正文按需加载到普通 run，使用普通文本与现有 citations；自动和显式调用均使用当前普通工具集与权限模式。
- `/skills` 精简为 `list/show/validate/enable/disable`；工作区 schema 升至 v5，删除旧 `skill_runs`，仅保留当前禁用状态所需的 `skill_settings`。
- 依赖由 `jsonschema` 和 `packaging` 改为安全解析 frontmatter 的 `PyYAML`。
- 已安装旧版 editable/release 环境必须重新执行 `python -m pip install -e '.[dev]'`，否则会缺少 `yaml` 模块或继续显示旧版本元数据。

### Removed

- 删除旧 TOML/Schema Skill 包、`capslock.skills/` 与旧用户 Skill 目录的发现和布局迁移支持。
- 删除旧 Skill run 领域类型、repository、CLI 历史查询和 SQLite 表；迁移前的 v4 数据库备份仍可用于人工恢复。

### Security

- Skill 不能声明工具、权限、hooks、上下文模型或执行行为；这些 frontmatter 字段会被明确拒绝。
- 拒绝重复或未知 frontmatter、越界/符号链接路径、二进制文本读取和超限包；资源在单次 run 内使用只读快照。
- Agent 不可读取 `.env`、`.capslock/local/`、`.capslock/state/` 或旧运行文件；只有项目 Skill 可创建文件提案，且即使 `full_access` 也必须逐次确认。

## [1.5.0] - 未发布，由 1.5.1 取代

v1.5.0 的开发提交未创建 tag 或 GitHub Release。其 TOML、JSON Schema 和独立 Skill run 契约在公开发布前由 v1.5.1 的 `SKILL.md` 渐进加载契约取代。

## [1.4.1] - 2026-07-16

### Added

- 会话标题默认取首个问题，并支持聊天内 `/rename <title>` 及 `capslock sessions rename` 手动重命名。
- 裸 `capslock resume` 提供按最近更新时间排序的方向键会话选择器，显示标题、更新时间和 Session ID。

### Changed

- 裸 `capslock` 现在直接创建交互会话，同时保留 `capslock chat` 兼容入口。
- 工作区 schema 升至 v3；旧会话从首个问题回填标题，手动标题不会被后续问题覆盖。
- `capslock sessions` 以标题为主展示历史；`resume` 和历史重命名支持唯一 Session ID 前缀。
- `prompt-toolkit` 最低版本升至 3.0.52，以使用原生方向键选择控件。

### Fixed

- `/exit`、`/quit`、EOF 或输入阶段取消时自动清除完全为空的会话，同时保留已有内容、手动标题或 session 级记忆的会话。
- 修正恢复选择器在序号前缀、窄终端和中英文混排下的列对齐与换行问题。

## [1.4.0] - 2026-07-16

### Added

- 新增用户级 SQLite 记忆库、global/workspace/session 作用域、FTS5 检索、来源、置信度和过期时间。
- 新增 `/memory list/search/show/add/edit/forget/undo/purge/export/import/status/enable/disable` 手动工作流。
- 新增只读模型记忆检索工具和 `[[memory:...]]` 引用；不自动提取、写入或注入记忆。

### Changed

- 工作区 schema 升至 v2，消息关联 run；记忆失效或 revision 变化后，相关历史 run 不再进入模型上下文。
- `doctor` 报告用户记忆库、FTS5 与当前工作区写策略。

### Security

- 记忆写入和导入统一脱敏密钥、Bearer token、私钥及常见 token；模型工具审计不保存查询或记忆正文。
- `capslock.toml` 可禁止当前项目写记忆，本机开关不能覆盖项目禁令；永久清除擦除正文、索引和历史版本。

## [1.3.2] - 2026-07-15

### Changed

- 引入统一动作主表、版本化 SQLite 迁移和迁移前自动备份，规范文件、命令、Web 与 MCP 的状态和结果类型。
- 增加应用层 `ActionCoordinator`，让 CLI 与模型工具共享审批、拒绝、执行和撤销流程。
- 拆分存储 repositories、模型适配器、工具目录以及 CLI 命令、输入和渲染模块。
- 将 CLI 转换为 package，并拆分组合根、聊天循环、命令分发、动作适配、诊断和 Rich 渲染。
- 将模型、命令、Web、MCP 和运行时配置分组，并集中管理文件类型、风险判断和敏感信息脱敏。

### Fixed

- 每次回答仅返回当前 run 的事件，不再混入先前运行事件。
- SQLite、OpenAI client 和内部 HTTP client 现在具有明确的关闭路径。
- 输入区增加下边框；完整命令、父命令子树和 `/quit`、`/session` 等 alias 的实时补全保持可见且可分发。

## [1.3.1] - 2026-07-15

### Changed

- 使用 `capslock/_version.py` 统一包元数据、CLI、启动界面和 Git tag 的版本。
- 增加 Linux/macOS、Python 3.11/3.12 的测试、lint、构建和安装验证。
- 从实际 `--workspace` 加载环境文件，并固定环境变量、兼容变量、TOML 和默认值的优先级。

### Fixed

- wheel 和 editable 冒烟测试在仓库外运行，避免误用旧的 `build/`、egg-info 或源码目录。
- 为 CLI 启动、`doctor`、真实 MCP 子进程和配置优先级补充稳定回归测试。

### Security

- 清除误提交的编辑器临时文件和 CapsLock 运行数据，扩大 Git 忽略与 CI 禁止名单。
- 轮换可能暴露的凭据，重写包含敏感产物的 Git 历史，并对完整历史执行密钥扫描。

## [1.3.0]

### Added

- v1 只读工作区 Agent：会话恢复、SQLite 轨迹、结构化证据与只读工作区工具。
- v1.1 受控编辑：持久化变更提案、逐次审批、统一 diff、哈希冲突检测与可确认撤销。
- v1.2 受控执行：固定命令模板、逐次审批、超时/取消处理、输出上限、任务状态与 token/费用统计。
- v1.3 受控研究与 MCP：Tavily Web 搜索、受 SSRF 保护的抓取、来源审计、双层 MCP 配置和 stdio 工具调用审批。
- 权限策略：`full_access`、`approve_for_me`、`ask_for_approval` 三档运行时切换，以及风险评估、审计和回滚建议。
- CLI UI：CapsLock 启动卡片、透明语义主题、独立输入区域、权限状态、实时斜杠命令双列补全、前缀过滤和退格刷新。

### Security

- 默认拒绝工作区外访问、二进制/超大文件、网络、写入和任意命令执行。
