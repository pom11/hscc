# Live-probe findings: Hermes gateway protocol for the dashboard chat + tool-call feed

Task: t_776e294a (increment 3 — outbound gateway driver for hscc-api)
Captured against an **isolated** `hermes serve --isolated` instance on a scratch
port 9211 with a throwaway home (`/tmp/hermes_iso_home_t776`). No connection to
the live operator gateway (port 9119), no writes to its state.

## How the isolated instance was brought up

The live operator gateway on 9119 must not be touched, so the probe uses a fully
isolated instance:

```
HERMES_HOME=/tmp/hermes_iso_home_t776 \
HERMES_DASHBOARD_SESSION_TOKEN=iso_probe_token_7f3a9c2e \
HERMES_SERVE_HEADLESS=1 \
hermes serve --isolated --port 9211 --host 127.0.0.1 --skip-build
```

- `--isolated` keeps this a dedicated per-profile server (avoids re-exec to the
  machine dashboard).
- `HERMES_HOME` points at a scratch home so no operator/profile state is touched.
- `HERMES_DASHBOARD_SESSION_TOKEN` pins the ephemeral `_SESSION_TOKEN` so the
  probe can authenticate as `?token=`.
- The scratch home's `config.yaml` was seeded with a minimal `model` block
  pointing at the local vLLM endpoint (`http://localhost:4000/v1`, `worker-model`)
  so the agent can actually run. Without it the agent emits
  `{"type":"error","payload":{"message":"No inference provider configured"}}`.

## Auth

- WS endpoints take the shared token as a query param: `?token=<TOKEN>`.
- Wrong token ⇒ WebSocket handshake refused (HTTP 401/InvalidStatus). Verified.
- Per-channel fan-out requires the SAME `?channel=<id>` on pub + sub sides.

## Wire framing

- Every frame is newline-framed JSON text.
- `/api/pty` additionally streams raw ANSI byte payloads (binary/multibyte) for
  the terminal render. The JSON is UTF-8 text.

## Two qualitatively different frame kinds on /api/events

The `/api/events` feed carries the dispatcher's writes verbatim, and those are a
MIX of JSON-RPC **responses** and JSON-RPC **event notifications**. The driver
must filter on `method == "event"`.

### 1. JSON-RPC responses (NOT events — ignore for the feed)
```json
{"jsonrpc":"2.0","id":"r24","result":{"sessions":[{...}]}}
{"jsonrpc":"2.0","id":"r2","result":{"config":{...}}}
{"jsonrpc":"2.0","id":"r5","result":{"provider_configured":true}}
```
These answer the TUI's internal RPC calls (session.list, config.get, ...).
Distinguished from events by the presence of `id` and `result` and the ABSENCE
of `method:"event"`.

### 2. Event notifications (the real feed)
Envelope:
```json
{"jsonrpc":"2.0","method":"event","params":{"type":"<TYPE>","session_id":"<SID>","payload":{...}}}
```

## Captured event types and payloads (real, verbatim)

- **gateway.ready** — `payload: {skin:{...}}` (theme). Boot signal.
- **session.info** — full session: model, provider, tools list, etc. Fires on connect.
- **sessions.changed** — `payload: {}`. Coarse "session set changed".
- **message.start** — `payload` absent (no payload key). New assistant message begun.
- **message.delta** — `payload: {"text":"<chunk>"}` (no role). Stream chunks of the
  assistant message.
- **message.interim** — `payload: {"text":"<...>","already_streamed":true}`. Interim
  full-text snapshots during long generation.
- **thinking.delta** — `payload: {"text":"<chunk>"}`. Reasoning token stream.
- **reasoning.available** — reasoning summary available.
- **tool.generating** — `payload: {"name":"read_file"}`. Tool about to be called.
- **tool.start** — `payload: {"tool_id":"call_...","name":"read_file","context":"<arg preview>"}`.
- **tool.complete** — `payload: {"tool_id":"call_...","name":"read_file","args":{...},"duration_s":0.215,"result":{...}}`.
- **status.update** — `payload: {"kind":"lifecycle","text":"⚠️ ..."}`.
- **error** — `payload: {"message":"agent init failed: No inference provider configured. ..."}`.

## Driving chat over /api/pty

`/api/pty` spawns the real Ink TUI (`node ui-tui/dist/entry.js`) in a PTY and
streams its stdout as raw ANSI bytes. To send a message you must type it
char-by-char, then send `\r` (CR) — NOT `\n` — for Enter:

```python
for ch in "say hi":
    await pty.send(ch.encode()); await asyncio.sleep(0.05)
await asyncio.sleep(0.4)
await pty.send(b"\r")   # Enter submits the prompt
```

- Sending `\n` (LF) only inserts a line break; it never submits. The text is
  echoed into the input box but `message_count` stays 0 and status stays `idle`.
- Sending the whole string in one WS frame also fails; the TUI needs per-keypress
  delivery (this matches how the browser's xterm.js sends input to /api/pty).

## Captured artifacts

- `probe_01_auth_events.py` — token auth + /api/pub->/api/events fan-out probe.
- `probe_02_pty_capture.py` — spawn TUI, drive chat, capture boot + prompt frames.
- `probe_03_tool_capture.py` — drive a real tool call, capture tool.start/complete.
- `probe_03_tool_frames.jsonl` — 284 real event notifications (the ground-truth corpus).

## Mapping table (native -> hscc session_event)

| native type            | hscc event type | notes |
|------------------------|-----------------|-------|
| message.start          | message (role=assistant) | open assistant message; accumulate deltas |
| message.delta          | message (assistant)    | concat text; emit on message.end/flush |
| message.interim        | message (assistant)    | replace interim text |
| tool.start             | tool_call (status=running) | tool_id from payload.tool_id |
| tool.complete          | tool_call (status=done)   | merge result json |
| thinking.delta         | (card/system, optional)  | reasoning stream, may be dropped or surfaced |
| session.info / gateway.ready / sessions.changed | system | bootstrap housekeeping |
| status.update          | system | lifecycle note |
| error                  | error | payload.message |

The binary ANSI bytes from /api/pty (terminal rendering) are NOT events and are
NOT translated — the driver only relays the driver's own writes and ignores the
raw terminal bytes except where text is recoverable from message.delta frames.

## Isolation safety record

- Only scratch port 9211 used; live 9119 never contacted.
- Scratch home /tmp/hermes_iso_home_t776 only; no /Users/desac/.hermes state written.
- All frames captured from the isolated instance.
