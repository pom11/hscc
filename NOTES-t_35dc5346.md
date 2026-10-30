# t_35dc5346 — wake path must carry per-unit max_num_seqs

Base: branch wt/t_35dc5346 @ fe17597b (PR #118 head — ModelIntent/ResolvedUnit/
_render_serve_cmd leg already shipped there). NOT rebased on origin/main because
main does not have PR #118 yet (verified: `git merge-base --is-ancestor fe17597b
origin/main` => NO).

## Gap (confirmed by reading the code)

1. `hscc-cluster/template_intent.py:to_serving_json` (~l.729) builds each worker
   unit dict with id/role/keepalive/model/recipe/nodes/port/tp/pp/family and
   the orchestrator with id/role/model/recipe/nodes/port. It never emits
   `max_num_seqs`, so the cap is LOST the moment apply writes serving.json.
   `gpu_memory_utilization` is likewise absent today (known, out of scope).
2. `hscc_daemon/serving.py:_unit_run_cmd` (l.517) builds the wake argv from
   recipe/nodes/port/model/role/tp only -> no `-o max_num_seqs=N`. Consumers:
   `fleet_up_plan` (l.616), `health._default_autoheal_worker` (health.py:2628),
   `health` gentle relaunch (health.py:2954). All three restart at the RECIPE
   default (flash-next ships 8) -> breaks the <=5-running-requests/node rule.
3. `_serve_cmd_mismatch` (l.473) compares recipe/--hosts/--port plus the
   intent-hash flags `--served-model-name` and `--tp` only. A recorded serve_cmd
   carrying `-o max_num_seqs=5` against a derived cmd lacking it is SILENT.

## Plan

1. `to_serving_json`: worker units carry `max_num_seqs` when not None
   (omit-if-absent, same rule as `uniform_pool`); orchestrator too.
2. `serving._unit_run_cmd`: truthy int `max_num_seqs` on the unit entry ->
   append `-o max_num_seqs=N`.
   `fleet_up_plan` + `keepalive_units` must pass the field through into their
   per-entry dicts (they build entries, not raw units).
3. `_serve_cmd_mismatch`: extend so a recorded `-o <key>=<v>` the derived cmd
   lacks (or differs on) is LOUD, mirroring `cluster_template._serve_cmd_flags`
   normalization.
4. Tests (fakes only) in hscc_daemon/tests + a serving.json emission test in
   hscc-cluster/tests; pin golden byte-identity for non-cap serving.json.
5. changelog.d/t_35dc5346.md fragment.
6. `bash scripts/run_tests.sh` ALL GREEN + address_guard --tracked clean.

## Findings (append as I go)

- (pending)
