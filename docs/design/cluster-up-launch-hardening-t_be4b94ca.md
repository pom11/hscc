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

## Decision 4 — sparkrun-side root cause (fix 4) — RESOLVED by source read

**Verdict: the hang is sparkrun's readiness wait, NOT a missing stdin EOF.**
Attribution is against the tree the live `sparkrun` actually runs from
(v0.4.1, editable install `/Users/desac/dev/sparkrun-041-verify@1aaa6479` —
the `.pth` under `.venv-sparkrun-041` points there; the `~/dev/sparkrun`
checkout is a *newer* tree whose CLI already gates the wait behind
`--no-ready-wait`).

Chain, with file:line in the v0.4.1 tree:
- `src/sparkrun/cli/_run.py:157` — the `--ensure` help says it outright:
  *"Reuse an existing job when present; **still wait for readiness**"*.
- `src/sparkrun/cli/_run.py:160` — the flag that skips the wait is
  **`--no-ready-wait`**, which hscc does not pass; `--no-follow` only
  controls the log stream (`show_logs=not no_follow`, `_run_lifecycle.py:21`).
- `src/sparkrun/cli/_run_lifecycle.py:24-35` — `wait_for_run` echoes
  `Waiting for server readiness...` then BLOCKS on `watcher.done` — it
  returns only when the readiness observer has a verdict.
- The observer (`launcher.wait_for_serve_ready` ->
  `orchestration/startup.run_probe`) needs a first non-empty inference
  **token**, not an HTTP 200 on `/v1/models` — so a unit whose model loads
  far past the port-bind keeps the local call open for ~port+health+
  inference deadline (~3950s observed) while `/v1/models` already answered
  200 within minutes (the operator's exact observation).
- Operator evidence matching the chain (redacted): both hand-run logs end
  `Waiting for server readiness... Inference readiness observation failed:
  startup observation exceeded its deadline` / `ERROR: endpoint did not
  become ready ...` / `Launch timings (total 3947.3s)` — 65.8 min; the
  19-min first observation was the same wait cut short by the operator's
  kill.

The `ssh ... bash -s` idle-with-no-child the operator saw is that wait's
*own* ssh usage (every probe/poll goes over `run_remote_script`), and the
missing-EOF hypothesis is **ruled out with source evidence**: every
`bash -s` site in sparkrun provably closes its local stdin —
`orchestration/ssh.py` `_run_subprocess` passes `input=` (subprocess.run
closes stdin after writing; `run_remote_script` :520,
`run_remote_script_streaming` :596, `run_remote_scripts_parallel` routes
through both), `core/monitoring.py` `_start_host` writes the script then
`proc.stdin.close()` (:421), `orchestration/startup.run_probe` uses
`communicate(pending)`, and the `networking.py`/`sudo.py` local paths all
pass `input=`. An idle `bash -s` with no child is what the poll loop looks
like BETWEEN polls; it is not an unreaped payload.

Filing upstream: `pom11/sparkrun` has GitHub issues DISABLED
(`gh issue list` refuses), and the live tree's flag design (`--ensure` +
`--no-ready-wait`) shows the wait-on-ensure behavior is *intended* — the
defect from hscc's point of view is the composition hscc chose
(`--no-follow --ensure` without `--no-ready-wait`), which this card's fixes
1-3 bound from the hscc side instead. An `--ensure` that skips the wait (or
honors `--no-follow` for the wait too) is the upstream ask, recorded here
for the operator to file from an issues-enabled repo. hscc deliberately
does NOT pass `--no-ready-wait`: the launch-phase timeout bounds the whole
call, which also covers a genuinely wedged transport, while the daemon's
existing workers check owns post-launch readiness.

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

## Decision 6 — heal.py: the pre-serve skip does NOT apply (checked, no change)

`hscc-cluster/heal.py` `restart_model(..., ensure=True)` issues the same
`sparkrun run --no-follow --ensure` form via `cl.run_cmd(run, timeout=900)`.
The already-serving skip must NOT be copied there: heal only fires on a unit
its caller already judged unhealthy, and the daemon deliberately STRIPS
`--ensure` on the wedged-container path (health.py: "``--ensure`` no-ops on
a container that is Up-but-wedged") — skipping a unit because
`/v1/models` answers 200 would re-open exactly the wedge hole that stripping
closes. The 900s bound is the right scale there for now: it is a single-unit
repair, already bounded (unlike the serial fleet loop this card fixes), and
a readiness-wait expiry it produces is evidence for the heal verdict, not a
fleet starvation. If the operator later adds `--no-ready-wait` upstream,
heal.py should adopt it — recorded, out of scope here.

## File map

- hscc-cluster/hscc.py            — fixes 1-3 (launch timeout, skip, parallel)
- hscc_daemon/health.py           — fix 5 (per-unit alias detection + check_proxy wiring)
- hscc_daemon/declaration_agreement.py — fifth leg (doctor surface)
- hscc-cluster/tests/test_cluster_up_launch.py  — NEW: fixes 1-3 (TDD first)
- hscc_daemon/tests/test_unit_alias_coverage.py — NEW: fix 5 (TDD first)
- changelog.d/t_be4b94ca.md       — one fragment
