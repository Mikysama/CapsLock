# 代码清理记录（2026-09-24）

本次在可靠性优化分支上增量收敛实现，不修改 workspace、Memory、config、archive 的版本和历史读取路径。

## 行为和边界

- Slash command 使用统一 handler 映射；补齐 `/instructions`，`CommandOutcome` 只与同类型结果比较。CLI 进程退出码不变。
- `glob_files` 和 `search_files` 共用 rg 子进程生命周期。缺失 rg 明确报错；不再使用 Python glob/gitignore fallback。默认 `*` 遵循 rg ignore，显式正向 glob 保留 rg 的 override 规则；隐藏文件必须显式启用。截断时只排序已保留的结果前缀，返回 `truncated` 与 `stop_reason`。
- `/mcp list|status|tools` 读取 manager 的安全状态快照，显示 enabled、connected、allowed_tools、available_tools 和错误；不重新解析一套不同策略的配置，不输出凭证。
- exec JSONL 和 stdio JSON-RPC 共用 version 3 事件序列化；RPC 增加与 exec 一致的顶层 status。
- TUI 和 app-server 共用 ForegroundRunController。关闭会取消活动任务和排队任务；不会在关闭后启动排队模型请求。RPC 幂等入队归 WorkItemRepository 管理，保留原有持久化 key，旧请求仍可去重。
- 应用与 Provider 工厂位于 composition，CLI 保留明确导出，app-server 不依赖 CLI。
- 权限参数摘要和工具结果封装复用现有责任边界，避免审批与执行两处算法漂移。

## 兼容与回退

保留数据库迁移、归档兼容、历史会话与两套 TUI。Python 内部接口的类型化清理不承诺保留无效参数或跨类型相等判断；调用方应使用类型化结果和 profile 配置。

本次不升级持久化格式。若需回退，恢复本次清理前的代码即可；不要回滚或覆盖此前可靠性优化及用户配置。工作区内未执行提交或推送。

## Profile 与上下文接口

- `Settings.model_config` 保留为只读派生视图，运行配置以 profiles/providers/routing 为准；不再接受构造或 dataclass replace 时注入第二份模型配置。
- 同 Provider、同模型的 profiles 可使用不同输出限制；adapter 不再维护按模型名称索引的限制，Router 的每请求 profile 限制是唯一来源。
- 没有可用 Provider 凭证时明确报错，不创建被丢弃的客户端。
- 摘要缓存遵循包含 policy digest 的协议，不再捕获 TypeError 后重放旧签名；无缓存实现仍受支持。删除未生效的 `micro_compact(preserve_messages=...)` 参数。
- 取消排队的暂停恢复任务时，run、work item、工具调用和待处理请求在同一事务中结束，并保留已记录用量；禁止留下无法恢复的半取消状态。

取消待审批的已提交计划时，将计划恢复为可编辑草稿，取消关联待处理请求并保留所有计划修订；不会遗留没有审批入口的 `awaiting_approval` 状态。

## 验证结果

- 最终完整 pytest：680 passed，1 skipped（2026-09-24；临时数据库使用内存文件系统加速）。
- 普通磁盘已执行完整首轮测试及修正后的 TUI、边界、计划恢复 69 项测试；新增持久化取消与计划恢复 3 项测试通过。
- Ruff lint、format check、compileall、仓库卫生检查和 git diff --check 全部通过。
- 本环境仅验证 Linux，未宣称完成 macOS 验收；未发起付费模型评测。
