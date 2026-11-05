# Incident 2026-11-05 — model streams silent while every health check is green

- Card: t_22b546e3 (hscc board) · Author: backend-engineer · Status: **IN PROGRESS** (evidence-first draft, committed early per card process)
- Scope: locate the silent layer among client (hermes stream reader) / litellm proxy / sparkrun_balancer in-flight accounting / vLLM engines. Fixes are out of scope; fix proposals per layer only.
- All real hosts redacted to documented placeholders: LAN engines `10.0.0.244/.246/.247/.248`, gateway Mac `10.0.0.245`. No tokens printed anywhere in this file.

## 1. Timeline (log-verified)

Proxy log = `~/.cache/sparkrun/proxy/litellm.log` (local to the gateway Mac; all 408s in it are *proxy→engine* read-timeouts, logged with NO upstream host). Client log = `~/.hermes/profiles/backend-engineer/logs/agent.log`.

| Time (local) | Source | Event |
|---|---|---|
| 05:58:33–06:13:36 | proxy | 6× `litellm.Timeout: APITimeoutError` → 408 to client (mid-stream read-timeout tracebacks) |
| 06:39:02–06:44:41 | proxy | 11× 408 (burst, self-triggered — no operator load at this time) |
| 08:24:28, 08:25:38 | proxy | 2× 408 |
| 09:25:32–09:26:51 | proxy | 3× 408 |
| 09:33:10 | client | backend-engineer worker: `Stream stale for 900s — no chunks received` (ctx ~72.9K) |
| 09:57:04 | proxy | 1× 408 |
| 10:02:02–10:02:04 | client | **4 simultaneous** stale-900s kills (sessions `7227f8`,`173fa3`,`52aae9`,`158059`; ctx 59–100K) |
| ~10:12–10:17 | operator | load burst (~13 injects + 2 /compress across orchs) — card context |
| ~10:16 | operator | fleet-wide stream silence begins (workers + orchestrators); health green |
| 10:25:57 | proxy | 1× 408 — ~9–10 min after the silence began (see §4: consistent with a 600–900s upstream read-timeout expiring on streams that went silent at ~10:12–10:16) |
| 10:29:17 | client | worker `173fa3` stale-900s kill (ctx ~75K); retry completed in ~68s; failed call logged `latency=967.9s` |
| 10:32:09–10:32:10 | client | **4 simultaneous** stale-900s kills (`ff9b3d`,`158059`,`f02e6f`,`c66cad`) — **exactly 900s after requests launched ~10:17**, matching streams that went silent at ~10:16–10:17 |
| 10:34–10:39 | client | F5, app worker, hscc-orch first completed calls (operator-reported recovery) |
| 10:36:07 | client | worker `f02e6f` API call completes in 90.9s at in=109K — engine healthy post-recovery |
| 10:57–11:57 | proxy | 408s continue (10:57:19/31/58, 11:27:03, 11:54:17, 11:56:01/07, 11:57:05) — the signature is chronic, not incident-specific |

Proxy error census (whole log, ANSI-stripped): 84 `LiteLLM Proxy:ERROR` blocks, ~all `proxy_server.py:5605 async_data_generator ... litellm.Timeout`; by hour: 06h:17, 01h:6, 03h:9, 04h:14, 05h:7, 08h:2, 09h:4, 10h:4, 11h:5 (+previous days). Each 408 appears twice in some cases (proxy also logs `_handle_llm_api_exception`).

## 2. Per-layer evidence

### 2.1 vLLM engines — NOT globally wedged (positive evidence)

- From the gateway, `GET /metrics` on all four engines responds and counters advance under normal traffic (watch window in progress; snapshot method: `metrics_watch.py`, 10s steps).
- Current live snapshot (post-incident): running=1–3, waiting=0 on all four; `num_preemptions_total` nonzero on every engine (103–299).
- `run=0` with frozen counters during the incident is **consistent with idle keepalives**: current `lsof -nP -p <litellm pid>` shows 1–3 long-lived ESTABLISHED sockets per engine with no request in flight — the pool keeps sockets open. The "4 and 7 sockets" observation is therefore NOT proof of wedged streams (operator note confirmed as likely-idle).
- Decisive argument: during the silence NEW small requests completed fine and /health stayed green; a globally wedged engine could not serve those. If an engine wedge existed it would have to be *per-stream*, which vLLM's asyncio model does not naturally produce (§4 hypothesis N3).

### 2.2 sparkrun_balancer — chronic 408s do not need it; leak hypothesis weakened but not excluded

Deployed copy: `~/.cache/sparkrun/proxy/sparkrun_balancer.py` (440 lines; READ-ONLY). Upstream `sparkrun/src/sparkrun/plugins/litellm/balancer.py` (325 lines) has **DRIFT**: the deployed file adds session-affinity (rendezvous hash on `session_key()` = explicit `x-litellm-session-id` else hash of system-prompt head + first user message), `AFFINITY_SLACK=2`, pick stats. The affinity tier exists only in the operator's newer local fork.

Key facts read from the deployed file:
- `STALE_AFTER_S = 900.0` — reservations self-expire at 900s (balancer.py:89, :270–279).
- `async_post_call_streaming_iterator_hook` (:415–425) releases only on `GeneratorExit`/`CancelledError`. A stream that neither errors nor closes is covered by the 900s expiry, so **a permanent in-flight leak is not possible**; worst case is a 900s phantom-load window per hung stream.
- Line ~421 (`async for chunk in response: yield chunk`) is NOT the emitter of the "408 Timeout on reading data from socket" log line — those tracebacks originate in litellm's `aiohttp_transport.py` + `streaming_handler.py` and surface from `proxy_server.py:5605 async_data_generator` (see the log tracebacks verbatim in §5). The card's hypothesis "line 421 logs the 408" is wrong; the balancer merely *sees* the exception propagate.
- Consequence for LeastInFlight: with a 900s expiry and `FAULT_AVOID_S=10`, a hung stream inflates a backend's in-flight count for ≤900s — that makes LeastInFlight *avoid* the wedged backend longer (opposite of re-picking it), unless the stream keeps producing fake "in flight" reads. So the "re-picks wedged nodes while green" mechanism as written is **not supported**.

### 2.3 litellm proxy — chronic mid-stream read-timeout; suspected event-loop-level stall during the incident window (investigating)

- All proxy errors are of one shape (excerpt trimmed in §5): `aiohttp SocketTimeoutError: Timeout on reading data from socket` → `httpx.ReadTimeout` → `litellm.Timeout: APITimeoutError` → client sees `POST /v1/chat/completions 408`. This is litellm's **upstream read timeout** firing after N seconds of silence on an engine socket (N = the configured client timeout; to be pinned from sparkrun engine.py — pending).
- **Concentration clue**: the 4 stale-kills at 10:02:02–04 and the 4 at 10:32:09–10 land within ~1–2s of each other across DIFFERENT sessions on DIFFERENT engines. Independent engine wedges do not synchronize; a shared upstream event does. The 900s stale window means "silent since T" with T≈09:47 and T≈10:17 respectively — two synchronized fleet-wide silence onset points.
- New small requests + /health stayed green during 10:16–10:34 → the proxy loop was NOT fully blocked (it served those). Hypothesis under test: partial stall (loop starvation / connection-level network flap at the gateway Mac) that silently black-holes *existing* upstream sockets while new connections still work.

### 2.4 client (hermes) — behaves correctly, with one blind spot

- `agent/chat_completion_helpers.py:3587` kills after 900s of zero chunks (threshold `get_provider_stale_timeout`). The kills are the *detection*, not the disease.
- `agent.stream_diag` shows the failure shape at recovery: streams that HAD delivered chunks (up to 1969 chunks / 98KB over 1268s, ttfb ~5–6s) then died with `RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)` `http_status=200 upstream=[server=uvicorn]`. i.e. the proxy closed the HTTP response without a terminating chunk — the proxy-side stream died (read-timeout path) and litellm ended the body un-cleanly.
- Blind spot for the watchdog design (§6): a chunked-200 that stops mid-body is invisible to health checks AND to status-code monitoring; only chunk-progress detection (what hermes already does) sees it.

## 3. Reproduction recipe (WIP — validated part first)

Safe observation probe (read-only, no fleet risk):
1. `sparkrun status` — all 4 solo containers Up (expected).
2. `GET http://10.0.0.{244,246,247,248}:{8000,8003,8001,8002}/metrics` twice, 10s apart — `generation_tokens_total` must advance under traffic; frozen counters + `run>0` for >10min = real engine wedge signature.
3. `lsof -nP -p <proxy pid> | grep 192.168.88` — long-lived engine sockets with no matching in-flight request are pool keepalives, NOT hung streams (calibrated: 1–3/host at idle).
4. Proxy 408 census one-liner (counts the failure signature per hour):
   `grep -a "LiteLLM Proxy:ERROR" ~/.cache/sparkrun/proxy/litellm.log | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-5 | uniq -c`

Trigger recipe: pending — see §4/§7 (the chronic 408s self-trigger ~hourly; the goal is to catch one live with per-socket evidence rather than to force a fleet-wide event).

## 4. Narrowing (working hypotheses)

- N1 (weakened): balancer in-flight leak keeps re-picking wedged nodes. Weakened by 900s expiry (no permanent leak) and because phantom load *repels* LeastInFlight. Not excluded for the *affinity* tier: a session pinned (slack=2) to a half-dead backend could re-stick to it while 3 idle backends sit at 0 — affinity can mask least-in-flight exactly when other nodes are green. Probe: `balancer_stats.json` choiceN skew.
- N2 (alive): gateway-Mac network flap at ~09:47 and ~10:17 silently black-holes existing TCP streams (engines + clients), new connections fine. Explains synchronized multi-engine silence + green health + green new requests. Probe: `powermetrics`/`syslog`/`dmesg`-equivalents on the Mac, IOKe network events; litellm 408 cluster timestamps after each flap.
- N3 (mostly excluded): per-stream engine wedge — see §2.1.
- N4 (chronic, separate): long reasoning-model decode gaps exceed litellm's upstream read timeout → hourly 408s even without any incident (05:58 etc. with no load). This is the pre-incident signature; it kills long-thinking streams but is NOT the 18–23min event.

## 5. Log excerpts (verbatim, redacted)

(proxy traceback excerpt — appended below as evidence lands)

## 6. Watchdog implications

(to be written from findings)

## 7. Next probes (for continuation)

1. Pin litellm upstream read-timeout from sparkrun engine.py config generation; confirm it explains 408-delay-after-silence (~9–10min at 10:25:57).
2. Compare `balancer_stats.json` choiceN skew vs per-session stickiness for the incident sessions.
3. Check gateway Mac system logs for network interface flap / sleep events 09:45–09:50 and 10:15–10:20.
4. hscc-orch + other profiles' agent.log: reconstruct per-session silence-onset to time the flaps precisely.
5. stream_diag across all profiles for 09:40–10:40 to count how many streams died `incomplete chunked read` (proxy-side deaths) vs silent-zero-chunk (client kills).
