# Design: Session ownership handoff (HSCC app ⟷ CLI, "remote-control" continuity)

Date: 2026-10-02
Status: Proposed
Author: hscc-orch
Board: hscc (implementation → upstream hermes via feature card)

## Problem

HSCC's session-continuity makes the app a live viewer of a project's named Hermes
session. Direction session→app fully works (store-tail live source). Direction
app→session does not: when the operator's interactive CLI holds the session, an
app send is refused by Hermes' per-session exclusivity fence with
`SESSION_NOT_OWNED` (4090) before a turn starts — no reply, no working indicator,
nothing written. The HSCC bridge already degrades gracefully (working indicator +
`session_busy` notice), but the operator wants the app to be able to genuinely
drive the session from the phone, the way Claude Code's remote control lets a
remote drive the same agent.

## The two fences (from hermes source, confirmed by reading the code)

1. **Active-session fence** — `hermes_cli/active_sessions.py`. A live interactive
   CLI claims a per-session lease (`try_acquire_active_session`, surface=cli) held
   for the whole REPL. Any second driver is refused BEFORE admission at
   `tui_gateway/methods_prompt.py:603 _ensure_active_session_slot` →
   `SESSION_NOT_OWNED`. Identity = (pid, live_session_id); `_is_same_writer`
   bypasses only for the identical caller (re-entrancy, not concurrency).

2. **Durable turn lease** — `agent/turn_facade_lease.py`. `run_conversation` takes a
   per-turn lease (`admit_durable_turn_lease`), keyed `pid=<pid>:turn=..:platform=..`.
   This one WAITS (TTL 300s, wait up to 1800s) rather than refusing instantly — the
   exact "another Hermes process is using this session... waiting" path. ACP goes
   through `run_conversation` too (`acp_adapter/server.py:803`).

## What exists (the raw materials)

- `transfer_active_session(lease, session_id=...)` — moves a lease (used by
  `_take_over_detached_runtime_lease`), but ONLY for a detached sibling in the SAME
  process (`_detached_lease_holder` iterates `_sessions`, requires dead transport).
  It explicitly leaves "live foreign pid" exclusivity untouched.
- `hermes acp` (ACP server, `acp_adapter/`) — Agent Client Protocol, the same
  protocol family Claude Code's remote control uses. IDE integration today.
- `GatewayDriver` + serve sidecar (`hscc-api/gateway_*`) — HSCC's bridge.

## Missing piece (the patch)

No FIRST-CLASS "authorized remote controller" handoff exists: a trusted controller
cannot gracefully take the active-session lease from a LIVE foreign CLI (the CLI)
in another process. The takeover path only covers a same-process detached sibling.

## Proposed change (upstream hermes feature)

Add an **authorized-controller handoff** primitive to the active-session lease:

- A caller that identifies as a trusted controller (a scoped/signed token minted by
  the serve bridge — reuses `HERMES_DASHBOARD_SESSION_TOKEN` precedent) may invoke
  a new `request_active_session_handoff(session_id, controller)`.
- The owner CLI is asked to consent (server→client request surfaced in the TUI:
  "HSCC wants to take over session X — Allow for one turn / Always / Deny").
- On Allow, the lease is transferred to the controller (reuse
  `transfer_active_session`); the CLI is told it lost ownership and may (a) stop or
  (b) revert to read-only live-view of the session the controller now drives.
- The controller becomes the session owner for its turn, then hands back (release →
  CLI re-claims) on turn end — the "whichever surface is active drives the loop,
  the other live-views" contract, now with a real ownership transfer.

Safety principle: **exclusivity is a correctness guarantee — the patch adds a
CONSENTEDLEASE transfer, never a silent bypass.** Only the operator's authorized
controller, with the owner's consent, can take over.

## Delivery path

- Write full spec → file a hermes-side feature card (assignee model that modifies
  the hermes agent repo) — this is UPSTREAM work, so it lands on the hermes/
  codebase, and HSCC consumes whatever seam it exposes.
- HSCC side: wire the app send to invoke the handoff + render the consent/owner
  states; keep the `session_busy` notice until handoff is consented/complete.
- Verify by execution on the live stack: app send (while CLI holds session) →
  consent prompt in CLI → transfer → app drives a turn → reply streams to app AND
  the CLI transcript shows it (1:1), then handoff back.

## Scope note / decision not made

This is deliberately a larger upstream change. Alternative (simpler, but NOT
1:1-identical-session) is Option B: have the app send drive a *separate* headless
session and mirror results into the shared store so the app sees a coherent
transcript. That's HSCC-only, immune to the upstream fence, and arguably good UX —
but it is NOT the same session as the CLI, so it fails the operator's explicit
"1:1 identical" requirement. The operator has twice stated the requirement;
this spec targets the A (remote-control) approach. If scope/risk win over
fidelity, Option B is the fallback.
