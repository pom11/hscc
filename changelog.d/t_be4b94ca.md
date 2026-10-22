kind: Fixed
task: t_be4b94ca

- **`hscc cluster up` hung "forever" on an already-serving unit and starved
  the rest of the fleet.** The 2026-10-22 cooldown repro: a unit that already
  answered `/v1/models` 200 kept its `sparkrun run --no-follow --ensure` call
  open for 19 min (then 8 min on retry) while the SERIAL plan loop waited, so
  the other two units never launched. Three fixes in `cmd_cluster_up`:
  (1) each launch is bounded by a dedicated LAUNCH-PHASE timeout —
  `HSCC_CLUSTER_UP_LAUNCH_TIMEOUT`, default **180s**, deliberately NOT the
  1200s model-load grace — and an expiry is reported as that unit failing
  with the captured output tail, not swallowed; (2) a unit whose every span
  node already answers `/v1/models` with a parseable NON-EMPTY catalogue is
  reported already-up and sparkrun is never invoked (fail-open probe: any
  error = launch anyway, `--ensure` makes that safe); (3) launches run
  CONCURRENTLY over the independent hosts (ThreadPoolExecutor) while the
  per-unit report stays plan-ordered, and the A2 guard keeps its single
  prefetched status fetch. Sparkrun-side attribution (fix 4, source-read
  only, no hscc workaround): the hang is sparkrun's OWN readiness wait — in
  v0.4.1 `--ensure` "still wait[s] for readiness" and `--no-follow` only
  stops the log stream; `wait_for_run` blocks until the startup probe sees a
  first non-empty inference TOKEN, which a model that loads far past the
  port-bind can take ~66 min to deliver while `/v1/models` already answers
  200 — the operator's own hand-run log shows exactly that shape
  (`Waiting for server readiness... startup observation exceeded its
  deadline ... total 3947.3s`). The local `ssh ... bash -s` observation is
  that wait's per-poll SSH probe, not a missing-EOF bug: every `bash -s`
  site in sparkrun provably closes its stdin (`input=` or `communicate`).
  `--no-ready-wait` is the sparkrun-side flag that skips the wait; hscc
  deliberately does NOT pass it (the hscc launch timeout bounds the whole
  call instead, which also covers a genuinely wedged ssh).
- **`hscc_daemon.serving.resolve_cluster_config()`'s fallback could exec the
  operator's live sparkrun CLI — and on this host it DID, three times per
  daemon suite run.** The call was
  `subprocess.run("timeout 2 sparkrun cluster list --json", shell=True, ...)`:
  a shell string cannot be argv-rewritten by `sparkrun_bin.exec_argv`, so the
  `HSCC_SPARKRUN_BIN` hermetic pin never covered it (the daemon conftest's own
  docstring recorded exactly this gap). Now a LIST argv built by
  `sparkrun_bin.argv` — the repo's single execution chokepoint — with
  `shell=True` dropped and `timeout=3` replacing the PATH-dependent
  `timeout 2` wrapper (macOS keeps `timeout` in Homebrew). Pinned by
  `hscc_daemon/tests/test_serving_fallback_no_live_cli.py` against a fake CLI
  on a PATH-less HOME.

---
kind: Security
task: t_be4b94ca

- **sparkrun argv FENCE at the conftest level (stop-work order; live `:4000`
  proxy SIGTERM incident).** One shared harness,
  `hscc-cluster/tests/sparkrun_fence.py`, wired into the four suites that can
  reach the CLI (`hscc-cluster`, `hscc-commands`, `hscc_daemon`,
  `sparkrun-hermes`): it wraps every subprocess entrypoint, resolves each
  argv element against PATH, and BLOCKS + FAILS the test whose call resolves
  to the operator's real sparkrun CLI (bare `sparkrun`, an absolute CLI path
  outside a pytest per-test tmp dir, a CLI at any LATER argv element, or the
  CLI name embedded in a `shell=True` string). Recipe paths named
  `sparkrun.yaml` are pinned as NOT CLI tokens (false-positive guard), and the
  sanctioned escape hatches are a `tmp_path` fake or an explicit
  expect-block — the fence reports blocked-vs-unexpected in the terminal
  summary every run. First run of the fence caught **six** real leak sites
  that had been silently exec'ing the operator's CLI in CI-shaped runs:
  `_pin_topology`'s `monkeypatch.setattr` reading lazy `clusterlib.HEAD/NODES/
  NAS_HOST` (→ live `sparkrun cluster list --json` for EVERY cluster test),
  a live discovery-cache WRITE from `test_discovery`, real `sparkrun show`
  per shipped recipe in `test_template_intent` (now opt-in gated), and the
  three `resolve_cluster_config` fallback shells above. Conftests now pin
  canned discovery output instead; no normal test run touches the fleet.

---
kind: Added
task: t_be4b94ca

- **Per-unit alias coverage — the blind spot the 15:10 hand-launch exposed.**
  Two units came up with `--served-model-name` = base name only (an unquoted
  copy of the `--dry-run` line) and the daemon stayed GREEN for ~2 h: the
  existing alias check is PROXY-level, so the merged catalogue at `:4000`
  still contained `worker-model`/`orchestrator-model` (served by the one
  correctly-launched unit) while every aliased request funneled to it alone.
  New in `hscc_daemon/health.py`: `unit_expected_aliases` +
  `check_unit_serving_aliases` — probe each unit's OWN
  `http://<node>:<port>/v1/models` (uniform_pool: BOTH role aliases per
  unit; otherwise the unit's own role alias) — surfaced by `check_proxy` as
  a named WARN + `unit_alias_warn` in the proxy state WITHOUT flipping proxy
  health (the proxy IS healthy in that shape; a restart fixes nothing), and
  as FIFTH declaration-agreement leg `unit_alias_coverage` so
  `hscc project doctor` / `hscc daemon verify` report the same finding
  fail-closed (unreachable unit = unverified, never ok).

---
kind: Verified
order: 1

- `hscc-cluster/tests/test_cluster_up_launch.py`: barrier-proved concurrency,
  skip/refuse/timeout matrix, dry-run unchanged (fixes 1-3).
  `hscc_daemon/tests/test_unit_alias_coverage.py`: the base-name-only unit
  fixture names the unit AND the missing aliases through health AND the
  fifth leg (the addendum's "proxy-level check alone does not count").
  `hscc_daemon/tests/test_declaration_agreement.py` +
  `hscc-project/tests/test_doctor_declaration.py` updated for the fifth leg
  (the doctor default-seam test now pins `serving.SERVING_JSON` so the new
  leg cannot read — or probe — the operator's live serving.json).
  Canonical runner `scripts/run_tests.sh` green on the branch tip.
- Fence self-tests: `hscc-cluster/tests/test_sparkrun_fence.py` — the live
  CLI as argv[0], as a later argv element, inside a shell string, as an
  absolute path outside a pytest tmp dir, and as a recipe-name false-positive
  — plus blocked-vs-expected accounting. `hscc-cluster` 666 passed + 1
  skipped (the gated recipe-fit test), `hscc_daemon` 2217 passed,
  `hscc-commands` 69 passed with the fence line visible in the terminal
  summary, `sparkrun-hermes` 12 passed. address_guard --tracked rc=0.
