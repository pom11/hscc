# iOS chat end-to-end — overnight report (2026-10-14)

Branch: `fix/ios-chat-e2e-2026-10-13` (from main `e9a2766c`, hscc 2.7.0 + hooks fix).
PR: **https://github.com/pom11/hscc/pull/25** (draft; CI guard pass x2).
NOTHING released, installed, or restarted on the live runtime:
the operator's API (PID 45019, installed 2.7.0) and daemon were never touched.
All live verification ran against a THROWAWAY world (project `pom`, profile
`pom-orch`, API on 127.0.0.1:8799, tmux session `smoke-pom`).

---

## 1. What was broken (verified evidence)

### 1a. The read path was dead whenever the serve mount failed (the whole point of the bug)

`GatewayDriver.start()` (gateway_driver.py, pre-fix) ran:

```
_resolve_named_session() -> _backfill() -> _connect_upstreams()  # raises on 403
```

`_connect_upstreams()` raising meant **the store-tail poller and pty reader
threads were never spawned** — even though the poller's own docstring says it
"does NOT need the upstream sockets". With every mount 403ing (see 1b), the
store received nothing the terminal REPL wrote, so any WS client saw a stream
frozen at its last directly-appended frame. Confirmed exactly as the reviewer
reported: 234 frames, newest = the user message, no assistant frame ever.

### 1b. Every serve mount 403'd: an orphaned `hermes serve` holding port 9119 with a stale token

Evidence from the live box (addresses redacted to `100.64.0.1`):

* `~/.hscc/logs/api.start.log`: `serve did not bind 127.0.0.1:9119 within 20.0s`
  (20:49:43) then ~100x
  `gateway-bridge: mount driver for <project> failed: ConnectionError("upstream
  rejected WebSocket upgrade: HTTP/1.1 403 Forbidden")`.
* `ps`: serve PID 8958 started **14:44**, PPID 1 (launchd — orphaned by an
  earlier API instance); the current API PID 45019 started **21:00:37**.
* hermes serve receives its session token ONLY as a subprocess env var at
  spawn (`hermes_cli/serve.py:92`, `HERMES_DASHBOARD_SESSION_TOKEN`), and the
  API mints that token **in-process at each boot**. New API => new token =>
  the old orphan serve 403s every upgrade. Reproduced locally: a token-wrong
  WS handshake against the live 9119 gives exactly `403 Forbidden`, while a
  tokenless plain GET gives 401 (so 403-upgrade == token mismatch at the
  auth check in `web_server._ws_auth_ok`).

Two independent gates made this permanent instead of transient:

1. `gateway_bridge.ensure_mounted()` returned early whenever `is_serve_up()`
   was False — no driver at all, so **not even the (serve-independent) tail
   ran** despite `routes_ws` calling `ensure_mounted` on every connect.
2. `start_serve()` health-checked the port by **TCP accept only**: any
   listener (including a stale-token orphan) counted as "our serve" — and
   when our spawn then 403'd, nothing distinguished "wrong serve" from
   "transport broken".

### 1c. Store correctness defects in the captured replay (state.db forensics)

* **Out-of-order replay / "seq 1 at 14:50Z"**: `state.db` `messages.id` order
  is the ground-truth event order; `timestamp` is NOT monotonic — measured
  **4670 of 12282 rows** (session `20260908_134933_152c5b`) regress vs id
  order, worst regression **12.9 days** (id 14301, a context-handoff user
  row). A store sorted or deduped by timestamp puts compaction/handoff rows
  catastrophically out of place — matching the reported ts-min-at-18:10Z.
* **Compaction summary as `role=user` (seq 1)**: live in `state.db` —
  3 rows carry the `[CONTEXT COMPACTION` marker and are emitted with
  `role='user'` (backfill translated them straight into user messages).
* **Duplicate user message (seq 116 and 233)**: the WS send already appends
  the user echo AND marks the tail watermark by `created_at` SECOND only;
  the same state.db row re-appearing later (or the pre-fix native relay)
  re-appended it — no content-level idempotence existed.

### 1d. The stuck banner and the phone's connection resets: ONE root cause, client side

The server's hello envelope is `{"seq":N,"type":"hello","payload":{"next_seq":N}}`
— `kind` appears ONLY on client->server frames. `StreamingChatStore.isHelloFrame`
matched `kind == \"hello\"`, so it NEVER matched:

* `phase = .connected` was set only inside that branch -> banner sat on
  "Connecting to the live stream..." forever while frames streamed in.
* The hello fell through to the seq-cursor as an ordinary event; on a fresh
  or trimmed store its seq looked like a gap -> `reconnect()` -> the repeated
  `disconnected mid-request (ConnectionResetError/BrokenPipeError)` from
  `100.64.0.1` in the API log (consistent with, not provably equal to, the
  operator's observation).

---

## 2. What each commit fixed

| commit | scope |
|---|---|
| `e6c146fb` | docs: this report skeleton (committed first) |
| `596cd7fc` | **A** — store-tail decoupled from the serve mount. `start()` resolves + backfills + spawns the poller FIRST, then attempts native attach. `native_attached()` (both sockets up) is the reuse/retry predicate. `ensure_mounted` no longer no-ops when serve is down: it mounts a **degraded tail-only driver** (new `degraded=True` config) when the project's orchestrator REPL is provably live in tmux (injected seam `_tmux_repl_present`, hermetic default False in conftest). Poll interval 2.0s (already <= 2s). +8 tests (degraded start despite 403; frames reach a connected WS client with the mount failing; degraded reuse; no-serve-no-tmux still mounts nothing). |
| `e4d2f47f` | **B** — `start_serve` port-occupancy heal. Before spawning: if 9119 is occupied, probe it with the REAL handshake (`_serve_accepts_token`, same `/api/events?token=` upgrade the driver performs). Accept a token-matching occupant (adopt — survives crash-restart races); otherwise identify it via `lsof` and SIGTERM ONLY an exact hermes-serve command line (never a foreign listener; SIGKILL after 5s), then spawn our own so the token matches. Verified live against the orphan: wrong-token probe -> False (403 reproduced), non-serve listener -> abstain. +4 tests. |
| `0c42e253` | **B (spam)** — mount-failure backoff: per project, one honest WARNING per 60s window instead of one per WS connect (the 102-line flood), with the suppressed-attempt count folded into the next warning. +1 test. |
| `c0c33b41` | **C+D translation contract** (backfill AND tail share it now). (C1) rows are ordered and watermark-keyed by state.db `id` — ground-truth order — never by `timestamp`; (C3) compaction rows (marker prefix or the `_compressed_summary` column, role-insensitive) emit as `system kind=compaction`, never user; (C2) a backfill user row is skipped iff an identical user echo was appended by the WS send within 120s (exact-content idempotence, UTC-safe ts parse); (D) every assistant `tool_calls` row now emits BOTH `start` (with args) and `finish` (with result) — pre-fix, `start` only existed on live events, so post-restart replay showed tool calls finishing without ever starting (visible in the first live fixture: `tool_finish` with no `tool_start`); (D) persisted reasoning (1929 of 5646 assistant rows in this session's state.db carry the `reasoning` column) emits `system kind=reasoning`. The tail also keeps its content-dedupe fallback. +14 contract tests (hermetic dicts). |
| `556cd615` | **E+iOS** — `StreamHello.isHelloEnvelope` (type-first, `kind`-tolerant) replaces the `kind`-only check: banner reaches Connected on the real envelope and the hello no longer feeds the seq-cursor (kills the reconnect churn). Tests fixtures = VERBATIM live-captured API frames (plain replay + a full tool turn); `ws_decode_check.sh` compiles the REAL `SessionEvent` decoder + `isHelloEnvelope` and asserts the decode shape of both fixtures. `project.yml` registers StreamHello.swift (the project lists sources explicitly — an unlisted file silently does not build). |
| `0f5a10ae` | **E** — server-restart rebase: the store is in-memory, so an API restart renumbers seq from 1; a persisted cursor above the new boundary would `.duplicate` EVERY later frame (Connected + silent forever — the "no replies ever come" failure, second variant). `cursorRebaseAfterHello` (pure, StreamHello.swift) detects the rewind from the hello's `next_seq`, rebases to 0 and surfaces an honest notice. +11 assertions in `reconnect_check` (8 scenarios / 42 assertions). |
| `556cd615` | **F** — `scripts/api_smoke.py`: drives the REAL protocol against a live API (hello -> send -> user echo -> working(via=tmux) -> assistant reply), asserts order, single echo, non-empty reply, exits non-zero on any gap. Refuses protected projects. |
| `10e0a533` | packaging fix — the two frame fixtures committed with `556cd615` never entered the tree (repo-wide `*.jsonl` ignore swallowed them: the decode check "passed" on ZERO files in a fresh clone). Negation placed after `*.jsonl` (last match wins), fixtures tracked, `git check-ignore` proven. |

---

## 3. Harness output (F) — verbatim, final tip

Throwaway world (per contract, NEVER `hscc` = this REPL, NEVER `ecofire-bc`):

```
$ tmux new-session -d -s smoke-pom "hermes -p pom-orch chat --continue pom"
$ # API from this branch on 127.0.0.1:8799, hscc_dir=~/.hscc-smoke (own token,
$ # own registry: project pom -> profile pom-orch), serve startup patched OFF
$ # (exercises exactly the degraded path the live box is in).
$ python scripts/api_smoke.py pom --url ws://127.0.0.1:8799/v1/projects/pom/session/ws \
      --hscc-dir ~/.hscc-smoke --timeout 240
```

```
ok hello: next_seq=12
sent: Reply with exactly one word: SMOKE 31823. No tools, no punct…
ok echo: seq=12
ok working: via=tmux pane=smoke-pom:0.0
ok reply: seq=15 ts=2026-10-13T22:50:37Z
ok order+dedupe: 4 frames, single user echo
PASS: full round trip in 15.9s (echo -> via=tmux -> assistant 'SMOKE 31823', in-order, no dupes)
```

(Earlier run on the same world before the iOS commits: `PASS ... in 17.8s`,
next_seq=1.) The run proves the FULL read path — user echo appended by the
WS send, the tmux-reachability working frame, the REPL's reply lifted from
`state.db` by the DEGRADED (no-serve) store-tail, in order, exactly one echo.

## 4. Gates

* **Full suite, both interpreters, at the final tip**: `ALL GREEN`
  (`PY311_RC=0`, `P313_RC=0`) — clean-env recipe
  (`env -u CONDA_SHLVL -u CONDA_DEFAULT_ENV -u CONDA_PROMPT_MODIFIER -u CONDA_PREFIX`).
* **Address guard**: clean on every staged diff (public repo), incl. the two
  frame fixtures (scanned for tailnet/LAN literals and token shapes before
  commit).
* **iOS compile**: `scripts/build_check.sh` — HSCC 81 files + 4 widget/live-
  activity targets, **0 errors / 0 warnings** (compile+link only; never run
  on a device, per contract).
* **ws_decode_check**: both real-frame fixtures decode through the shipped
  parser (`12-frame turn: hello, 2 user, 2 assistant, 2 working, 1 tool_start,
  1 tool_finish, 3 reasoning`; seqs strictly increasing; no known type
  decoding as `.unknown`).
* **reconnect_check**: 8 scenarios / 42 assertions PASS incl. the new
  server-restart rebase and the hello-field regression.

## 5. Honest limits — what Hermes persists and what it cannot

* **Reasoning**: persisted per assistant row in `state.db` (`reasoning`
  column; 1929 of 5646 assistant rows in this session carry it). It is the
  model's REASONING SUMMARY at row-finalization time — not token-by-token
  live deltas. So reasoning reaches the app once per assistant row, complete,
  via `system kind=reasoning`; there is nothing persisted to replay for turns
  that streamed no reasoning.
* **Tool calls**: `tool_calls` JSON on the assistant row (name/args) + the
  tool result row (content + `tool_name`) — both sides now emitted (start +
  finish). In-flight "running" state between start and finish is not
  persisted; it is only observable live.
* **Message content**: complete, ordered by `id`; `api_content` holds provider
  payloads; compaction summaries are stored as messages (now rendered as
  system `compaction`, never user).
* The degraded tail polls at 2.0s — reply latency in the app is
  poll-interval + model latency, not streaming-token latency. True token
  streaming requires the native serve path (item B heal) to be healthy; with
  the heal, mounts should succeed after the operator's restart and tokens
  then arrive as live `delta` frames.

## 6. What needs the operator's phone (morning test list)

Precondition: release this branch (API restart heals/adopts serve per B),
rebuild the app from the branch, keep tmux REPL `0` running.

1. Open app -> project **hscc** chat. Expect: banner **"Live" within ~5s**
   (was stuck on "Connecting to the live stream..."), no reconnect churn in
   `~/.hscc/logs/api.start.log` from the phone IP.
2. Send `ping`. Expect: your message appears ONCE, a "working..." banner,
   then this REPL's reply in the app (<= 2 min). One `via: tmux` working
   frame in the timeline (system chip).
3. While I am mid-turn, send a second message. Expect: queued/`/queue`
   behavior, then BOTH replies arrive, no duplicate user rows.
4. In tmux, open a SECOND hscc-orch chat in a new window
   (`tmux new-window 'hermes -p hscc-orch chat --continue hscc'`) and send
   from the phone. Expect: refusal with the ambiguity explanation; close that
   window -> next send works again.
5. Restart the API (`hscc api restart` or kill its PID; supervisor relaunches).
   Expect: app reconnects, shows at most one "restarted its event numbering"
   notice, and the next `ping` still round-trips (this is the rebase path —
   the pre-fix behavior was a silently dead stream).
6. Ask: "run `echo TOOLTEST` with the terminal tool, then reply PONG".
   Expect: a tool-call row (name `terminal`, args, then result) BEFORE the
   PONG reply row, in order.
7. Optional (only if the API is healthy): with serve adopted, messages from
   the phone should show token deltas (typing effect) rather than one-shot
   rows.

## 7. Not done / follow-ups

* Native serve path live-verification: the token-adoption heal was proven by
  unit tests + a local reproduction of the 403, but NOT exercised live (the
  operator's API was not restarted). First operator API restart runs it for
  real; harness in §3 can then be re-run without the serve-off patch.
* Reasoning/tool rendering polish in-app: reasoning currently renders as a
  system chip (kind + details), tool rows already render structured. A
  dedicated collapsible reasoning row is a small follow-up card.
* `api_smoke` currently targets the standalone-throwaway flow; wiring a
  `hscc api smoke <project>` subcommand (daemon-owned launch) is a follow-up.
* The smoke world (tmux `smoke-pom`, ~/.hscc-smoke, pom-orch smoke sessions)
  is torn down by me at end of shift; recreate per §3 commands if wanted.
* Root cause of WHY the old API crashed/leaked serve at ~15:21Z (the
  original orphan birth) not chased — B makes it self-healing either way.
