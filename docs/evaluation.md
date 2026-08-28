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

首次 confirm 的建议固定为 `requires_second_confirmation`。使用不同 `--seed` 再运行一次，并通过 `--peer-report` 指向第一份 confirm 报告；只有两次选择相同候选时，第二份报告才会输出 `request_human_approval`。

Provider 名称用于解析 `<PROVIDER>_API_KEY` 和可选的 `<PROVIDER>_BASE_URL`。Live 阶段不回退到 `CAPSLOCK_API_KEY`，避免意外使用生产凭据。

`refine` 根据 OAT 报告为每个参数保留两个最优可行档位，再执行子系统内组合。`memory-optimize` 只对入围 Memory 配置运行带固定 seed 的高斯过程 UCB 权重搜索；后续报告会作为观测输入改善下一轮提案。

每次运行输出：

- `samples.jsonl`：逐任务原始样本、停止原因、token、成本和安全指标。
- `report.json`：环境清单、置信区间、硬门禁、Pareto 集合与报告哈希。
- `recommendation.json`：旧值、新值、收益、风险、回滚值和人工审批动作。
- `pareto.svg`：成本与 p95 延迟图。
- `reproduce.txt`：复现命令。

## 更新规则

评测不会写入运行时配置或源码。只有两个独立 confirm seed 批次得出一致候选，且 recommendation 为 `request_human_approval` 时，维护者才可以更新 `capslock/behavior_defaults.py`。变更应同步默认值断言、README 示例和发布说明，并记录 report/recommendation 哈希。未达到最小收益或任一安全、质量门禁失败时保留当前默认值。

`agents.max_depth` 等安全硬边界只登记、不参与矩阵搜索；修改它们需要独立的资源耗尽、模糊测试和安全评审。
