kind: schema
task: t_fd56b3ee

- **Templates can cap per-unit vLLM concurrency: per-model `max_num_seqs`.**
  `ModelIntent` gains an optional integer `max_num_seqs: <int >= 1>`, carried
  end-to-end exactly the way `gpu_memory_utilization` is (absent = the
  recipe's own default wins; validated at parse time so `hscc template
  validate` rejects a bad value — non-int, bool, or < 1 — offline instead of
  mid-provision). A resolved unit's serve command emits
  `sparkrun run ... -o max_num_seqs=N` (sparkrun's repeatable `-o key=value`
  override — there is no dedicated flag; verified against the live
  `sparkrun run --help`). Drift comparison handles a changed value the same
  way it handles a changed `--gpu-mem`: apply reports
  "serve command drift NOT applied ... -o max_num_seqs: '8' -> '5'" and
  leaves the running unit untouched until `--force-recreate`. Follows the
  PR #106 precedent for a model-level key (no schema-version bump — v4
  already exists for the declaration blocks). The shipped
  `4node/flash-next-all` now caps BOTH models at 5, honouring the operator
  rule of at most 5 running requests per node against the recipe's default
  of 8.
