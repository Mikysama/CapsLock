# External benchmark quarantine

Quarantine is limited to a reproducibly broken gold/oracle run, a damaged pinned image,
or an upstream grader defect. Agent failures are never grounds for quarantine.

Each versioned JSON entry must record the suite and instance ID, upstream revision,
observed failure, reproduction-log SHA-256, date, reviewer, and upstream issue URL. A
suite report always publishes both the official and effective denominator.

Store entries in `<suite>.jsonl`. `reason` must be one of
`gold_or_oracle_unstable`, `image_damaged`, or `upstream_grader_defect`. Set
`entry_hash` to the canonical SHA-256 of the remaining entry fields. `doctor` and
`run` fail closed on stale revisions, unknown tasks, invalid evidence, or agent-based
reasons.
