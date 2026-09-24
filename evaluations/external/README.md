# CapsLock External Eval v1

This suite is independent of `core-v1`: it runs the production CapsLock CLI on
third-party benchmark workspaces and delegates correctness to each benchmark's official
harness. External data, repositories, images, credentials, and run artifacts are never
tracked in this repository.

## Profiles

- `smoke`: five deterministically sampled tasks per suite, one rollout.
- `core`: sixty tasks per suite selected with seed `20260906`, three rollouts.
- `full`: the complete pinned official split, one rollout.

The supported model tracks are `flash` and `pro`. The tracked registry pins upstream
harness commits. `sync` resolves mutable Hugging Face branches to immutable commits and
writes content-addressed locks into the external cache. A scored run refuses to start
without that lock.

## Normalized catalog

Upstream preparation jobs export one JSON object per line. Grader configuration remains
in the orchestrator catalog and is never copied into the agent workspace:

```json
{
  "suite": "setupbench",
  "instance_id": "example",
  "problem_statement": "Configure the project and its service.",
  "workspace_source": "/srv/eval/workspaces/example",
  "repository": "example/project",
  "language": "python",
  "task_type": "reposetup",
  "gold_patch_size": 0,
  "resource_class": "linux-amd64-cpu",
  "grader": {
    "command": ["python", "setupbench/evaluation_harness.py", "{workspace}/metadata.json"],
    "cwd": "harness",
    "success_exit_code": 0,
    "success_substring": "Setup successful",
    "timeout_seconds": 3600
  }
}
```

Catalog import rejects `hint`, `FAIL_TO_PASS`, `PASS_TO_PASS`, `test_patch`,
`gold_patch`, and `fix_patch` whenever they appear in agent-visible task data. Official
grader commands are accepted only when they use the suite-specific harness entry point.

## Commands

Build the exact wheel that will be evaluated, then sync the pinned sources and import
catalogs produced by the upstream preparation jobs:

```bash
python -m build --wheel
python scripts/evaluate_external.py sync \
  --cache /srv/capslock-eval \
  --catalog swebench_verified=/srv/catalogs/swebench-verified.jsonl \
  --catalog setupbench=/srv/catalogs/setupbench.jsonl

DEEPSEEK_API_KEY=... python scripts/evaluate_external.py doctor \
  --cache /srv/capslock-eval

sudo install -d -o "$USER" /opt/capslock
CAPSLOCK_EVAL_ISOLATED=1 \
CAPSLOCK_EVAL_NETWORK_POLICY=task-allowlist \
DEEPSEEK_API_KEY=... python scripts/evaluate_external.py run \
  --cache /srv/capslock-eval \
  --suite swebench_verified \
  --model-track flash \
  --profile smoke \
  --wheel dist/capslock-*.whl \
  --output /srv/capslock-results
```

Resume uses the original run ID and refuses changes to the wheel, tasks, prompt, model,
or limits. Completed result hashes are verified and skipped; incomplete attempts are
discarded and restarted from their clean workspace source.

```bash
python scripts/evaluate_external.py resume ... --run-id external-...
CAPSLOCK_EVAL_ISOLATED=1 python scripts/evaluate_external.py grade \
  --cache /srv/capslock-eval RUN_DIR --catalog CATALOG.jsonl
python scripts/evaluate_external.py report RUN_DIR \
  --catalog CATALOG.jsonl --baseline BASELINE_RUN_DIR
```

By default, patch and environment grading happens during `run`. Use `--defer-grade` to
persist the artifact and workspace with a `not_run` grader status; `grade` then invokes
the pinned official harness, atomically updates results and reports, and cleans the
workspace. Grader infrastructure failures retain the workspace so `grade` can retry.
`report` only rebuilds/compares normalized reports and never invokes a grader.

## Infrastructure policy

- CPU tasks must be scheduled on disposable Linux amd64 self-hosted workers.
- GPU and declared multi-container catalogs must be prepared and scheduled by the
  surrounding Modal/Harbor worker using pinned images. This repository's Python layer
  validates the declared resource class and consumes its isolated workspace; it does
  not itself create cloud resources.
- With `provider-only`, only the model client may reach the configured provider host;
  task shells are offline. With `task-allowlist`, the current Linux/macOS sandbox
  uses its supported unrestricted-network mode for task shells, so dependency
  installation and test downloads can reach the public network. Use this only on a
  disposable worker and keep the run isolated.
- `CAPSLOCK_HOME`, project state, and workspaces are fresh for every task. Memory, MCP,
  plugins, project/user skills, Web, worktrees, and delegation are disabled.
- SetupBench and Terminal-Bench preparation must run the injected-runtime no-op baseline.
  Terminal-Bench oracle validation is five runs; other gold validation is three runs.

The `pro` pricing values in `suites.toml` intentionally start at zero. `doctor` rejects a
scored environment until maintainers update them from the current provider price sheet.
The environment flags are assertions checked by the runner; the disposable worker and
egress proxy are responsible for enforcing the actual filesystem and network boundary.
Set `CAPSLOCK_EVAL_RUNTIME_ROOT` only when `/opt/capslock` is unavailable.

The flash track is pinned to a `128000` token context window and an `8192` token
single-response output limit. The external `max_tokens` setting is the cumulative
rollout budget, not a provider context-window setting. Reports therefore expose both
cumulative provider input usage (`input_tokens`) and peak per-request context usage
(`peak_context_tokens`), plus the number of successful context compactions.

## Result compatibility and SWE-bench bridging

Task-result schema v1 keeps context diagnostics optional for older artifacts. All
result readers verify the hash over the exact stored field set before adding
default zeros and computing a normalized in-memory hash. Read-only report and
comparison commands do not rewrite source artifacts. Resume still requires the
same run identity (including code and wheel hashes); compatibility does not allow
resuming a run with a different runtime. New and deferred results retain all three
context metrics, including through subsequent grading.

The SWE-bench adapter writes a temporary prediction JSONL and scopes `--run-id`
with an idempotent `-repN` suffix. If no run ID is supplied, the evaluation run ID
is used. It removes its sidecar on success, failure or timeout. The legacy
`scripts/swebench_patch_bridge.py` wrapper passes existing JSONL through unchanged;
it only converts raw patches, so old installations can safely keep the wrapper.

`CAPSLOCK_EVAL_NETWORK_POLICY=task-allowlist` deliberately retains `network=["*"]`.
This is unrestricted networking, not a hostname filter. Set it only for isolated
evaluation processes; inherited values also affect ordinary Shell normalization.
Permission checks still apply after normalization.
