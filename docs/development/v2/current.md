# 当前运行内核与安全边界

本文描述 CapsLock 2.3.1 的开发边界。产品在本机运行，支持直接能力工具、可审批 Action、受沙箱保护的通用 Shell、session 隔离后台进程、受管理 MCP/LSP 和单层子 Agent；不提供远程控制、后台 daemon 或第三方可执行 Hook。

## 模块边界

- `runtime/` 只包含模型协议、Agent/Run 编排、ToolLoop、上下文、路由和治理。
- `lsp/`、`mcp/`、`shell/` 分别拥有外部协议、子进程、沙箱和生命周期管理。
- `tooling/` 提供 Tool contracts、纯元数据 Catalog、Executor、权限中间件和按能力拆分的模型工具。
- runtime、tooling 与 Action handler 通过 `ports/` 使用 LSP/MCP；具体 manager 只由 `composition/` 和 `bootstrap` 构造。
- `bootstrap.WorkspaceApplication.open()` 是唯一顶层组合根，负责资源所有权和 active-workspace 切换。

## Tool Runtime v2

`ToolContract`、`ToolDefinition`、`ResolvedToolPolicy`、`ToolOutcome` 和 `ToolPause` 描述输入/输出 schema、参数级策略、取消行为、富结果及可恢复暂停。`ToolCatalog` 只负责稳定排序、schema fingerprint、deferred discovery 和动态刷新；`ToolExecutor` 固定执行 normalize、validate、authorize、execute、output validation 和 middleware；`ToolRuntime` 是 Agent/ToolLoop 使用的聚合接口。

只允许只读、并发安全且不改变上下文的调用并发，提交顺序保持模型 tool-call 顺序。审批和用户输入可跨进程恢复；副作用执行状态与结果 delivery 状态独立。单项超过 16 KiB 时使用 content-addressed artifact，批次结果受聚合预算限制。

## 外部执行

Shell 在 Linux Bubblewrap 或 macOS sandbox-exec 中执行，工作区可写、系统只读、默认断网；沙箱不可用时 fail closed。确定性规则可 hard deny 危险命令，快速分类器只能在默认无网络沙箱和高置信度边界内自动 allow。后台任务由 session-scoped process manager 管理并支持有界输出和 TERM→KILL 取消。

MCP 使用唯一的受管理长连接路径，负责 tools/resources discovery、list-changed、重连、取消和 workspace 切换；不存在单次 stdio fallback。LSP 使用已安装或显式配置的 server，在只读、禁网沙箱中运行，支持请求取消、didOpen/didChange、崩溃恢复和空闲回收。

## 权限与插件

结构化权限来自用户、项目、本地和 session 规则，合并顺序为 hard deny、deny、ask、allow、permission mode。Action 继续提供审批、revalidate、undo 和持久化审计。

插件 manifest、grant 和 stdio protocol 使用版本 4。普通调用使用独立沙箱进程；只有显式授权的 session 生命周期插件可以池化。插件 capability 必须是 manifest grant 的子集，宿主 broker 重新执行文件、网络、进程和 credential 边界。

## 当前数据协议

当前格式为 config 5、workspace schema 8、memory schema 3、portable archive 3、JSONL schema 3 和 plugin protocol 4。workspace 启动支持 backup-first、事务化的 v6/v7→v8 升级；config v3/v4 自动备份并转换为 v5。迁移失败保留原库和备份，不继续部分升级。

## 发布门禁

合并前运行 compileall、Ruff、全量 pytest、真实迁移 fixture、确定性 Agent/Memory 评测、依赖审计和 wheel/sdist 冒烟。边界测试必须验证 runtime/tooling 不依赖具体 LSP/MCP manager、旧 `*_runtime.py` 模块不存在、Shell 分类规则只有一个实现，并确保轻量 CLI 不导入 MCP SDK或启动集成进程。
