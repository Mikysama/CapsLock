# M1–M4 可靠性优化（2.7.6.4）

本版本聚焦执行正确性、长任务恢复与可解释计量；离线通过不代表真实模型成功率或成本已经提高。默认权限、Memory 权重、Agent 并发和工具选择 shadow 模式不变。上下文压缩在有有效用量与增长观测时改用剩余输入预算、输出预留、安全余量和预计增长量判断；80% 仅作为观测不足时的兜底。

## 行为变化

- 流式模型响应必须具有唯一成功终态，且验证整个响应结束后才执行工具。incomplete、failed、错误事件及无终态 EOF 均走 failed。部分文字与 reasoning 保留，失败响应的工具不执行；输出后不自动重试。
- `/model [profile-id]` 使用实际配置；session 保存 profile 身份。同名模型需明确选择，已删除配置不影响查看历史，但新 run 必须重选。fallback 使用自身模型、窗口、价格及能力配置。
- `search_files` 必须安装 `rg`；默认正则、大小写敏感、不含隐藏文件。支持 `mode="literal"`、`case_sensitive=false`、`include_hidden=true`。无匹配与执行失败分开；忽略用户 ripgrep 配置，仍遵守项目 ignore 与路径权限。`doctor` 提示安装方法，不自动安装。
- `process_output` 支持 stdout/stderr 字节 offset 与 `wait_ms`（0–30000，默认1000），输出 next offset、状态与截断信息。仅真实内置轮询可用进程进展避免重复工具误判；120秒无输出或状态变化判断停滞，全局预算仍生效。
- SDK 内部重试关闭，由 Router 统一处理；支持 Retry-After 秒数/日期、抖动退避与共享 deadline。认证和参数错误不重试，已产生输出的请求不重放。
- 模型调用记录 profile、Provider、模型、用量来源、缓存输入、reasoning 明细、请求 ID、首 token 延迟、重试和价格快照。未知用量明确标识，历史缺失值不伪造。reasoning 不重复计入输出，未配置缓存价格按普通输入价保守估算。

## 工作区编辑预设

交互式执行 `/permissions preset workspace-edit`，阅读作用范围并明确确认后写入 workspace-local 规则；`/permissions preset workspace-edit remove` 撤销。仅为 create_file、edit_file、write_file、edit_notebook 增加允许规则。原有 deny/ask、Plan Mode、文件策略和硬边界继续优先；不增加 Shell、网络、删除、MCP、插件或工作区外授权。两套 TUI 共用同一逻辑；预设 ID 与用户规则冲突时拒绝修改。

## 结构化输出

```sh
capslock exec --output-schema result.schema.json '生成满足 schema 的结果'
capslock exec --json --output-schema result.schema.json '生成满足 schema 的结果'
```

启动前本地校验 Schema，禁止远程引用和引用基址变化。最终正文须为标准 JSON 并通过本地 Schema 校验；否则返回 `output_schema_validation_failed` 并保留原文，不自动增加修复请求。`--json` 仍是 schema_version 3 的 JSONL；成功事件的 data 增加 `structured_output`，保留 answer。

## 本地集成

`capslock app-server --stdio` 使用版本握手的 JSON-RPC 2.0，stdout 仅协议、stderr 诊断。会话、run、审批、输入及事件复用运行内核。启动 request ID 持久幂等；断开连接取消执行并等待清理。当前每个连接绑定一个 session，活跃执行期间不能换 session，session 内串行执行。相同数据库只允许一个独立客户端持有恢复所有权；其他客户端收到 busy 错误，受信任进程内子 Agent 可共享；未增加 HTTP、daemon 或 IDE 扩展。

完整契约和示例见 [app-server](app-server.md) 与 `examples/app_server_client.py`。

## 离线验收与真实评测

```sh
python scripts/evaluate_offline.py --output /tmp/capslock-offline.json
python scripts/evaluate_offline.py --output /tmp/capslock-offline.json --resume
```

固定60个可执行内核场景，五类各12个，带 fixture、断言与 grader。报告明确标注离线脚本内核回归；外部模型评测需单独指定 profile、权限、预算、seed 与固定 revision。支持 dry-run、费用上限、恢复及配对报告，基础设施失败单列。成功数为0时每成功任务成本为 null。工具选择实验只有通过硬门禁、召回率99%、schema token中位数下降20%和真实确认配对非劣界限后才具备晋级资格；代码不会自动改默认值。

Linux/macOS CI 都安装并检查 ripgrep、执行完整测试和离线门禁。开发机上的 Linux 结果不能替代 macOS CI。未执行付费评测，不对真实任务质量或成本改善作结论。

## 数据兼容与回退

| 数据 | 开发格式 | 兼容入口 |
|---|---|---|
| workspace SQLite | 21 | 6–20 backup-first 升级 |
| Memory SQLite | 6 | 保留原迁移行为 |
| config | 14 | 3–13 备份后原子升级；13→14 不改默认配置 |
| portable archive | 8 | 读取3–7，保留缺失字段为空 |
| session JSON export | 8 | 现有导出接口；不新增独立 JSON 导入器 |
| JSONL events | 3 | 仅增加可选 data 字段 |

数据库备份位于数据库同目录 `backups/capslock-v<source>-<timestamp>.sqlite3`；配置备份为 `config.toml.v<source>-<timestamp>.bak`。升级先备份，失败恢复，重复启动不重复升级。

回退时先停止所有 CapsLock 客户端，保留当前数据库/配置及其 WAL 状态，再用对应升级前备份恢复数据库和配置，最后启动旧二进制。不能让旧二进制直接打开新 schema；也不能把升级前数据库与升级后的 WAL/SHM 混用。回退会丢失升级之后新增的数据，应先另存当前数据并验证备份完整性。

## 本次验证记录（2026-09-26，Linux / Python 3.12）

- 完整 pytest：1265 passed、1 skipped。
- 固定离线内核门禁：60/60，通过；0 基础设施失败，0 Provider 调用。此次运行一次，三次重复稳定性字段保持 null，未伪造重复结果。
- 普通磁盘 profile 与迁移回归包含在全量 pytest 中，覆盖备份、恢复及归档兼容。
- 确定性 Agent 评测无回归；Memory 评测 100/100。
- Ruff lint/format、compileall、依赖一致性、仓库卫生、diff 空白检查、wheel/sdist 构建、Twine 检查和版本一致性检查通过。
- 未执行 macOS 本地验证、付费真实模型确认或发布部署。macOS 和完整发布流水线仍须 CI 验证。

离线报告保存在本机 `/tmp/capslock-offline-2.7.6.4.json`；跨平台与完整发布流水线仍以 CI 结果为准。
