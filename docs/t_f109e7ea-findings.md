# t_f109e7ea — App→CLI two-way: working/reasoning indicator + 1:1 session surfacing

## Status
In progress. Root cause confirmed; design in this doc.

## Root cause (verified against hermes-agent source by delegation)
The app→session direction fails to drive a reply for ONE confirmed reason:
**hermes' active-session exclusivity.** An interactive CLI (`hermes chat
--continue <session>`) holds an active-session registry lease
(`HERMES_HOME/runtime/active_sessions.json`, `surface="cli"`) for the entire
REPL lifetime. Any second driver of the SAME session — whether the serve's
`/api/pty`-spawned TUI child or a headless `hermes chat -Q` process — is
refused at `tui_gateway/methods_prompt.py` (-> `_ensure_active_session_slot` ->
`ActiveSessionRefusal(SESSION_NOT_OWNED)`, error code 4090) BEFORE admission.
Consequences:
- no turn executes -> no assistant reply;
- no reasoning / working indicator;
- NOTHING is written to state.db for the app message, so it never surfaces in
  the operator's CLI session (`--continue`);
- the only thing that happens is the WS echo into the in-memory `SessionEventStore`
  (the "receipt ack"), which neither drives a reply nor reaches the CLI.

There is NO handoff mechanism in hermes for a live interactive CLI lease
(only `transfer_active_session` from a DETACHED same-process sibling with a dead
transport, never from a live foreign CLI). So "app-send drives a turn on the
SAME shared session while the interactive CLI holds it" is impossible without an
UPSTREAM hermes change — it is not an HSCC defect.

## HSCC-side deliverable (this card's scope)
Working within the confirmed constraint, implement + verify:

1. **Working / turn-started indicator**: when a WS `send` is relayed, append an
   immediate store event signalling the orchestrator is working, so the app
   always gets instant feedback. The assistant reply (streamed deltas + done)
   comes from the driver/translator when a turn runs.

2. **Reasoning surfacing**: translate the serve's reasoning stream (if the native
   feed carries it) into the store so the app shows the indicator text.

3. **Honest CLI-held-session notice**: when a send cannot drive a turn because
   the session is owned by the interactive CLI, append a clear `system`/`error`
   "session busy on the machine" notice instead of a silent ack — so the
   operator understands why no reply is coming, and that the message WAS
   received.

4. **Session-not-owned relay fallback**: make the app→session path (driver PTY)
   fall back correctly to the headless REST job when the driver path can't
   drive, and surface the busy state.

## Constraints / limits
- Cannot and must not edit another profile's or hermes' runtime state by hand
  (active_sessions.json) — that is prohibited fleet behaviour.
- Verifying the FULL gate against the operator's live serve + a live CLI is
  operator-controlled and risks interfering with production; the mechanical
  correctness is verified hermetically with unit tests against the store +
  driver harness, using the same patterns as the existing gateway tests.
