# Task t_7cc2e7d5 — Consume session-ownership handoff (app drives session 1:1)

Status: DONE — implementation + hermetic tests + iOS surfacing, verified (run 904 continuation)
Assignee: backend-engineer
Upstream seam: fork branch feat/session-ownership-handoff @ 77f046f089 (commit ed7c04b338 + 77f046f089), pom11/hermes-agent. NOT live (dep-bump-2026.9.24 lacks primitives at runtime) → no-deploy path; needs the seam deployed to validate the live 1:1 turn (t_ff4b986e's domain).

## The seam (documented contract, from active_sessions.py @ 77f046f089)
- request_active_session_handoff(session_id, *, controller, registry_home) -> ({granted:bool, owner:{surface,pid,live_session_id}} | None, ActiveSessionRefusal|None)
- grant_active_session_handoff(session_id, *, controller, live_session_id) -> nonce|None  (OWNER-only)
- complete_active_session_handoff(session_id, *, controller, nonce, surface, metadata, registry_home) -> (ActiveSessionLease|None, refusal|None)
- SESSION_HANDOFF_PENDING vs SESSION_NOT_OWNED distinct. TTL 300s.
- RPCs on gateway: session.request_handoff, session.handoff_consent, session.complete_handoff.
- Nonce flows owner→controller out-of-band; server reads the granted nonce from the shared registry entry it itself coordinates through.

## What was my architecture decision
HSCC-side server (serve bridge) drives the handoff end-to-end by calling the hermes
PRIMITIVES directly against the project profile's registry:
  1. request_active_session_handoff(session, controller="hscc-app", registry_home=profile_home)
     - (None, SESSION_NOT_OWNED/no owner) -> fallback session_busy
     - {granted: False} -> emit handoff_pending "waiting for approval on your computer"
     - {granted: True} -> owner already consented; proceed
  2. Poll owner's registry entry (read-only) until handoff.granted==True capturing nonce,
     or TTL expiry -> session_busy/denied.
  3. complete_active_session_handoff(session, controller, nonce, surface="tui", ...) -> lease
  4. drive turn via existing send_user_message PTY path
  5. on turn end, lease.release() -> hand back to operator CLI.
Seam availability detection: hasattr(hermes_cli.active_sessions, 'request_active_session_handoff').
If absent (no deploy) -> fall back to existing session_busy notice (no regression).

## Files
- NEW hscc-api/active_sessions_handoff.py  (coordinator)
- MOD hscc-api/routes_ws.py  (wire handoff into _handle_client_send; injectable coordinator_factory)
- NEW hscc-api/tests_gateway/probe_05_handoff_coordinator.py  (cross-process probe vs real fork seam)
- NEW hscc-api/tests/test_handoff_coordinator.py  (17 hermetic tests)
- NEW hscc-api/tests/test_ws_handoff_path.py  (7 hermetic WS-wiring tests)
- MOD ios-app/Sources/HSCC/Views/StreamingChatStore.swift  (TurnState enum + pure derive; drives banner)
- MOD ios-app/Sources/HSCC/Views/StreamingChatView.swift  (turn-state banner + SystemRow icons for handoff_pending/working/session_busy)
- MOD ios-app/Sources/HSCC/Views/SessionHistoryView.swift  (systemLabel for the three kinds)
- NEW ios-app/scripts/turn_state_check.sh (+ turn_state_check/ harness) — headless proof of TurnState.derive
- MOD hscc-api/tests/test_routes_orchestrator.py  (teardown-clear fix for pre-existing cross-file leak)
- THIS docs/TASK_t_7cc2e7d5_handoff-consume.md
- (SessionActivitySummary.swift NOT modified — its generic systemDetail already falls back to p.kind, enough)

## Verification
- hscc-api FULL suite green on both interpreters: hermes venv 3.11.16 = 869 passed, 1 skipped; p313 = 854 passed, 16 skipped (post teardown-clear fix). Interpreter-split counts differ only by skip reasons (e.g. optional deps).
- NEW hermetic suites: test_handoff_coordinator.py 17/17; test_ws_handoff_path.py 7/7 — green on both interpreters.
- Cross-process probe (probe_05_handoff_coordinator.py) against the REAL fork seam @ /tmp/hscc-fork-seam: full request→grant(owner subprocess)→complete→drive→release cycle verified by execution on both interpreters.
- iOS: scripts/turn_state_check.sh compiles the REAL TurnState.derive + REAL SessionEvent decode layer, feeds real wire JSON, asserts the handoff-pending/working/busy states — ALL PASSED. check_sources.sh in sync (88 Swift files, no new Sources/ files).

## What needs deploy (out of this task's reach)
- The hermes seam (feat/session-ownership-handoff) must be live for the full 1:1 drive to work end-to-end; today the live dep-bump-2026.9.24 lacks the primitives, so the coordinator degrades to the pre-existing session_busy notice (no regression). Deploy + live validation is t_ff4b986e's domain.

## Findings so far
- Approach A (direct primitives from the API-server process) chosen over Approach B (gateway JSON-RPC session.* over /api/events) — A is genuinely hermetic-testable in an isolated HERMES_HOME; B needs the serve gateway to be the controller exposing methods over a device path (complex, unverifiable today).
- The controller drives the turn via the existing relay_user_message hook, which routes through the serve PTY when mounted. Honest caveat: after the controller completes the handoff, driving through the serve PTY requires the serve process to hold the lease — needs the deploy to validate.
- Nonce conveyance is out-of-band per upstream; the coordinator polls the shared registry snapshot for the granted nonce.
- Seam-absent fallback to session_busy preserves prior behaviour exactly (no regression) until the seam deploys.
- Pre-existing p313-only full-suite flake (test_reap_is_triggered... hardcodes chat-1 leaked into test_ws_relay_not_noop's stop no-op) fixed by clearing _jobs at teardown too in _clear_jobs.
