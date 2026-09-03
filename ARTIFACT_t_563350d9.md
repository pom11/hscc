# Per-project permanent sessions (t_563350d9)

Status: IMPLEMENTED, testing (run #435).

## Task
Each project must have its own permanent chat session:
1. Persist the session id against the project in the registry, server side — a
   phone reinstall must not lose the thread.
2. The chat relay (routes_ws._default_relay) and the history endpoint resolve
   the SAME session for a project.
3. Idempotent creation — two concurrent first-messages must not create two
   sessions.

## Change set (branch wt/t_563350d9)
- hscc-project/flightdeck/core/registry.py
  - Project.session field (default None) + _OPTIONAL_FIELDS += "session"
  - Module-level _registry_lock (threading.Lock)
  - _row_to_project loads session; save_registry emits it (via OPTIONAL loop)
  - add_project(... session=...)
  - set_session(name, session[, path]) -> Project  (mirror set_board)
  - ensure_session(name[, path]) -> str
      * persists deterministic default (= project name) if absent
      * idempotent: returns existing id unchanged, no rewrite
      * concurrency-safe under _registry_lock
- hscc-roles/orchestrators.py  resolve_orchestrator: session = row.get("session") or name
- hscc-api/routes_ws.py         _default_relay: resolve via _registry.ensure_session,
                               invoke orchestrator with the durable id
- hscc-api/routes_session.py    history endpoint: resolve via _registry.ensure_session,
                               key store by session id, surface "session" in payload
- tests: test_project_permanent_session.py (8 tests) + updated fake registries
  in test_session_event.py, test_ws_route.py, test_ws_relay_not_noop.py

## Verification
- hscc-api: 697 passed, 1 skipped (full suite, ~202s) — includes the 8 new
  test_project_permanent_session.py tests and the updated existing ws/session
  fakes.
- hscc-project: 1253 passed (full suite).
- hscc-roles: test_orchestrators.py 29 passed; full suite 93 passed + 1 FAILED
  (test_generator.py::test_every_hscc_owned_worker_profile_has_role_spec) — a
  pre-existing ENVIRONMENT failure (FileNotFoundError: no profiles dir at
  rolelib.PROFILES_DIR), unrelated to the session change.
- test_project_permanent_session.py alone: 8 passed
  - test_ensure_session_persists_deterministic_default
  - test_ensure_session_is_idempotent_and_noop_on_repeat
  - test_ensure_session_returns_persisted_id_without_overwriting
  - test_restart_resolves_the_same_id
  - test_unknown_project_ensure_session_raises
  - test_concurrent_first_sends_produce_exactly_one_session
  - test_two_sends_land_in_one_session
  - test_relay_and_history_resolve_the_same_session

## Requirements → implementation map
1. Persist session server-side -> registry.ensure_session writes session=<name>
   to the project row; _default_relay calls it before invoking.
2. Relay + history resolve SAME session -> both call _registry.ensure_session;
   deterministic default = project name => always the same id; history keys its
   store by the session id and surfaces "session" in the response.
3. Idempotent concurrent creation -> _registry_lock serialises the RMW, so N
   concurrent first-sends yield exactly one persisted session (tested with 32
   racing threads).

## Known trade-off
- The WS event store stays keyed by project name; the durable session id (=
  orchestrator `--continue` thread) is what's persisted. They agree because the
  deterministic default IS the project name. Renaming via set_session is an edge
  case; relay uses the persisted id for the invoke, which is what carries the
  thread.
