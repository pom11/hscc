# Task t_7cc2e7d5 — Consume session-ownership handoff (app drives session 1:1)

Status: IN PROGRESS (run 904)
Assignee: backend-engineer
Upstream seam: fork branch feat/session-ownership-handoff @ 77f046f089 (commit ed7c04b338 + 77f046f089), pom11/hermes-agent. NOT live (dep-bump-2026.9.24 lacks primitives at runtime) → no-deploy path.

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
- MOD hscc-api/routes_ws.py  (wire handoff into _handle_client_send)
- NEW hscc-api/tests/test_handoff_coordinator.py
- NEW hscc-api/tests/test_ws_handoff_path.py
- MOD ios-app/Sources/HSCC/Views/StreamingChatStore.swift
- MOD ios-app/Sources/HSCC/Views/StreamingChatView.swift (SystemRow icon for handoff_pending/working)
- MOD ios-app/Sources/HSCC/Views/SessionHistoryView.swift (systemLabel)
- MOD ios-app/Sources/HSCC/SessionActivitySummary.swift (systemDetail)
- THIS docs/TASK_t_7cc2e7d5_handoff-consume.md

## Verification
- hscc-api tests green on both interpreters (3.11.16 hermes venv + 3.13.7).
- Hermetic coordinator test against fork primitives via isolated HERMES_HOME IF reachable.

## Findings so far
(see commits)
