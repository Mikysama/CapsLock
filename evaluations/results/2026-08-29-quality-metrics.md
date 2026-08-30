# CapsLock policy evaluation — quality metrics — 2026-08-29

## Decision

Keep the current production defaults. The reports below are deterministic
synthetic smoke results and are not sufficient for a production update. The
best candidate is retained for the next live screen batch only.

## Metric definition

In current reports, `success_rate` is the primary normal-workload rate: successful
samples divided by samples with `in_budget=true` and `capacity_case=false`.
`raw_success_rate` is retained as an audit-only rate over all samples, while
`quality_success_rate` is a compatibility alias for `success_rate`.
Capacity pressure tasks are reported separately as `capacity_coverage`; when a
candidate cannot cover such a task, the expected bounded termination is counted
as `safe_stop_rate`, not as a quality failure. Every run in this report has a
safe-stop rate of 1.0.

## Runs

| Run | Candidates | Samples | Report hash |
| --- | ---: | ---: | --- |
| OAT | 53 | 11,660 | `668d724ef248ce9e129e312e57a40233aa4ca2a8e62f6bc5f2f77fcdc0af8d6a` |
| Refine | 85 | 18,700 | `12f2dbb087b442de1a61d333d3656e0122477212cb634bd5d2c9e15b3ddb5a07` |
| Memory weight search | 57 | 12,540 | `bfc3762817540d58f7431066ed227240a0cff4749ab1a6751c09e76433e174e3` |
| Live screen health probe | 3 | 1,320 | `b1a8305edac25aae74ce2f7ad469ada619c244dc4b6c1af029888d575cc2cdbb` |
| Live candidate-runtime channel check | 3 | 15 | `d4469a29cfbe182b54ad02d81a8feba9cd99a791a20e56af6106673e5022ad17` |
| Repriced deterministic OAT | 53 | 11,660 | `4f9dfe916940f52f6302233218b4d7cdb81a7ba57fbde1c2e9a0ee8970505c40` |
| Current candidate-runtime channel check | 3 | 15 | `98d40f76b0981fa391283ab90a7ed16e8d01ca2fb8403fa742b2d03aa2221003` |
| Real Runtime screen (30 tasks x 2) | 3 | 180 | `fdd048f848b6714f0d6c1276a32688e29457134ac3d3943f912048bb6c239fa7` |

The OAT, Refine, and Memory weight-search runs use
`execution_mode=synthetic_smoke`.

The repriced deterministic OAT uses `deepseek-v4-flash` peak cache-miss
pricing, input `$0.44/M` and output `$1.32/M`, from the provider pricing page:
<https://api-docs.deepseek.com/quick_start/pricing>. Pricing is included in
the matrix fingerprint so cost results remain reproducible.

The live screen health probe used `execution_mode=synthetic_plus_model_probe`
with the configured `capslock` Provider and `deepseek-v4-flash`. Provider
errors were zero after disabling model thinking for the exact-token probe, but
the run intentionally produced no recommendation because candidates were not
injected into a real Runtime.

The baseline quality breakdown is Runtime 100% (15/15), Context 100% (40/40),
Loop 75% (30/40), Memory 36.67% (22/60), and Agents 100% (12/12). The lower
raw overall rate is therefore caused by capacity-pressure tasks and by Memory
and Loop quality failures, not by normal Runtime or Agent tasks.

## Baseline and candidates

| Candidate | Quality success | Raw success | Capacity coverage | Safe stop |
| --- | ---: | ---: | ---: | ---: |
| Current baseline | 71.26% | 54.09% | 0.00% | 100% |
| OAT `loop.max_cycle_length=6` | 74.85% | 56.82% | 0.00% | 100% |
| Refine/Memory `recall_limit=8, recall_bytes=8192, semantic=0.35, recall=0.4` | 82.04% | 62.27% | 0.00% | 100% |
| Runtime `max_tool_rounds=48` | 71.26% | higher raw rate | 13.21% | 100% |

The Memory candidate is the provisional live-screen candidate because it has
the largest quality improvement in the corrected metric. Its deterministic
Memory precision is 86.67% and recall is 61.90%, below the production gates of
98% and 90%, so it must not update `behavior_defaults.py`.

The Runtime candidate increases capacity coverage without improving normal
quality. It should be evaluated with real provider pricing and p95/p99 latency
before any resource increase. Loop candidates require model/runtime validation
of progress-aware detection.

The no-credential screen control run produced 1,320 samples with
`provider_error_rate=100%`, no recommendation, and both `provider_health` and
`candidate_policy_injection` gates failing. This is an expected control result,
not evidence about model quality.

The fixed live screen produced 681 successful samples out of 1,320. By
candidate, quality success was 66.77% for baseline, 70.36% for
`loop_detection.max_cycle_length=6`, and 66.77% for
`tools.max_read_concurrency=16`. These numbers are model-health evidence only;
the `candidate_policy_injection` gate remains mandatory for parameter claims.

The candidate-runtime channel check used the real `WorkspaceApplication` and
`AgentSession` for one task from each subsystem and three candidates. Provider
errors and candidate-injection failures were both zero; every candidate had
40% quality success across the five representative tasks. The run correctly
reported `insufficient_power` and did not recommend a value. This validates the
injection and event-collection path, but the small corpus and representative
prompts are not suitable for parameter selection.

The current candidate-runtime channel check reran the path after fixing
authoritative terminal handling. It used five normal tasks (one per subsystem),
three candidates, and one repetition. Provider errors and security counters were
zero. Normal-workload success was 60% for the baseline and loop candidate, and
80% for the provisional Memory candidate; with only five quality samples each,
all candidates correctly failed the `insufficient_power` gate. This is channel
evidence, not a parameter recommendation.

The probe fix also clears a synthetic `stop_reason` when a candidate-aware real
Runtime reports a successful terminal state, preventing a sample from being
simultaneously marked successful and `memory_not_recalled`.

The 30-task real Runtime screen reached the 30-sample power minimum per
candidate, with provider errors and all six security counters at zero. Results:

| Candidate | Normal success | p95 latency | Cost/task | Gate result |
| --- | ---: | ---: | ---: | --- |
| baseline | 58.33% (35/60) | 33.52s | $0.00375 | context, memory, loop gates failed |
| `loop_detection.max_cycle_length=6` | 58.33% (35/60) | 39.18s | $0.00359 | non-inferiority, memory, loop gates failed |
| provisional Memory | 56.67% (34/60) | 36.08s | $0.00404 | non-inferiority, memory, loop gates failed |

The live screen therefore selects no candidate. The loop value improved live
loop recall to 66.67% versus 33.33% at baseline, but its overall paired CI was
`[-0.15, 0.15]`, latency increased, and the loop false-positive upper CI was
above the 0.01 gate. The provisional Memory value cost more and reduced normal
success; its live recall (66.67%) still missed the 0.90 lower-CI gate.

A single real Runtime smoke task also completed successfully through the same
adapter, recording 3 tool calls, 4 context updates, and 3 approval events.
This confirms the formal runtime lifecycle and event extraction, but does not
replace the required full-power screen and two independent confirm batches.

## Next evidence required

1. Run the full tune corpus with a real low-cost model and two repetitions per
   task; the 30-task screen above is power-valid but not corpus-complete.
2. Run two independent confirm batches with different seeds and three
   repetitions each.
3. Reject any candidate with a security, isolation, duplicate-side-effect or
   safe-stop gate failure, regardless of quality success.
4. Update production defaults only after both confirm reports choose the same
   feasible candidate and request human approval.
