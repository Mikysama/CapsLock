# CapsLock policy evaluation audit — 2026-08-28

## Decision

Keep every current behavioural default. No candidate is approved for a default
update from this run.

This is an evidence decision, not a claim that every current value is optimal.
The deterministic benchmark exposed useful capacity differences, but it cannot
close the production-selection loop because:

- no model/provider credentials were available for `screen` or either `confirm`
  batch;
- pricing in `core-v1.toml` is zero, so resource increases have no measured cost;
- no loop candidate meets the required recall and false-positive gates;
- no Memory candidate meets the required precision and recall gates;
- deterministic Agent tasks do not exercise merge/write-conflict outcomes.

The automatic deterministic recommendation must therefore not be used to edit
`capslock/behavior_defaults.py`.

## Runs

| Run | Candidates | Tasks | Samples | Report hash |
| --- | ---: | ---: | ---: | --- |
| OAT | 53 | 220 | 11,660 | `077948498cba19b7280032912640d4a6b69eca1649756a0553b9d942d0ee08af` |
| Subsystem refine | 85 | 220 | 18,700 | `bd14075e95088fb0e97ee7fd1f17a7bf60aa2579a63f46e0302dfb45e12a8ec1` |
| Memory weight search | 51 | 220 | 11,220 | `5f3d023486e7f3214fba04a82dbed4e48df9d3c33e75fbec889c18d98f783f72` |

Total deterministic observations: 41,580. The evaluation, runtime, and Memory
test selection also passed: 46 tests passed.

## Material results

| Subsystem | Baseline task success | Best observed capacity result | Gate/audit result |
| --- | ---: | --- | --- |
| Runtime | 37.5% | 100% at 64 rounds, 120 s timeout, concurrency 16, repairs 2 | Not selected: zero pricing and no live provider mean the resource/latency trade-off is unmeasured. |
| Context | 100% | All retained OAT levels tied at 100% | Keep baseline: no qualifying improvement. |
| Loop detection | 45.0% task success | No candidate jointly separates loops from legal repetition | No feasible candidate. The best observed recall was 80%, with an 80% false-positive rate. |
| Memory | 36.7% task success | 86.67% precision and 61.90% recall for the best combined threshold/budget candidate | No feasible candidate. CI lower bounds were 83.71% precision and 58.57% recall, below 98% and 90%. |
| Child Agents | 30.0% | 100% at children 8, concurrency 4, child rounds 24 | Not selected: this only demonstrates capacity and does not test conflict-free merge or duplicate side effects. |

## Selected values

All values remain at the current baseline:

| Parameter | Selected value |
| --- | ---: |
| `runtime.max_tool_rounds` | 32 |
| `providers.timeout_seconds` | 60 |
| `tools.max_read_concurrency` | 4 |
| `tools.max_argument_repair_attempts` | 1 |
| `context.trigger_ratio` | 0.80 |
| `context.target_ratio` | 0.60 |
| `context.preserve_recent_turns` | 6 |
| `context.preserve_recent_tokens` | 32,768 |
| `context.max_compaction_failures` | 3 |
| `loop_detection.consecutive_repeats` | 3 |
| `loop_detection.failed_retries` | 3 |
| `loop_detection.cycle_repetitions` | 3 |
| `loop_detection.max_cycle_length` | 4 |
| `memory.recall_limit` | 5 |
| `memory.recall_bytes` | 4,096 |
| `memory.semantic_threshold` | 0.45 |
| `memory.recall_threshold` | 0.50 |
| `agents.max_children` | 4 |
| `agents.max_concurrency` | 2 |
| `agents.max_child_tool_rounds` | 16 |

Memory scoring weights also remain unchanged: retrieval 0.72, scope 0.08,
confidence 0.08, freshness 0.06, and source validity 0.06.

`agents.max_depth=1` remains an architecture safety constraint and was not
tuned.

## Required evidence before changing defaults

1. Repair the loop corpus so legitimate progress and identical non-progressing
   calls are represented separately, then require the deterministic hard gates.
2. Add Memory examples that permit the declared precision/recall target and
   validate them independently.
3. Add executable child-Agent conflict, merge, and duplicate-side-effect cases.
4. Set the actual input/output prices for the chosen screen and target models.
5. Run `screen` twice per tuning task, then two independent three-repetition
   `confirm` seed batches. Only matching feasible recommendations may proceed to
   human approval.
