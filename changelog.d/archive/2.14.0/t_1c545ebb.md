kind: Fixed
task: t_1c545ebb

- **`hscc verify` multiplex false-failed on two leftover directories, and
  `gateway.json` contradicted it with a different count.** Both multiplex
  scanners enumerated "every directory under `~/.hermes/profiles`", but the
  gateway builds `served_profiles` from Hermes' own enumeration
  (`hermes_cli.profiles._iter_named_profile_dirs`), which is narrower — so the
  check demanded the gateway serve profiles Hermes refuses to recognise, a
  requirement that can never be met. `profiles/.deleted/` (Hermes' tombstone
  dir, holding one 8-byte marker per deleted profile) was reported as a profile
  id named `.deleted`, and `profiles/ecofire-bc/` — a stub carrying only
  `workspaces/` and none of the six identity markers — as an unserved profile.
  The two surfaces also disagreed *with each other*: the daemon's scanner
  skipped dot-dirs and `verify`'s did not, so `gateway.json` said
  "1 profile(s) not served" while `hscc verify` named two. Both now share one
  roster helper, `hscc_daemon/profile_roster.list_profiles_on_disk`, which
  mirrors Hermes' gate (identity marker + profile-id grammar + not tombstoned
  + not parked), using Hermes' own predicates when `hermes_constants` is
  importable and a conservative mirror when it is not — the daemon deploys
  under `~/.hermes/plugins`, where it need not be. Coverage is NOT weakened: a
  directory that carries an identity marker and is unserved still fails, and
  names the missing profile on both surfaces. Measured on this host: the naive
  scan's expected-minus-served was `['.deleted', 'ecofire-bc']`; the shared
  gate's is `[]` (46 real profiles, all served).
- **`watchdog.json` reported "Degraded — gateway not reachable" while the
  gateway job and vLLM were both healthy.** Same root cause, not a separate
  defect: `check_gateway()` forces `ok=False` on a multiplex failure, and the
  watchdog copies that into its own verdict (and into the 3-failures-in-10min
  pipeline breaker). With the false-fail gone the stream reports healthy —
  `gateway.json` shows `gateway_job: true`, `vllm_healthy: true`,
  `multiplex_ok: true`. No no-op vLLM auto-restart is triggered by this check
  (`auto_restart_count` moves only on a `not dgx_ok` path), so the counter's
  270 is historical and untouched here.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_profile_roster_multiplex_t1c545ebb.py`: **16 new**
  cases — the card's two required pins (a dot-directory under `profiles/` is
  never a profile id; a directory with no profile config is never counted as
  expected-served), plus identity-marker set, `default`, id-grammar, tombstone,
  parked, and a reproduction of this host's exact leftover shape. Every roster
  case runs against **both** branches (Hermes' predicates injected, and the
  fallback mirror forced) and asserts they agree, so the mirror cannot drift
  into a false-fail silently. `check_gateway()` is pinned too: a false multiplex
  fail no longer drags the gateway verdict red.
- Mutation probe: restoring the old naive scan (append-a-shadowing-def, byte
  copy + restore) turns **13 of the 16** new cases RED (`13 failed, 3 passed`)
  — they pin the gate, not its own shadow. `verify.py`/`health.py` fixtures in
  the pre-existing suites that used a bare `mkdir()` as "a profile" now use a
  helper that writes a `config.yaml`, so the "unserved real profile must fail"
  intent stays pinned rather than being satisfied by a stub.
- Suites: `test_verify.py` + `test_health.py` + new file — **248 passed**.
  Live host (read-only, hermes venv): `verify.check_multiplex()` →
  `ok=True, "all 46 profiles served"`; `health._check_multiplex_profiles()` →
  `ok=True`. Both agree, and the two counts differ only because `verify` counts
  the served list (46 incl. `default`) and the daemon counts the
  served∩expected intersection (45, `default` is not a dir under `profiles/`).
