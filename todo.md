# TODO list

当前没有未完成的 Lost in the Middle 或记忆召回缺陷。

已完成项：

- workspace schema 16 提供 transcript、Tool Result 与 Artifact 的 session-scoped episodic retrieval。
- 上下文压缩先持久化 Artifact，并使用可缓存的分层 map-reduce 摘要，不再字符截断。
- `search_session_history` 支持模型主动检索原始历史。
- memory schema 5 提供独立验证分数、多来源 Candidate 和完整 durability 生命周期。
- 自动提取读取完整用户 transcript；普通项目事实不再统一标记为 `instruction_proposal`。
- 位置敏感确定性评测覆盖前部、中部、尾部以及 transcript、compaction、Tool Result、Artifact。

回归命令：

```text
python scripts/evaluate_context.py
python scripts/evaluate_memory_calibration.py
pytest -q
```

当前确定性基线：位置评测 12/12，通过率 100%，position gap 0；记忆校准 automatic precision 1.0、supported recall 1.0、ECE 0.0192。
