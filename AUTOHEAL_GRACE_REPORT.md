# Auto-heal startup grace keyed on container age — implementation report

Task t_a763020e (re-dispatch of t_61f27161). Branch wt/autoheal-grace2.

## What was fixed
A still-loading unit (fresh container, never served) is now shielded from the
force-recreate auto-heal path. The grace keys on container creation time
(docker inspect) PLUS a persisted has-served flag, both DURABLE across a daemon
restart — so a still-loading unit never gets a fresh in-memory 3-strike
countdown into a force-recreate just because the debounce reset.

## How it works (hscc_daemon/health.py)
- WORKER_AUTOHEAL_LOAD_GRACE (health.py:78-83): configurable via env
  HSCC_WORKER_AUTOHEAL_LOAD_GRACE. Default 1200s (20 min), derived from the
  repository's real observed load bound VLLM_LOAD_GRACE_MINUTES
  (lifecycle.py:125, default 20 min) — the documented large-model
  weight-staging phase (5-10+ min to first serving). This makes the operator's
  stopgap (HSCC_WORKER_AUTOHEAL_DEBOUNCE=15 / COOLDOWN=30) unnecessary: the
  shutdown is keyed on durable container age, not the debounce value.
- _worker_container_state + _WORKER_CONTAINER_STATE_FILE (health.py:88-92):
  persisted per-unit (node, port) record {created_ts, has_served}.
- _load/_save_worker_container_state (health.py:1038-1074): durable JSON, atomic
  tmp+rename, best-effort.
- _worker_container_created_ts (health.py:1099-1139): injectable container-age
  helper. Real implementation runs `docker ps --filter publish=<port> --format
  '{{.ID}} {{.CreatedAt}}'` (the docker inspect path) and parses the creation
  timestamp. FAIL-OPEN: any failure returns None ⇒ no suppression (existing
  behavior preserved).
- _suppress_autoheal_for_startup_grace (health.py:...): at the auto-heal
  decision point, returns True when container age < grace AND has never served.
  Detects container recreation (live created_ts differs from recorded) and
  resets has_served so a recreated, still-loading container gets a fresh grace.
- check_workers: loads the persisted state each check; marks has-served when
  /health returns OK (no shell-out on the healthy path — preserves the
  "healthy units cause no shell-outs" invariant); suppresses auto-heal for a
  young never-served unit (counts as down, not healed).

## Evidence
- Mutation test: neutering _suppress_autoheal_for_startup_grace makes the
  young-never-served / restart / recreate tests FAIL (auto-heal fires) —
  proves the tests catch the regression. Restored, all green.
- hscc_daemon/tests/ : 1053 passed (includes test_health.py 74 + the new
  TestWorkersStartupGrace 5 tests).
- test_no_live_hscc_leak.py: passes after adding health to _isolate_hscc
  _module_attrs() (it caught the new worker_container_state.json leaking into
  the sandbox — a real leak seam my change introduced and the audit correctly
  flagged).
- test_no_real_addresses_committed.py: passes (no real host/IP in the diff).
- Full suite: see FULLSUITE_EXIT in /tmp/full_tests2.log.

## Tests added (hscc_daemon/tests/test_health.py, TestWorkersStartupGrace)
1. test_young_never_served_no_autoheal — young container, never served, many
   down-checks ⇒ no auto-heal.
2. test_young_never_served_survives_daemon_restart — in-memory debounce wiped,
   on-disk container-age record survives ⇒ still no auto-heal (ACROSS restart).
3. test_old_never_served_is_genuine_wedge_and_heals — old container, never
   served ⇒ auto-heal fires (genuine wedge, not a load).
4. test_served_then_stopped_still_heals — has served, then stopped ⇒ auto-heal
   fires promptly (genuine failure).
5. test_recreated_container_gets_fresh_grace — recreated container resets the
   serve history ⇒ fresh load shielded again.

## What I did NOT do (and why)
- Did NOT force-recreate against the live fleet (acceptance rule).
- Did NOT run `hscc doctor` / bootstrap.sh against the live runtime (they
  mutate ~/.hermes/config.yaml). All verification via the unit suite.
- Did NOT use Telegram or mutate live state in any test.
- Did not touch the operator's live ~/.hscc; the autouse isolation fixture
  redirects every write to a per-test tmp dir.

## Commits (branch wt/autoheal-grace2, off dev)
- 59440e9 auto-heal: startup grace keyed on container age + has-served
- 079390d auto-heal: redirect new container-state file in _isolate_hscc
