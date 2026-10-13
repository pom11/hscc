# iOS chat end-to-end — overnight report (2026-10-14)

Branch: `fix/ios-chat-e2e-2026-10-13` (from main `e9a2766c`, hscc 2.7.0 + hooks fix).
Scope: make iOS chat work both ways — app → REPL (already OK) **and** REPL →
app (broken). No release, no install, no daemon restart.

STATUS: IN PROGRESS (started 2026-10-13 21:57 EEST). This file is updated as
each commit lands; the "Morning summary" section is final.

## 1. What was broken (operator evidence, 21:55, hscc 2.7.0, API PID 45019)

Write path **works**: iOS msg 18:48:36Z → store seq 116 (user) + 117
(working, via=tmux); terminal REPL replied "received" 18:49:25Z in state.db
session `20260908_134933_152c5b`.

Read path **broken**, four distinct defects:

1. **Store-tail gated on serve mount** — `_run_store_tail_loop` is started
   only after `_connect_upstreams` succeeds (gateway_driver.py ~1136-1146),
   but every mount fails: 102x `gateway-bridge: mount driver for <project>
   failed: ConnectionError("upstream rejected WebSocket upgrade: HTTP/1.1
   403 Forbidden")` in `~/.hscc/logs/api.start.log`. The poller's own
   docstring says it does NOT need upstream sockets. Net: nothing the
   terminal REPL writes ever reaches the store/WS. [my verification: TBD]
2. **hermes serve 403 on WS upgrade** — port 9119 (PID 8958) rejects the
   upgrade. Root cause TBD (token/Origin/header/route change in Hermes
   0.2x?). [TBD]
3. **Store correctness** — frames not in ts order (seq with ts 14:50Z after
   18:10Z); seq 1 = compaction summary emitted as role=user; iOS user msg
   duplicated (seq 116 + seq 233). [TBD]
4. **App stream state** — banner stuck "Connecting to the live stream…"
   while socket open + frames arriving; repeated `disconnected mid-request
   (ConnectionResetError/BrokenPipeError)` from the phone. [TBD]

## 2. Commits (one concern each)

(none yet — appended as they land, each with what it fixes + how verified)

## 3. Harness output (`hscc api smoke`, item F)

TBD — run against THROWAWAY project + `hermes -p pom-orch chat` in a
detached tmux session. Never project "hscc" (this REPL) or "ecofire-bc".

## 4. iOS build + decode tests

TBD — compile+link on generic iOS only; Swift decode tests fed with REAL
captured frames (fixture from live capture).

## 5. Morning summary — operator phone test list

TBD (numbered: open app → project → send "ping" → expect …).

## 6. Not done / needs operator

TBD.
