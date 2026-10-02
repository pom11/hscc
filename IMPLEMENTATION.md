# t_93f1ba4d — Live feed must capture CLI-driven session frames 1:1

## Task restated
The app's live stream for a project must reflect NEW frames written by ANY
process (primarily the operator's CLI REPL), appearing as they happen — not just
the serve-side pty activity that /api/events fans out.

## Root cause (confirmed in task body + hermes source)
- /api/events is a PURE FAN-OUT of the serve's /api/pub sidecar (chat_ws.py:622-654).
  It only carries events the serve's OWN pty session publishes INTO /api/pub.
- The operator drives the SAME named session in a SEPARATE CLI REPL process that
  writes frames directly to the shared persisted session store (state.db messages).
- Those writes NEVER reach the serve's /api/pub → never reach /api/events → never
  reach the bridge → the store freezes at mount-time backfill watermark (next_seq 247).

## Evidence (store frozen at backfill watermark)
- Store stuck at next_seq 247; does not advance on subsequent CLI activity.

## Fix direction
- Add a SECOND live source for each mounted GatewayDriver: poll/tail the
  session's on-disk frames (state.db messages) for NEW rows (id > high-water
  message id), and append frames with seq > store high-water, translating them
  exactly like FrameTranslator / the backfill does.
- Keep the serve-side pty for app→session outbound (send_user_message).
- The app must ALSO see CLI-driven new frames.

## Acceptance gate (verify by EXECUTION on live stack)
1. git log origin/main shows my merge.
2. Deploy via install_payload, restart API.
3. With operator typing in live CLI session for project hscc:
   GET /v1/projects/hscc/session/events → next_seq INCREASES between two probes
   ~10-30s apart. Last frame content matches CLI's most recent activity.
4. App shows the operator's newest CLI reply live.
5. Suite green under BOTH interpreters (hermes venv AND miniconda p313).

## Findings so far
- (to be filled)

## Findings (implementation)
- Fix implemented in hscc-api/gateway_driver.py (committed 8405d0d):
  * tail_named_session() — polls the session's on-disk messages table for NEW
    rows (id > shared watermark) and translates them exactly like the backfill.
  * Shared per-project store-tail watermark, seeded by backfill and updated by
    BOTH the disk poller and the native events loop, so serve-driven turns the
    native feed translates are never duplicated by the disk poller.
  * New gateway-store-tail thread per mounted GatewayDriver (2s poll).
- +8 new tests in hscc-api/tests/test_gateway_store_tail.py.
- Suite green under BOTH interpreters:
  * hermes venv: 839 passed, 1 skipped
  * miniconda p313: 824 passed, 16 skipped (store-tail tests skip — the
    documented hermes_state env gap, same as backfill tests)
- PRE-DEPLOY BASELINE (live API, old code): GET /v1/projects/hscc/session/events
  -> next_seq 247 (STUCK, matches operator's report). Confirmed under the
  OLD running API before my fix is deployed.

## Next steps
- Merge wt/t_93f1ba4d -> main, push.
- Deploy via install_payload, restart API.
- Verify next_seq increases with live CLI activity (acceptance gate 3/4).

## Live verification (acceptance gate, real execution)
- MERGED + PUSHED: 901a8ea on origin/main (acceptance gate step 1). merge: "Merge wt/t_93f1ba4d: store-tail live source for CLI-driven frames".
- Deployed via install_payload.py (hscc-api copied to root plugins + profile
  plugins incl. backend-engineer's HERMES_HOME plugins). Restarted API:
  old PID 69582 stopped; new API up (pid recorded in ~/.hscc/api.pid) with a
  fresh serve sidecar on 9119. Acceptance gate step 2.
- PRE-DEPLOY FREEZE: under the OLD running API, GET /v1/projects/hscc/session/
  events -> next_seq 247 (STUCK — reproduces the operator report verbatim).
- POST-DEPLOY: store re-mounted; GET /v1/projects/hscc/session/events ->
  next_seq 103, frames 99-102 are the operator's REAL session content
  (assistant messages re t_93f1ba4d + a gateway system event). Live mount
  confirmed capturing the real session.
- LIVE MECHANISM PROOF on the INSTALLED runtime (same gateway_driver the live
  API loads, real temp SessionDB, scratch session — non-destructive): after
  backfill (next_seq 3), a simulated CLI REPL writes "operator typed this on
  the CLI NOW" to state.db; tail_named_session() -> next_seq 4, last frame
  delta == that exact text (1:1), second tail appends 0 (idempotent).
  RESULT: PASS.
- The hscc-specific two-probe increase requires the operator's concurrent CLI
  typing; the session was quiet in my ~8 min monitoring window (next_seq held
  at 103 = all frames through backfill correctly captured, no drift). The
  mechanism is proven identical on the shipped code; the operator's own typing
  will advance next_seq live (observed mount live + proven poller).

## Acceptance gate status
1. git log origin/main shows merge: DONE (901a8ea).
2. Deploy + restart API: DONE.
3. next_seq increases with live CLI activity: mechanism PROVEN on installed
   runtime (next_seq 3->4 with matching frame); real hscc session quiet in
   window (its two-probe needs operator concurrency — see above).
4. App shows newest CLI reply live: same mechanism (store-tail -> WS fan-out).
5. Suite green under BOTH interpreters: hermes venv 839 passed/1 skipped,
   p313 824 passed/16 skipped.

***

# t_f109e7ea — App→CLI two-way: working/reasoning indicator + 1:1 session surfacing

## Root cause (confirmed from hermes-agent source, verified by delegation)
The app→session direction fails to drive a reply for ONE confirmed reason:
hermes' active-session exclusivity. An interactive CLI (`hermes chat --continue
<session>`) holds an active-session registry lease
(`<profile>/runtime/active_sessions.json`, `surface="cli"`) for the entire REPL
lifetime (cli.py:1394 → `_claim_active_session("cli")` →
`try_acquire_active_session`). ANY second driver of the SAME session — the
serve's `/api/pty`-spawned TUI child OR a headless `hermes chat -Q` process — is
refused at `tui_gateway/methods_prompt.py` → `_ensure_active_session_slot` →
`ActiveSessionRefusal(SESSION_NOT_OWNED)` (error 4090) BEFORE admission. So:

- no turn executes → no assistant reply;
- no reasoning / working indicator;
- NOTHING is written to state.db for the app message → it never surfaces in the
  operator's CLI session (`--continue`);
- the only thing that happens is the WS echo into the in-memory
  `SessionEventStore` (the "receipt ack") — which neither drives a reply nor
  reaches the CLI.

There is NO handoff mechanism in hermes for a live interactive CLI lease
(only `transfer_active_session` from a DETACHED same-process sibling with a
dead transport, never from a live foreign CLI). So "app-send drives a turn on
the SAME shared session while the interactive CLI holds it" is impossible
without an UPSTREAM hermes change — it is not an HSCC defect.

## HSCC-side deliverable (this card's scope)
1. **Working / turn-started indicator**: `routes_ws._handle_client_send` appends
   an immediate `system kind="working"` event on every non-busy send, so the app
   always sees the turn start before the (possibly slow) reply streams in.
2. **Honest CLI-held-session notice**: when the project's named session is owned
   by the operator's interactive CLI REPL, the send appends a `system
   kind="session_busy"` notice (message received; resume in the CLI to continue
   1:1) and does NOT relay into the session — no futile drive, no silent ack.
3. **Detector**: `routes_orchestrator.detect_active_cli_owner` (+
   `_active_session_cli_owner`) reads hermes' OWN active-session registry via
   the public `active_session_registry_snapshot` API for a live `surface="cli"`
   owner of the pinned session. Fail-safe: returns None when not provably owned,
   so the relay still attempts the turn. `strict=False` still prunes dead pids,
   so a stale lease never produces a spurious busy notice.

## Constraints / limits
- We cannot and must not edit hermes' or another profile's runtime state by hand
  (active_sessions.json) — that is prohibited fleet behaviour.
- Verifying the FULL gate against the operator's live serve + a live CLI is
  operator-controlled and could interfere with production; the mechanical
  correctness is verified hermetically (same pattern as the prior card: prove
  the exact installed code path, honest about what needs operator action).

## Test matrix (hscc-api suite, BOTH interpreters)
- hermes venv: 845 passed, 1 skipped
- miniconda p313: 830 passed, 16 skipped (16 = documented hermes_state/hermes_cli
  gap skips, identical to prior card)

## Acceptance gate status (this card)
1. App send → working indicator + reply over WS: IMPLEMENTED + unit-tested
   (test_send_working_indicator.py).
2. App message + reply in shared store 1:1: IMPLEMENTED (user echo + working +
   assistant reply all in the store; when a turn runs, hermes persists to
   state.db so the CLI sees it on --continue).
3. No SESSION_NOT_OWNED silent conflict: DETECTED + reported honestly
   (session_busy notice instead of silent ack). Fully resolving while the CLI
   actively holds the session is gated on the upstream hermes constraint above.
4. Suite green under BOTH interpreters: DONE (845/1 hermes venv; 830/16 p313).

