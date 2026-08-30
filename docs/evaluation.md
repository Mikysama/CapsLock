# 行为指标评测

CapsLock 使用版本化任务集和参数矩阵评估运行时、上下文、循环检测、Memory 与子 Agent 的行为默认值。默认矩阵位于 `evaluations/core-v1.toml`；调优集固定为 220 条，隔离确认集固定为 60 条。

## 运行漏斗

```bash
python scripts/evaluate_policies.py \
  --stage deterministic \
  --matrix evaluations/core-v1.toml \
  --output /tmp/capslock-policy-deterministic

python scripts/evaluate_policies.py \
  --stage deterministic \
  --strategy refine \
  --matrix evaluations/core-v1.toml \
  --input-report /tmp/capslock-policy-deterministic/report.json \
  --output /tmp/capslock-policy-refined

python scripts/evaluate_policies.py \
  --stage deterministic \
  --strategy memory-optimize \
  --matrix evaluations/core-v1.toml \
  --input-report /tmp/capslock-policy-refined/report.json \
  --output /tmp/capslock-policy-memory-weights

PROVIDER_API_KEY=... python scripts/evaluate_policies.py \
  --stage screen \
  --matrix evaluations/core-v1.toml \
  --provider provider \
  --model low-cost-model \
  --repetitions 2 \
  --input-report /tmp/capslock-policy-memory-weights/report.json \
  --output /tmp/capslock-policy-screen

PROVIDER_API_KEY=... python scripts/evaluate_policies.py \
  --stage confirm \
  --matrix evaluations/core-v1.toml \
  --provider provider \
  --model target-model \
  --repetitions 3 \
  --input-report /tmp/capslock-policy-screen/report.json \
  --output /tmp/capslock-policy-confirm

PROVIDER_API_KEY=... python scripts/evaluate_policies.py \
  --stage confirm \
  --matrix evaluations/core-v1.toml \
  --provider provider \
  --model target-model \
  --repetitions 3 \
  --seed 20260829 \
  --input-report /tmp/capslock-policy-screen/report.json \
  --peer-report /tmp/capslock-policy-confirm/report.json \
  --output /tmp/capslock-policy-confirm-peer
```

线上参数评测必须提供 candidate-aware probe：
`--candidate-probe your_adapter:probe`（仓库内置实现为
`capslock.evaluation.runtime_probe:probe`）。该异步函数接收
`(task, candidate, provider, model)`，必须用 candidate 值构造真实的
`AgentSession`、`ContextBudgetManager`、`MemoryService` 或
`CollaborationService`。返回值可为兼容的
`(success, input_tokens, output_tokens)`，或包含这些字段及
`stop_reason`、`tool_calls`、`compaction_events`、`approval_events`、
`conflicts` 和 `metrics` 的对象。
Runtime probe 只有在终态为 `completed`、没有 `stop_reason` 且答案匹配时才算成功；
即使终态带有答案，`max_tool_rounds` 等 budget exhaustion 也算失败。
未提供时的默认模型探针只验证 Provider 可用性，报告会触发
`candidate_policy_injection` 门禁，不会生成线上参数推荐。
对于返回 reasoning 而不返回正文的 DeepSeek 兼容端点，探针会自动发送
`thinking.type=disabled`；其他 Provider 可通过
`<PROVIDER>_DISABLE_THINKING=1` 显式启用相同行为。

首次 confirm 的建议固定为 `requires_second_confirmation`。使用不同 `--seed` 再运行一次，并通过 `--peer-report` 指向第一份 confirm 报告；只有两次选择相同候选时，第二份报告才会输出 `request_human_approval`。

Provider 名称用于解析 `<PROVIDER>_API_KEY` 和可选的 `<PROVIDER>_BASE_URL`。Live 阶段不回退到 `CAPSLOCK_API_KEY`，避免意外使用生产凭据。

`refine` 根据 OAT 报告为每个参数保留两个最优可行档位，再执行子系统内组合。`memory-optimize` 只对入围 Memory 配置运行带固定 seed 的高斯过程 UCB 权重搜索；后续报告会作为观测输入改善下一轮提案。

每次运行输出：

- `samples.jsonl`：逐任务原始样本、停止原因、token、成本和安全指标。
- `report.json`：环境清单、置信区间、硬门禁、Pareto 集合与报告哈希。
- `recommendation.json`：旧值、新值、收益、风险、回滚值和人工审批动作。
- `pareto.svg`：成本与 p95 延迟图。
- `reproduce.txt`：复现命令。

## 成功率口径

`success_rate` 是正常工作负载的主指标：正常样本中的成功数除以正常样本数。
正常样本必须同时满足 `in_budget=true` 且 `capacity_case=false`。参数选择、
非劣置信区间和 Pareto 排名都使用这一口径。容量压力任务不会降低质量成功率，
但必须通过独立的容量和安全停止指标报告。

- `success_rate`：正常工作负载成功率（推荐用于比较和选型）。
- `raw_success_rate`：全部样本成功数除以全部样本数，仅用于审计和与旧报告对照。
- `quality_success_rate`：`success_rate` 的兼容别名，将在后续报告版本移除。
- `capacity_coverage`：容量压力任务中被当前候选完整覆盖的比例。
- `safe_stop_rate`：候选无法覆盖任务时，以预期停止原因安全结束的比例。
- `provider_error_rate`：在线阶段 Provider 错误样本占比；非零时触发
  `provider_health` 硬门禁，不生成候选建议。

例如当前 tune 集的 220 个样本中，167 个是正常样本、119 个成功：
`success_rate = 119 / 167 = 71.26%`；全部样本口径为
`raw_success_rate = 119 / 220 = 54.09%`。后者较低只说明其中有容量压力样本，
不能单独解释为正常工作流的失败率。
推荐对象中的 `success_gain` 使用主指标；`raw_success_gain`（以及 v1 兼容字段
`overall_success_gain`）仅反映全部样本口径。

在线阶段正常质量样本少于 30 个时触发 `insufficient_power`，不生成候选
建议；deterministic smoke 和单元测试不受此门禁限制。

`safe_stop_rate` 必须为 1；安全停止不能被当成任务完成，也不会作为
质量失败与容量压力安全停止重复计入；容量压力的完成能力由
`capacity_coverage` 表示，无法完成时由 `safe_stop_rate` 验证是否安全停止。

## 更新规则

评测不会写入运行时配置或源码。只有两个独立 confirm seed 批次得出一致候选，且 recommendation 为 `request_human_approval` 时，维护者才可以更新 `capslock/behavior_defaults.py`。变更应同步默认值断言、README 示例和发布说明，并记录 report/recommendation 哈希。未达到最小收益或任一安全、质量门禁失败时保留当前默认值。

`agents.max_depth` 等安全硬边界只登记、不参与矩阵搜索；修改它们需要独立的资源耗尽、模糊测试和安全评审。
