# t_e1cfda68 — Node stream ownership & ws relay / Rust panic root cause

Task: "Investigate node stream ownership and root cause in hscc_daemon ... determine
whether the node stream is owned by settlng or by hscc. Identify the exact code paths
for the ws relay read error and Rust panics. Document findings with file:line references
and relevant code snippets. Do not modify any code."

Status: COMPLETE — investigation concluded. No code modified. The operator resolved the
underlying incident (macOS Local Network / TCC) and closed the root card t_9e66d919; this
report records the ownership determination + root cause and points to the authoritative
audit.

## 1. Ownership determination: the node stream is NOT owned by "settlng" nor by hscc

Verdict: neither component owns the failing node stream.

- "settlng" is not a real component anywhere in this repo. `git grep -ni settlng` /
  `grep -rni settlng` across /Users/desac/dev/hscc (excluding worktrees) returns EMPTY.
  (Strictly the only near-hit is a test helper named `_settle_until_client_gone` in
  hscc_daemon/tests/test_engine_wedge.py:356,377,381 — unrelated test scaffolding, not a
  stream owner.) The name appears to be a corruption of "settling" / "session" from the
  upstream task description; no such owner exists.
- hscc owns no "node stream". The file the task names — `hscc_daemon/api/routes_ws.py` —
  does NOT exist: there is no `hscc_daemon/api/` directory (hscc_daemon/ has no api/
  subpackage; confirmed by `ls hscc_daemon`). The real, only file is `hscc-api/routes_ws.py`
  (504 lines), which is the **session** WebSocket relay for the iOS app live event stream —
  not a "node stream".
- grep for "node stream" / "node_stream" across the hscc repo is EMPTY. There is no node
  stream component in hscc.
- The actual live symptom being investigated (intermittent ws relay read errors, Rust
  panics, endpoints scaled up/down server-side, node/health streams failing) was diagnosed
  by the operator and documented in `docs/audits/stream-flow-tcc-t_fc2e955d.md` (see §3).

So the premise "is the node stream owned by settlng or by hscc" is false in both
alternatives: there is no settlng, and hscc has no node stream. The subject of the
upstream card (ws relay read error + Rust panics) lives in the upstream hermes-agent
serving/streaming stack, not in this repo.

## 2. The code paths the task points at (for completeness — hscc's own WS relay)

Even though hscc's session WS relay is NOT the failing node stream, the card asks for the
"ws relay read error" and "Rust panics" code paths. Here they are, traced through the two
hscc modules that implement the WS relay, to make clear where hscc's relay is healthy and
separate:

### 2a. Server side — hscc-api/routes_ws.py (the iOS session relay)
Read path for inbound client frames (peer read error surfaces here):
- `_drain_socket` hscc-api/routes_ws.py:453-497 — `select.select` (460); `sock.recv(65536)`
  (466); on `OSError`/`socket.timeout` returns False -> stream ends (467-468); on empty
  data (peer closed TCP) returns False (469-470). This is the exact "ws relay read error"
  location in hscc code. It is fail-close and per-connection: a read error tears down that
  one socket (`_live_loop` returns at 439 -> `handle_session_ws` finally closes at 390-394),
  never the whole daemon.
- Send side (writer thread is the single sender per `_live_loop` docstring 414-420):
  `_send_text` 86-89 / `_send_event` 100-102 / `_send_hello` 91-97.
- Relay of operator messages OUT to the orchestrator: `_default_relay` 139-219 runs on a
  background thread (218), posts via `routes_orchestrator._run_job` (191), folds the reply
  into the store (193-209). Read errors here are caught broadly (`except Exception` 210) and
  surfaced as a `TYPE_ERROR` "relay_failed" frame (214-216) — never a crash.

There is no Rust in hscc-api/routes_ws.py; it is pure-stdlib Python (RFC 6455 via
ws_frame / ws_frame client-side framing in gateway_driver.py). So hscc's WS relay cannot
produce a "Rust panic".

### 2b. Client (upstream) side — hscc-api/gateway_driver.py (the hermes serve relay)
This module connects hscc OUT to `hermes serve` (the upstream Rust gateway) and translates
its native event feed into the store. Its read-error paths:
- `_WSClient._read_exact` gateway_driver.py:175-190 — `sock.recv` (180); empty data raises
  `ConnectionError("upstream closed during read")` (187-188); mid-frame timeout raises
  (185).
- `_WSClient._read_one_data_frame` 290-322 — frame reassembly, raises WSProtocolError on
  masked server frames (296) / oversized payload (302-303).
- `_run_events_loop` 635-659 — `read_frame()` (646); `socket.timeout` treated as keep-alive
  not disconnect (647-648); `ConnectionError` -> logged `"gateway: events feed
  disconnected"` (656-657) then `flush()` (659).
- `_run_pty_loop` 661-677 — `read_frame()` (672); `ConnectionError` -> logged
  `"gateway: pty feed closed"` (676-677).

These are the two "ws relay read error" code paths in hscc: the server-side sock.recv in
routes_ws.py:466 and the client-side upstream recv in gateway_driver.py:180 / 652 / 672.
All are caught and logged; none crash the process.

### 2c. Where a "Rust panic" would surface
`hermes serve` is the Rust binary. A panic in it kills the upstream server's handler,
which the client observes ONLY as an abrupt upstream disconnect (ConnectionError / empty
recv) — i.e. it lands at gateway_driver.py:656-657 ("events feed disconnected") or
676-677 ("pty feed closed"). hscc does not host or run that Rust code and has no panic path
of its own; the panic lives upstream in hermes-agent serving/streaming.

## 3. Root cause (authoritative — operator-resolved, see docs/audits/stream-flow-tcc-t_fc2e955d.md)

The failing node/health streams and the apparent "read errors" on live were NOT an hscc
code defect. Root cause: macOS **Local Network (TCC) privacy** denied LAN access to the
python interpreters the daemon runs under (launchd-run
`~/.hermes/hermes-agent/venv/bin/python`, real path
`~/.local/share/uv/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11`), so every
LAN `connect()` returned errno 65 "No route to host" while the cluster stayed healthy and
reachable from other processes (Terminal, which already held the same grant).

Decisive evidence (full detail in the audit):
- Same host, different process (Terminal / Apple-signed python3.9): `nc -vz <GATEWAY_IP> 8000`
  succeeds; ping 0% loss; socket.create_connection CONNECTED.
- The two interpreters hscc actually uses (hermes venv 3.11.16 ad-hoc; miniconda p313
  3.13.12): TCP 8000/22/443 -> errno 65 (urllib AND raw socket both fail).
- LAN-scoped not process-wide: same blocked interpreter reaches 1.1.1.1:53 CONNECTED,
  8.8.8.8:53 CONNECTED, <GATEWAY_IP>:8000 errno 65. This internet-OK/LAN-blocked /
  EHOSTUNREACH signature is the documented macOS Local Network privacy denial.

(Addresses redacted to placeholders per the public-repo rule.)

## 4. Recommendation & acceptance criteria

Recommendation: **No hscc code change. Do NOT file upstream** — the root cause was not an
upstream Rust/hermes-agent bug; it was a macOS TCC permission denial on the operator's
machine. The operational fix is a user/system action:

    System Settings > Privacy & Security > Local Network
      -> enable for the hermes-agent venv python3.11 and miniconda python3.13
    (or `tccutil reset LocalNetwork` and answer "Allow", or re-enable the denials).
    Then restart the daemon.

The operator has already begun granting access; dgx/gateway/watchdog streams went GREEN
as a result. The watchdog's auto-restart loop that fought the ghost failure stops once the
interpreters regain LAN access.

Acceptance criteria for "fixed":
- All node/health streams (gateway, dgx, watchdog) report GREEN after a daemon restart
  with Local Network granted.
- No errno 65 "No route to host" in daemon state files/logs for LAN targets.
- hscc-api/routes_ws.py session relay unaffected (it is a separate, healthy component).

## 5. What was NOT done (per task instruction + operator direction)
- No code modified anywhere in hscc (task: "Do not modify any code").
- No live mutations / no further probing of the live gateway ("run against live only if
  safe"; operator: "Do not proceed").
- No fabricated evidence: every file:line above is read from the current hscc checkout.

## References
- docs/audits/stream-flow-tcc-t_fc2e955d.md  — operator-authored root-cause audit (TCC)
- hscc-api/routes_ws.py                      — hscc session WS relay (server side)
- hscc-api/gateway_driver.py                 — hscc -> hermes serve relay (client side)
- Root card t_9e66d919 (closed by operator)  — upstream card this child was decomposed from
