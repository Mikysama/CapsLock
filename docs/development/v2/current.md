# 当前运行内核与安全边界

本文描述 CapsLock 2.7.6.1 之后的当前开发边界。产品在本机运行，支持直接能力工具、类型化斜杠命令、可审批 Action、AST 分析与沙箱保护的通用 Shell、session 隔离后台进程、受管理的本地/远程 MCP、LSP、IDE 上下文桥、受控仓库指令，以及单层但可持久化恢复的本机 Agent team；不提供远程控制、后台 daemon、跨机器 Agent 或第三方可执行 Hook。

## 模块边界

- `runtime/` 只包含模型协议、Agent/Run 编排、ToolLoop、上下文、路由和治理。
- `lsp/`、`mcp/`、`shell/` 分别拥有外部协议、子进程、沙箱和生命周期管理。
- `tooling/` 提供 Tool contracts、纯元数据 Catalog、Executor、权限中间件和按能力拆分的模型工具。
- `planning.py`、Plan repository 和 Planning boundary 提供 session 计划状态、不可变 revision、镜像与权限模式之上的只读 overlay。
- runtime、tooling 与 Action handler 通过 `ports/` 使用 LSP/MCP；具体 manager 只由 `composition/` 和 `bootstrap` 构造。
- `bootstrap.WorkspaceApplication.open()` 是唯一顶层组合根，负责资源所有权和 active-workspace 切换。

## Tool Runtime v2

`ToolContract`、`ToolDefinition`、`ResolvedToolPolicy`、`ToolOutcome` 和 `ToolPause` 描述输入/输出 schema、参数级策略、取消行为、富结果及可恢复暂停。contract 可选提供 `aliases`、`intent_tags` 和 `tool_group`，全部内置工具必须声明成功输出 schema。`ToolCatalog` 负责稳定排序、schema fingerprint、deferred discovery、动态刷新和 last-known-good snapshot；单个动态工具 schema 无效时只隔离该工具。`ToolExecutor` 固定执行 normalize、validate、authorize、execute、output validation 和 middleware；`ToolRuntime` 是 Agent/ToolLoop 使用的聚合接口。

只允许只读、并发安全且不改变上下文的调用并发，提交顺序保持模型 tool-call 顺序。额度检查、attempt reservation、历史追加与计数位于同一个异步临界区，完成状态按持久 `attempt_id` 回填；并发批次不能突破 `max_tool_calls`。审批和用户输入可跨进程恢复；副作用执行状态与结果 delivery 状态独立。`ToolOutcome.execution_state` 使用 `not_started | committed | unknown`，旧 `executed` 保持兼容；输出校验失败不得抹除真实执行状态。

只有确定 `not_started` 的 `invalid_tool_arguments` 与 `unsupported_tool` 可进入参数修复轮。修复预算由 `max_argument_repair_attempts` 控制，允许 0、1 或 2；已知工具只暴露原工具，未知名称按名称、别名、描述和参数字段提供最多三个候选。运行时不静默重写路径、命令、URL或业务参数。修复轮正常消耗 token、tool round 和 tool call 预算，预算耗尽后返回 `argument_repair_exhausted` 并解除工具限制。`unknown`、`committed`、权限拒绝和业务执行失败禁止参数修复。

工具选择默认处于 `shadow`：模型仍看到完整目录，runtime 记录候选集合、实际调用召回和混淆信息；`filtered` 只有在评测门槛满足后才用于真实裁剪，`full` 是回滚开关。声明 `strict_tool_calls=true` 的 provider 接收 required+nullable 的 strict schema，调用执行前移除表示未提供可选字段的 `null`；未声明支持的 provider 保持宽松 schema。单项超过 16 KiB 时使用 content-addressed artifact，批次结果受聚合预算限制。旧大型 Tool Result 只有在 Artifact 持久化成功后才能从模型上下文替换；失败必须保留原文并返回 `context_budget_exceeded`。

## 上下文、摘要与 episodic retrieval

原始 transcript、Tool Result 和 Artifact 是事实来源；compaction summary、FTS 和分段缓存均为可重建派生数据。workspace schema 18 继续使用 session-scoped `episodic_documents` 与 FTS5 保存来源 ID、run、类型和分块序号，并为 compaction 记录 summary-policy digest、结果 token 与质量状态。文本 Artifact 按 8 KiB 分块，二进制或 prompt-injection quarantine 只索引安全元数据。每轮自动召回最多 5 条、合计 4 KiB，并以不可信 `episodic_recall` section 注入；显式 `search_session_history` 最多返回 20 条，深度读取仍通过 `read_tool_artifact`。

摘要按完整 turn/tool round 和 token 预算进行 map-reduce；超大单条消息继续分片，不允许对整体来源执行字符级截断。summary v3 保留来源覆盖、逐项 source map、用户反馈、当前工作、代码符号、验证状态、降级说明与引用式 working set，v1/v2 继续只读兼容。每个 map 分段按 source digest、模型 profile 与 summary-policy digest 缓存；focus 是独立的低优先级策略，不能改变 schema、安全或来源要求。文件及 Skill 正文不会因恢复自动注入。portable export 不包含 episodic 或摘要分段缓存；升级、导入、branch 和 rewind 必须幂等重建索引。

## 外部执行

Shell 在 Linux Bubblewrap 或 macOS sandbox-exec 中执行，系统只读、默认断网；沙箱不可用时 fail closed。`approve_for_me` 仅自动批准 `pwd`、受限 Git 查询及只消费标准输入的安全管道过滤器，并将工作区只读挂载。模型分类器只记录风险提示，不能将白名单外命令升级为 allow；显式批准或明确权限规则仍可使用可写工作区。后台任务由 session-scoped process manager 管理并支持有界输出、TERM→KILL 取消和统一临时目录清理。

MCP 使用唯一的受管理长连接路径，负责 stdio/Streamable HTTP/SSE、tools/resources discovery、list-changed、重连、取消和 workspace 切换；远程只接受公开 HTTPS，凭据只从私有配置引用解析。只有 `readOnlyHint=true` 的 stdio 调用可在断线后重连并重试一次；写调用返回 `unknown`，不得自动重放。插件 envelope 携带稳定 invocation ID，支持的插件可将其作为幂等键。LSP 使用已安装或显式配置的 server，在只读、禁网沙箱中运行，支持请求取消、didOpen/didChange、崩溃恢复和空闲回收。

## 权限与插件

结构化权限来自用户、项目、本地和 session 规则。统一内核先执行参数规范化和 capability 边界，再按 hard deny/hard ask、deny、ask、allow、permission mode 判定；文件、Shell、Web、MCP/插件分别解释自身参数。Action 只保留风险展示、revalidate、执行与 undo，不再用独立风险策略覆盖统一决定。

插件 manifest、grant 和 stdio protocol 使用版本 4。普通调用使用独立沙箱进程；只有显式授权的 session 生命周期插件可以池化。插件 capability 必须是 manifest grant 的子集，宿主 broker 重新执行文件、网络、进程和 credential 边界。

## Plan Mode

Plan Mode 不扩展 `PermissionMode` 或 `RunMode`。Planning boundary 位于权限中间件之前，激活时仅允许显式标记的本地只读工具、用户提问和计划控制工具；Shell、Web、MCP、插件、Action、worktree、memory/task mutation 与子 Agent 均 fail closed。ToolLoop 每轮从数据库刷新 plan attachment 和工具 schema，恢复、compaction 与旧 invocation 不能依赖过期内存标志绕过边界。

计划正文保存在不可变 revision 中并绑定 SHA-256，`.capslock/state/plans/` 只保存可重建镜像。ToolLoop 将 active revision 的完整 JSON 快照注入每次模型调用；overlay 结束后，最近 plan 仍作为带状态的历史上下文保留，包括 `rejected`。历史快照必须标记为非指令、非批准、非权限，并对 XML 边界字符转义；工具目录只由 active attachment 决定。提交批准幂等创建新的 implementation work item；新 run 恢复普通目录但仍使用原权限内核，批准计划不授予实施权限。

## CLI 状态与恢复

inline 与 fullscreen 共用语义 theme token、选择/问题 view model、审批 presentation 和前台队列控制器。用户 prompt 使用浅灰背景、深色前景与焦点边线，CapsLock Markdown 回答保持透明；`NO_COLOR` 移除语义色和 prompt 背景。fullscreen 的 header 固定单行，footer 按 `<72`、`72-99`、`>=100` 列裁剪字段，Composer 在 3-8 行内增长，低终端上限为 5 行，补全浮层不参与 transcript 布局。

`context_updated` 是不写入 run journal 的非终止状态事件，先发送 context build estimate，再在 provider usage 可用时发送实际 input tokens；JSONL schema 仍为 3。`ForegroundRunController` 使用 `deque + asyncio.Condition`，只允许召回最新未开始项，持久取消旧 work item 后用新 ID 恢复原队列位置。resume transcript 以 run 插入顺序为主序，每个 agent run 内 user 必须先于 assistant；时间戳只描述发生时间，不能承担跨表全序。

## 记忆与指令

普通记忆始终作为不可信数据注入。run 完成只排队幂等的 extraction job，后台 worker 读取完整用户 transcript，按 40k 字符预算分段并复用未变化 map digest，再在 reduce 阶段合并跨轮偏好。Candidate 保存多个逐字来源；提取分数只作诊断，独立验证器不接收该分数，并输出支持性、指令属性、durability 建议和原始分数。

自动采用只使用与模型 profile、验证 Prompt 版本精确绑定的校准文件；无有效校准时 fail closed 为 review-only。直接陈述阈值为 0.95，跨轮推断阈值为 0.98 且至少需要两个独立用户来源。global、冲突、无来源、指令型或验证失败的 Candidate 始终审核。`temporary` 默认 7 天 TTL，`session` 绑定 lifecycle owner，`project` 绑定稳定 `project_instance_id`，`durable` 不因 session 或项目实例变化清理。召回批量融合词法与语义排名；edit、forget、purge 或来源失效通过 revision digest 使旧 compaction 失效。

consolidation 只自动合并完全重复的 automatic memory、遗忘来源已全部失效的 automatic memory，以及降级已确认 supersedes 的旧 automatic memory；冲突、manual/reviewed 修改和 instruction promotion 均进入 review。仓库指令按受控层级加载，拒绝 include 与符号链接，且始终低于系统安全、工具权限和审批策略。子 Agent 只能提交带 namespace 和验证 provenance 的 memory proposal，由父进程持久化晋升。

## Agent team、任务图与恢复

旧 `delegate_agents` 批量委派接口继续保持兼容；新控制面以 session-owned team 为边界，提供命名的 persistent worker、不可变任务契约和显式依赖图。任务经过 `blocked → ready → claimed → running`，claim token、attempt ordinal 与 contract SHA-256 共同防止并发重复领取。依赖只在前置任务成功后释放；队列按 priority 和创建顺序确定性调度，同一 worker 同时只运行一个任务。

worker workspace 支持 `snapshot`、`worktree` 和 `shared_read`。snapshot/worktree 都持久化父工作区 baseline，产物发布继续执行 allowlist、SHA-256、父文件 CAS 复验、批量替换与失败回滚；worktree 还要求父仓库干净并固定 base commit。`shared_read` 暴露实时父目录但 capability policy 强制只读，不创建可发布的私有副本。worker profile 和任务 contract 只能收窄模型、能力、路径与预算，不能扩大父 session 权限；子 Agent 仍不获得二次委派能力。

每次执行写入独立 attempt、预算 reserve/settle/release ledger、approval link 和 checkpoint。崩溃恢复必须复用原 attempt、child session/workspace 和已校验的 contract digest；只有标记为 resumable 的 checkpoint 可由 `resume_agent` 恢复，未知副作用不会自动重放。team mailbox 支持点对点或广播，但 payload 始终标记为 `untrusted_agent` 并继续受脱敏、大小、TTL、digest 和归属校验。Plan Mode 仍在普通权限之前拒绝所有 team mutation 与子 Agent 执行。

## 当前数据协议

当前格式为 config 10、workspace schema 18、memory schema 5、portable archive 6、session export 6、JSONL schema 3、IDE Bridge protocol 1 和 plugin protocol 4。workspace 启动支持 backup-first 的 v6-v17→v18 升级；schema 18 将旧 Agent task/mailbox 数据迁入默认 team，并新增 worker、dependency、attempt、checkpoint、budget ledger、approval link 与 workspace baseline。重建表的迁移必须显式列出源、目标字段，禁止依赖物理列顺序。memory schema v3-v4 与 config v3-v9 自动备份并升级。迁移失败保留原库和备份，不继续部分升级。

## 发布门禁

合并前运行 compileall、Ruff、全量 pytest、真实迁移 fixture、确定性/live Agent 工具评测、位置敏感 context 评测、memory calibration 评测、依赖审计和 wheel/sdist 冒烟。Agent eval 必须校验 pytest 退出码、收集数量和场景数，并统计首次工具选择、首次 schema 通过、一次修复成功、最终成功和重复副作用；零收集或少收集是 `test_runner` 失败。filtered 上线要求 deterministic 候选召回 100%、任务成功率不低于 full，且 live 召回至少 99%。context 确定性评测要求 transcript、compaction、Tool Result、Artifact 在 front/middle/tail 全部找回；memory 自动采纳要求 precision ≥98%、跨轮 recall ≥90%、ECE ≤0.05，否则相应 profile 保持 review-only。边界测试必须验证 runtime/tooling 不依赖具体 LSP/MCP manager、旧 `*_runtime.py` 模块不存在、Shell 分类规则只有一个实现，并确保轻量 CLI 不导入 MCP SDK或启动集成进程。

行为默认值变更还必须通过 `evaluate_policies.py`。PR 只运行 deterministic 阶段；screen 与 confirm 必须显式指定 provider、model、前一阶段报告和 candidate-aware probe。内置 Runtime probe 为每个 candidate 创建隔离的真实 `WorkspaceApplication`/`AgentSession`，并验证候选值确实进入 Runtime、Context、Memory 或 Collaboration 对象；仅做 Provider 健康检查的普通模型 probe 会触发 `candidate_policy_injection` 硬门禁。参数选择使用排除 capacity case 的正常工作负载 `success_rate`，同时独立报告 raw success、capacity coverage、safe stop 和 Provider error；live 正常样本不足 30 时触发 `insufficient_power`。评测只能生成 recommendation manifest，不能自动修改默认值。安全硬上限不参与普通调参。
