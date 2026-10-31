# t_35dc5346 — wake path must carry per-unit max_num_seqs

Base: branch wt/t_35dc5346, rebased onto origin/main (ac8e5520) in run 2.
Run 1 branched from fe17597b (PR #118 head) because main did not have PR #118
yet; PR #118 merged in the meantime (verified `git merge-base --is-ancestor
fe17597b origin/main` => YES), so the rebase is a clean fast-stack and the
acceptance stamp below was taken on the rebased tree — what a PR base=main
will actually merge. Rebase touched nothing of mine: `git diff --stat
fe17597b origin/main` over the four changed files is empty (main's 46 new
commits added changelog fragments, orch-waker/cards-monitor suites and the
systemd-unit-name suite — no overlap with serving.py/template_intent.py).

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

- STATUS (run 2, resumed after run 1 hit the quota wall mid-work): plan items
  1-5 were already implemented and partly committed by run 1. Verified, not
  trusted: `git show 45b748e6` carries the production changes
  (`to_serving_json` stamp + `_unit_run_cmd` emission + `fleet_up_plan` /
  `keepalive_units` pass-through + `_option_overrides`/`_max_num_seqs_from`
  helpers); run 1's UNCOMMITTED work was the test block in
  `hscc_daemon/tests/test_serving_extra.py` (TestFleetUpMaxNumSeqs), the
  sharper bidirectional canary messages in `_serve_cmd_mismatch`, and one
  byte-identity test in hscc-cluster. Committed as eecfb254 so it cannot die
  with this run.
- All three `_unit_run_cmd` consumers are covered by the single builder, which
  is the point of the design: `fleet_up_plan` (serving.py:712), gentle relaunch
  (health.py:2954) and auto-heal (health.py:2628) all call it. Auto-heal reaches
  it via `_autoheal_find_unit` -> `keepalive_units`, so the pass-through added in
  `keepalive_units` (serving.py:371) is what actually gets the cap into the heal
  command — a pass-through only in `fleet_up_plan` would have left 2 of 3 paths
  still at the recipe default. Pinned by `test_keepalive_units_carry_the_cap`
  (a per-unit test alone would not catch a removed pass-through).
- Auto-heal strips `--ensure` from the derived argv (health.py:2629-2630
  `recreate_argv`) but keeps every other token, so `-o max_num_seqs=N` rides into
  the recreate too.
- `_serve_cmd_mismatch` has exactly ONE non-test consumer (serving.py:717, inside
  `fleet_up_plan`), where the flag is a WARN and the ISSUED command stays the
  derived one. So widening the flag list cannot change behaviour beyond the
  warning, and cannot block a wake. Verified by
  `test_plan_still_runs_the_derived_command_on_option_drift`.
- The `-o` comparison is bidirectional and that is deliberate. One-way (record has
  it, derived lacks it) would catch the bug this card is about but would read a
  PRE-fix serving.json (written before this change, no `-o`) against a now
  cap-emitting derived command as agreement — which is exactly the "drift NOT
  applied" fact the apply path already reports. Both directions are asserted.
- Risk checked and cleared: no other `-o key=value` pair is emitted anywhere on
  the apply path (`grep '"-o"'` => only cluster_template.py:461 max_num_seqs and
  serving.py:645). So the widened comparison has no other live key to
  false-positive on today; the general code shape is still right for future
  overrides.
- serving.json has no key-level schema validator anywhere (`grep max_num_seqs
  hscc-roles hscc-bootstrap hscc-api hscc-project` => no hits), and readers use
  `u.get(...)`, so the new optional key is additive: old readers ignore it, and a
  hand-edited bad value degrades to no-cap (serving._max_num_seqs_from) rather
  than emitting a malformed override.
- INTENT-HASH PARITY verified against the real sparkrun source, not the card
  prose: `/Users/desac/sparkrun/src/sparkrun/orchestration/job_metadata.py:234`
  `generate_intent_id` hashes runtime + model + image + port + served-model-name +
  non-default parallelism, and its own docstring states "serve arguments are *not*
  hashed". So `-o max_num_seqs=N` cannot shift the cluster_id / duplicate a
  container. NOTE: my first comment cited `sparkrun.core.job_metadata` — the real
  module is `sparkrun.orchestration.job_metadata`; fixed in serving.py:569.
- Goldens: `hscc-cluster/tests/_v3_nonuniform_golden.json` +
  `_v3_nonuniform_apply_golden.json` pass untouched with the change in place (99
  tests across test_declaration_apply_paths / test_template_declarations /
  test_apply_overreach_guard). That is the omit-if-absent rule doing its job: the
  fixture templates declare no cap, so their serving.json is unchanged bytes.
  Independently pinned by
  `test_no_cap_serving_json_is_the_exact_pre_change_shape` (key list AND order)
  so the claim survives even if someone regenerates the goldens.
- `scripts/changelog_fragments.py check` => OK, 24 entries (was 23; the new
  fragment is `kind: Fixed`, task t_35dc5346).
- `scripts/address_guard.py --tracked` => rc=0, clean.
- Full-suite result: see the completion metadata (recorded from the actual
  `bash scripts/run_tests.sh` output, not from a per-file run).
