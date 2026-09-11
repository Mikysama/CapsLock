# Long-context compaction evaluation

The `context-compaction` strategy consumes an external, anonymized JSON dataset:

```bash
python scripts/evaluate_policies.py \
  --stage deterministic \
  --strategy context-compaction \
  --matrix evaluations/core-v1.toml \
  --context-dataset /path/to/anonymized-context-sessions.json \
  --output /path/to/results
```

The dataset is intentionally not committed. It must contain at least four
independent sessions, 60 fact questions, and 30 continuation tasks for each
selected split. Every case declares `split` (`tune` or `confirm`), `prompt`,
`expected`, and `position` (`front`, `middle`, or `tail`). Sessions contain a
`messages` array with `user`, `assistant`, or `tool` roles. Tool messages are
stored as labelled assistant transcript entries while seeding the isolated live
probe; active-run Tool Result behavior is covered separately by runtime tests.

Use distinct source sessions for tune and confirm. Remove credentials, personal
data, proprietary source text, and live artifact contents before evaluation.
Run two independent screen passes before confirmation. Confirmation requires two
different seeds, three repetitions per seed, and the second report passed through
`--peer-report`; no production default is changed by this evaluation strategy.

Minimal shape:

```json
{
  "sessions": [
    {
      "id": "anonymous-01",
      "messages": [{"role": "user", "content": "..."}],
      "fact_questions": [
        {
          "split": "tune",
          "prompt": "Return the recorded build SHA only.",
          "expected": "0123abcd",
          "position": "middle"
        }
      ],
      "continuation_tasks": []
    }
  ]
}
```
