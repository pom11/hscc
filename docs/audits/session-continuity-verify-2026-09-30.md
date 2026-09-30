# Session Continuity — Integration + End-to-End Live Verification

**Task:** t_59cc6714 (integration + e2e verify, design §5/§4)
**Date:** 2026-09-30
**Author:** architect
**Branch:** wt/t_59cc6714
**Parents landed:** t_fd031303 (resolver, f9bceb4) + t_f731de64 (backfill + two-way relay, 22a3c11)

## Scope

Prove the whole CLI ⇄ iOS session continuum END-TO-END by execution on the real
stack: for a real registered project, post a message as if from the iOS app
through the bridge and confirm it reaches the named Hermes session; then confirm
resuming on the CLI shows full history INCLUDING the phone-originated message.

Design §3.2 / §3.3 / §4 are the spec. Acceptance:

1. **Backfill determinism** on the live store — reconnect doesn't duplicate frames
   (idempotent, seq contiguous).
2. **Two-way** — app-side send lands in the named session; `--continue` on CLI
   shows full history incl. phone-originated message (one session, not two streams).
3. **Honest error paths** — no session / unknown project / resolution failure /
   backfill failure while live / kanban contention — each reports honestly.

## Findings so far

(in progress — see below)

## Evidence

(in progress — see below)
