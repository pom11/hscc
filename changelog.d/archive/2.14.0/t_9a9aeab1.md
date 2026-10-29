kind: Fixed
task: t_9a9aeab1

- **`project new --apply` died under the brew keg venv and left an orphan
  stub profile.** The keg venv carries only PyYAML/rich/psutil/markdown-it-py/
  mdurl/pygments (Formula/hscc.rb), so `ensure_session -> _open_profile_session_db`
  hit `ModuleNotFoundError: httpx` mid-import-chain, the session step died, and
  the just-created `<name>-orch` stub profile stayed behind with no registry
  entry — exactly the half-registered state the module docstring promises
  never to leave. The session-db opener now DELEGATES every runtime touch to
  the hermes venv interpreter as bounded JSON-line ops (Hermes' own
  SessionDB APIs on Hermes' own packages — nothing vendored into the keg, so
  no second package set to drift); `HSCC_HERMES_PYTHON` overrides resolution,
  which otherwise probes `$HERMES_HOME/hermes-agent/venv/bin`, `~/.hermes/...`
  and PATH with a sentinel-verified import check. A profile CREATED in a run
  whose session step fails is rolled back through `hermes profile delete -y`
  (Hermes' own verb; a pre-existing profile is never touched), the ensure
  itself is one atomic create+title op, the registry write is a reported step
  instead of a crash, and the rerun path is idempotent (recreate -> ensure
  once -> registered). The **board step had the same wall** (create_board ->
  init_db -> lazy hermes_state -> httpx crashed the whole command): it now
  runs as remote ops on the same transport, and any raw provider crash there
  is reported as a failed step instead of escaping as a traceback. The
  environment-vs-data distinction is preserved verbatim: an unresolvable
  runtime raises `HermesRuntimeUnavailable` naming the missing module, never
  "profile has no state.db".
---
kind: Verified
order: 1

- `hscc-project/tests/test_project_runtime_delegation.py`: **29** new cases —
  interpreter resolution order + sentinel probing, delegation chosen under a
  simkeyed brew-venv interpreter, the op protocol executed END TO END against
  a fabricated agent tree + planted venv python (no operator `~/.hermes` in
  sight), `create_db` semantics for a fresh profile, no-orphan rollback
  (created profile deleted, pre-existing profile untouched, failed rollback
  reported with the exact cleanup command), and retry idempotence across
  three runs. Full `scripts/run_tests.sh` green.
