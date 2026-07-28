# 当前运行内核与安全边界

本文描述 CapsLock 2.7.0 的开发边界。产品在本机运行，支持直接能力工具、类型化斜杠命令、可审批 Action、AST 分析与沙箱保护的通用 Shell、session 隔离后台进程、受管理的本地/远程 MCP、LSP、IDE 上下文桥、受控仓库指令和单层子 Agent；不提供远程控制、后台 daemon 或第三方可执行 Hook。

## 模块边界

- `runtime/` 只包含模型协议、Agent/Run 编排、ToolLoop、上下文、路由和治理。
- `lsp/`、`mcp/`、`shell/` 分别拥有外部协议、子进程、沙箱和生命周期管理。
- `tooling/` 提供 Tool contracts、纯元数据 Catalog、Executor、权限中间件和按能力拆分的模型工具。
- `planning.py`、Plan repository 和 Planning boundary 提供 session 计划状态、不可变 revision、镜像与权限模式之上的只读 overlay。
- runtime、tooling 与 Action handler 通过 `ports/` 使用 LSP/MCP；具体 manager 只由 `composition/` 和 `bootstrap` 构造。
- `bootstrap.WorkspaceApplication.open()` 是唯一顶层组合根，负责资源所有权和 active-workspace 切换。

## Tool Runtime v2

`ToolContract`、`ToolDefinition`、`ResolvedToolPolicy`、`ToolOutcome` 和 `ToolPause` 描述输入/输出 schema、参数级策略、取消行为、富结果及可恢复暂停。`ToolCatalog` 只负责稳定排序、schema fingerprint、deferred discovery 和动态刷新；`ToolExecutor` 固定执行 normalize、validate、authorize、execute、output validation 和 middleware；`ToolRuntime` 是 Agent/ToolLoop 使用的聚合接口。

只允许只读、并发安全且不改变上下文的调用并发，提交顺序保持模型 tool-call 顺序。审批和用户输入可跨进程恢复；副作用执行状态与结果 delivery 状态独立。单项超过 16 KiB 时使用 content-addressed artifact，批次结果受聚合预算限制。

## 外部执行

Shell 在 Linux Bubblewrap 或 macOS sandbox-exec 中执行，工作区可写、系统只读、默认断网；沙箱不可用时 fail closed。确定性规则可 hard deny 危险命令，快速分类器只能在默认无网络沙箱和高置信度边界内自动 allow。后台任务由 session-scoped process manager 管理并支持有界输出和 TERM→KILL 取消。

MCP 使用唯一的受管理长连接路径，负责 stdio/Streamable HTTP/SSE、tools/resources discovery、list-changed、重连、取消和 workspace 切换；远程只接受公开 HTTPS，凭据只从私有配置引用解析，mutating call 不自动重放。LSP 使用已安装或显式配置的 server，在只读、禁网沙箱中运行，支持请求取消、didOpen/didChange、崩溃恢复和空闲回收。

## 权限与插件

结构化权限来自用户、项目、本地和 session 规则。统一内核先执行参数规范化和 capability 边界，再按 hard deny/hard ask、deny、ask、allow、permission mode 判定；文件、Shell、Web、MCP/插件分别解释自身参数。Action 只保留风险展示、revalidate、执行与 undo，不再用独立风险策略覆盖统一决定。

插件 manifest、grant 和 stdio protocol 使用版本 4。普通调用使用独立沙箱进程；只有显式授权的 session 生命周期插件可以池化。插件 capability 必须是 manifest grant 的子集，宿主 broker 重新执行文件、网络、进程和 credential 边界。

## Plan Mode

Plan Mode 不扩展 `PermissionMode` 或 `RunMode`。Planning boundary 位于权限中间件之前，激活时仅允许显式标记的本地只读工具、用户提问和计划控制工具；Shell、Web、MCP、插件、Action、worktree、memory/task mutation 与子 Agent 均 fail closed。ToolLoop 每轮从数据库刷新 plan attachment 和工具 schema，恢复、compaction 与旧 invocation 不能依赖过期内存标志绕过边界。

计划正文保存在不可变 revision 中并绑定 SHA-256，`.capslock/state/plans/` 只保存可重建镜像。提交批准幂等创建新的 implementation work item；新 run 恢复普通目录但仍使用原权限内核，批准计划不授予实施权限。

## 记忆与指令

普通记忆始终作为不可信数据注入。run 完成只排队幂等的 extraction job，后台 worker 从用户消息和已验证 evidence/source 构造严格来源 envelope；自动采用受来源、置信度、风险和 scope 门槛约束。召回批量融合词法与语义排名，语义不可用时降级为词法，并记录过滤和选择原因。edit、forget、purge 或来源失效会通过 revision digest 使旧 compaction 失效。

consolidation 只自动合并完全重复的 automatic memory、遗忘来源已全部失效的 automatic memory，以及降级已确认 supersedes 的旧 automatic memory；冲突、manual/reviewed 修改和 instruction promotion 均进入 review。仓库指令按受控层级加载，拒绝 include 与符号链接，且始终低于系统安全、工具权限和审批策略。子 Agent 只能提交带 namespace 和验证 provenance 的 memory proposal，由父进程持久化晋升。

## 当前数据协议

当前格式为 config 9、workspace schema 14、memory schema 4、portable archive 6、session export 6、JSONL schema 3、IDE Bridge protocol 1 和 plugin protocol 4。workspace 启动支持 backup-first、事务化的 v6-v13→v14 升级；memory schema v3 与 config v3-v8 自动备份并升级。迁移失败保留原库和备份，不继续部分升级。

## 发布门禁

合并前运行 compileall、Ruff、全量 pytest、真实迁移 fixture、确定性 Agent/Memory 评测、依赖审计和 wheel/sdist 冒烟。边界测试必须验证 runtime/tooling 不依赖具体 LSP/MCP manager、旧 `*_runtime.py` 模块不存在、Shell 分类规则只有一个实现，并确保轻量 CLI 不导入 MCP SDK或启动集成进程。
