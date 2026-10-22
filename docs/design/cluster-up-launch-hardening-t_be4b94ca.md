# `hscc cluster up` launch hardening + per-unit alias detection (t_be4b94ca)

Design record for the card's four fixes plus the operator addendum (per-unit
alias detection), written BEFORE the code so the next agent can resume cold.

## Decision 1 — per-unit launch timeout (fix 1)

`cmd_cluster_up` currently routes every unit through `_run_or_print(cmd,
dry_run)` with no timeout, which falls back to `_fleet_op_timeout_seconds()`
(VLLM_LOAD_GRACE_MINUTES*60 = 1200s). That window is sized for a model LOAD,
but the launch command is `sparkrun run ... --no-follow --ensure`, which must
RETURN in seconds-to-minutes (container create + port bind); the model load
happens after it returns, watched by the daemon.

New: `_launch_phase_timeout_seconds()` in `hscc-cluster/hscc.py`, env
`HSCC_CLUSTER_UP_LAUNCH_TIMEOUT` default **180s**. Rationale: the operator's
hand-run parallel launches returned in ~3 min worst case, so 180s is above
every honest launch and 6.7x below the 1200s window that made the hang look
forever. The timeout applies ONLY to the per-unit launch loop, NOT to
`cmd_cluster_down` / other `_run_or_print` callers (their semantics unchanged).
On expiry `run_cmd` returns code 124 ("timeout"); the unit is reported failed
with the output tail and the loop continues (fix 3 makes the other units
independent anyway).

## Decision 2 — already-serving skip (fix 2)

Before invoking sparkrun for a unit, probe `http://<node>:<port>/v1/models`
for EVERY node in the unit's span (a TP unit only serves once ALL its hosts
joined the group — a lone responder mid-group-formation must NOT be treated
as serving; skip only if every span node answers 200 with a parseable body).
On skip: no sparkrun call; the issued entry carries `{"skipped": "already
serving", "served": [ids...]}`.
Probe seam: module-level `models_probe_fn = health._proxy_models_probe` in
`hscc-cluster/hscc.py` (tests monkeypatch the name, the same pattern as the
A2 guard's `_launch_guard_status_fn`). Fail-open: any probe error = not
serving = launch anyway (the probe is a fast-path, never an authority).
`--ensure` semantics keep this safe even on a false "serving" reading.

## Decision 3 — parallel launches (fix 3)

`ThreadPoolExecutor(max_workers=min(8, len(to_launch)))`, submitted in plan
order, results collected in plan order — `res["issued"]` stays 1:1 aligned
with `plan` (index i of issued belongs to plan[i]; asserted by test). Each
worker thread runs the existing per-unit guard (status is prefetched once on
the main thread and passed in — unchanged single-fetch property) then the
bounded launch. dry_run path stays serial and prints in plan order (thread
interleaving would garble the printed plan; nothing is executed there).
Failure of one unit never cancels another; a raising worker is converted to
a failed result rather than propagating.

## Decision 4 — sparkrun-side root cause (fix 4)

Read `sparkrun/engine.py` launch path for the `ssh ... bash -s` invocation.
Attribution ONLY — no edits, no workarounds. If the missing-EOF is inside
sparkrun: report on the card (filing an issue on the operator-owned private
repo from a worker is not possible from this session — the operator files
from the finding recorded here).

## Decision 5 — per-unit alias detection (operator addendum)

The proxy-level alias check (`_proxy_expected_aliases` + `_proxy_models_
healthy`) reads the MERGED catalogue at :4000, so a uniform_pool unit serving
base-name-only is invisible while all traffic funnels to the one aliased node.
New in `hscc_daemon/health.py` (placed right after `_proxy_models_probe`):

- `unit_expected_aliases(serving_data, unit)` — uniform_pool: {both role
  aliases} (every unit must serve either role); non-uniform: {_role_alias(
  unit.role)}.
- `check_unit_serving_aliases(serving_data=None, probe=None, timeout=5)` —
  every unit's OWN `http://<node>:<port>/v1/models`; a unit that answers
  parseably but LACKS an expected alias -> problem (WARN log naming unit +
  missing aliases); a unit that cannot be reached -> "unverified", never
  counted as ok (fail-closed doctrine). Returns {"ok", "findings",
  "unverified", "message"}.
- `check_proxy` records the finding in the proxy state and logs
  `alias_coverage: ...` WARN but does NOT flip proxy health: the proxy IS
  healthy in that incident shape; the unit is not. Flipping proxy health
  would invite the wrong remedy (a proxy restart fixes nothing).
- `declaration_agreement` gains FIFTH leg `unit_alias_coverage` (name
  `declaration_agreement.unit_alias_coverage`) aggregating the same function
  with a remediation naming `hscc cluster up` relaunch with the quoted alias
  list. `run_declaration_agreement("all")` includes it; the doctor adapter
  maps legs generically, so `hscc project doctor` surfaces it. The existing
  four-legs test pins leg names for `leg="all"` explicitly — updated to five.

## Evidence-caveat handling (operator note)

`cluster-up-2.log` is 0 bytes — treated as "absent, not contradicting"; the
attribution for fix 4 comes from source reading + (if the live fleet is
reachable and already-serving) a bounded live probe, never from the lost log.
`up-247.log`/`up-248.log` read for timing/sequence only; every repo-bound
artifact uses the sanctioned 10.0.0.x placeholder range and 192.0.2.x for
test fixtures (matching the existing test convention); `address_guard
--tracked` runs before every commit.

## File map

- hscc-cluster/hscc.py            — fixes 1-3 (launch timeout, skip, parallel)
- hscc_daemon/health.py           — fix 5 (per-unit alias detection + check_proxy wiring)
- hscc_daemon/declaration_agreement.py — fifth leg (doctor surface)
- hscc-cluster/tests/test_cluster_up_launch.py  — NEW: fixes 1-3 (TDD first)
- hscc_daemon/tests/test_unit_alias_coverage.py — NEW: fix 5 (TDD first)
- changelog.d/t_be4b94ca.md       — one fragment
