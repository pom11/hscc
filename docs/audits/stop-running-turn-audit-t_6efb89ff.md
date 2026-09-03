# Chat: stop/cancel a running turn — gap record (t_6efb89ff)

Status: BLOCKED ON SERVER — no server-side cancel route exists. Recorded per the
card's explicit fallback: "If no cancel route exists, record precisely what is
missing and stop."

A client-only stop button would be a lie: it would hide output locally while the
orchestrator keeps running the (possibly wrong) turn to completion server-side,
exactly what the card forbids. Stopping here rather than faking it.

This is an iOS card, but the blocker is entirely server-side (the HSCC HTTP API +
gateway). The iOS app is the client; it cannot cancel a turn the server gives it
no handle to cancel.

---

## Bottom line

There is NO cancel/stop/interrupt route, WS kind, or PTY control for a running
chat turn anywhere in `hscc-api/` or `hscc_daemon/`. A message once sent runs the
orchestrator turn to completion (or the 600 s wedge backstop), and the operator
can only watch — which is precisely the gap the card describes.

## Evidence (file:line)

1. WS inbound protocol handles only `send`. `routes_ws.py:190-204`
   (`_process_inbound`) switches ONLY on `kind == "send"`. A client
   `{"kind":"stop"}` / `{"kind":"cancel"}` / `{"kind":"interrupt"}` frame has no
   handler — it is silently ignored (the `if` simply doesn't match). The wire
   contract (module docstring, `routes_ws.py:21-25`) documents exactly one
   client->server message: `{"kind":"send","text":"..."}`.

2. The live streaming path (GatewayDriver over `hermes serve`) has NO interrupt
   primitive. `gateway_driver.py` exposes only:
     - `send_user_message(text)` (line 668) — types chars + `\r` into the PTY.
     - `stop()` (line 698) — tears the driver down entirely; closes sockets,
       does NOT interrupt the in-flight hermes turn (the hermes process keeps
       running, its reply simply has no socket to land on).
   There is no `interrupt()` / no method to send Ctrl-C (0x03) down the PTY.

3. The no-GatewayDriver fallback path is job-based and unkillable. `routes_ws.py`
   `_default_relay` (lines 139-182) spawns `_backing_invoke` on a background
   thread. `_backing_invoke` (`routes_orchestrator.py:232-334`) runs
   `subprocess.run(["hermes", ...], timeout=600)` (lines 277-279) — BLOCKING,
   and the Popen handle is never retained, so a second request has nothing to
   kill. The `hermes chat` subprocess runs to completion or the 600 s timeout.
   The reply lands as ONE complete `message` event (delta=reply, done=True) —
   there is no token stream to "stop" mid-way in this path.

4. No cancel REST route. `routes_orchestrator.py:1554-1556` registers only
   `POST /v1/orchestrator/chat` and `GET /v1/orchestrator/chat/{id}`. There is no
   `DELETE /v1/orchestrator/chat/{id}` (kill the job), no `POST .../stop`, no
   cancel mutation. The exhaustive route sweep confirms no stop/cancel route.

5. The only "stop" routes in the whole API are unrelated to a chat turn:
   `POST /v1/cluster/stop` (`routes_actions.py:329`) = stop the whole cluster,
   and daemon `sparkrun stop` / `daemon stop` (cluster/infra level). None
   cancels a running turn.

6. The old job-based chat exposes a CLIENT-ONLY "Stop waiting" that proves the
   point. `OrchestratorChatView.stopWaiting()` (`OrchestratorChatView.swift:447`)
   calls `store.abandonWaiting()` + `pollTask?.cancel()` — it clears the LOCAL
   spinner/poll. The server-side `hermes chat` job keeps running to completion;
   the view's own footer comment (lines 245-249) says the late answer "can still
   be resumed", because nothing was stopped server-side. This is the exact
   "hide output locally" behaviour the card forbids — evidence that even the
   legacy UI never had a real cancel, only an honest label for abandoning the
   wait. The NEW `StreamingChatView` has no stop control at all
   (`StreamingChatView.swift:110` `store.stop()` is only the view-disappear
   socket teardown; line 273 `store.send` is the only composer action).


## What is missing, precisely (what a real server-side cancel needs)

To ship "stop actually cancels server-side", the server needs ALL of:

A. A cancel transport. Either:
   - a new WS inbound kind `{"kind":"stop"}` handled in
     `routes_ws._process_inbound` (alongside `send` at line 202), and/or
   - a REST route `POST /v1/orchestrator/chat/{id}/stop` (or `DELETE …/{id}`)
     registered in `routes_orchestrator.py` alongside lines 1554-1556.

B. A cancellable execution handle. The single most important missing piece:
   `_backing_invoke` must keep the `Popen` (e.g. `subprocess.Popen` instead of
   `subprocess.run`) so the stop handler can `proc.terminate()` / `proc.kill()`
   it. Today there is NO retained handle anywhere — verified lines 277-279.

C. A registry of in-flight turns keyed by project (and/or job_id) so the stop
   handler can find the running Popen for the project being stopped. Currently
   there is no such registry; the WS relay thread and the job thread are
   fire-and-forget.

D. For the GatewayDriver PTY path: an `interrupt()` method on the driver
   (`gateway_driver.py`) that sends Ctrl-C (0x03) plus `\r` over the PTY socket,
   wired from the new WS `stop` kind. Without it, the only "cancel" available is
   `stop()` which kills the whole driver (and every active connection), not the
   single turn.

E. A transcript notice. On cancel, the server should append a
   `message role="assistant"` (or `system`) event like "turn stopped" to the
   store so the operator sees the cancel acknowledged in the stream — otherwise
   the client cannot distinguish "stopped" from "still running, just quiet".

## What the iOS side would need once the server supports it

- `StreamingChatStore`: a `@Published var isStreaming: Bool` (or "turn in
  flight" derived from transcript state), and a `func stop()` that writes the
  `{"kind":"stop"}` frame on the same `wsTask`, plus a `stopError` publisher.
- `StreamingChatView` / composer: a stop button (square-in-circle glyph) shown
  in place of/next to the send button while `isStreaming`, wired to
  `store.stop()`. AND, crucially, `streaming_check.sh`-style test coverage that
  asserts the stop actually sends the frame (not merely hides a row).

## Recommendation / handoff

This needs a SERVER card (assignee: the api/daemon role) to add A–E above.
Once a `stop` transport exists and `_backing_invoke` retains a killable Popen,
re-open this iOS card to wire the button. The iOS half is small; the server half
is the entire missing substance.

Root cause in one line: the server never retains a handle on a running turn, so
"cancel server-side" is currently impossible, not merely unimplemented in the UI.
