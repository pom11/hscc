# Incident 2026-11-05 — model streams silent while every health check is green

- Card: t_22b546e3 (hscc board) · Author: backend-engineer · Status: root cause narrowed to a single layer with a dated, log-verified mechanism hypothesis
- Scope: locate the silent layer among client (hermes stream reader) / litellm proxy / sparkrun_balancer in-flight accounting / vLLM engines. Fixes are out of scope; fix proposals per layer only.
- Redaction: LAN engines `10.0.0.244/.246/.247/.248`, gateway Mac `10.0.0.245` (documented placeholders; real addresses deliberately omitted — this repo is public). No tokens anywhere.

## 0. Headline finding (READ THIS FIRST)

The silent layer is the **gateway Mac's LAN path (en0), on a DHCP-renew clock**. The fleet's
"18–23 min of stream silence with every health check green" is not an engine wedge and not a
balancer leak: long-lived in-flight streams go **silently dead at DHCP `en0: RENEW` boundary
marks** (the gateway leases its LAN address with a 1800 s lease → macOS renews every ~899 s,
at :02/:17/:32/:47 each hour). The stream never errors — the client's 900 s stale-kill and the
proxy's 600 s upstream read-timeout are what finally surface it, which is exactly why the
outage "lasts" 15+ minutes and why every health check stays green (new connections and `/health`
are unaffected). Evidence in §2.5; the per-layer verdicts in §2.1–2.4 refute the four candidate
layers on the card.

## 1. Timeline (log-verified)

Sources: proxy = `~/.cache/sparkrun/proxy/litellm.log` (gateway-local; the 408s are proxy→engine
read-timeouts, no upstream host printed). Clients = `~/.hermes/profiles/*/logs/agent.log`.
Engines = `sparkrun logs <job>` (vLLM APIserver logs). DHCP = `log show --predicate 'process=="configd"'`.

| Time (local) | Source | Event |
|---|---|---|
| 05:14–05:19 | clients | first fleet-wide stale cluster today (7 profiles) |
| 05:32:56, 05:47:55, 06:02:54, … | configd | `DHCP en0: RENEW` every ~899 s (lease 0x708 = 1800 s) |
| 05:58:33–06:13:36 | proxy | 6× `litellm.Timeout: APITimeoutError` → 408 (05:58 = 05:47:55 RENEW + ~638 s) |
| 06:39:02–06:44:41 | proxy | 11× 408 burst (RENEW 06:32:52 + 370–709 s) — self-triggered, no operator load |
| 07:02:00–07:02:04 | clients | **8 simultaneous** stale-kills across 6 profiles (onset 06:47 ≈ RENEW 06:47:51) |
| 07:29:47 | clients | 9 simultaneous kills (≈ RENEW 07:17:49) |
| 08:01:52–08:01:55 | clients | 10 simultaneous kills (≈ RENEW 07:47:47) |
| 08:24:28, 08:25:38 | proxy | 2× 408 (RENEW 08:17:45 + 385/473 s) |
| 09:25:32–09:26:51 | proxy | 3× 408 (RENEW 09:17:41 + 471 s) |
| 09:32:05–09:33:10 | clients | 15 kills, 5 profiles (onset 09:17 ≈ RENEW 09:17:41) |
| 09:57:04 | proxy | 1× 408 (≈ RENEW 09:47 + 600 s) |
| 10:02:02–10:02:04 | clients | 12 kills, 5 profiles (onset 09:47 ≈ RENEW 09:47:39) |
| ~10:12–10:17 | operator | load burst (13 injects + 2 /compress) — red herring, see §5 |
| ~10:16 | operator | perceived fleet silence begins (streams launched pre-RENEW die at RENEW 10:17:37) |
| 10:25:57 | proxy | 1× 408 (RENEW 10:17:37 + 500 s) |
| 10:29:17 | client | worker `173fa3` stale-kill; retry completes in ~68 s; failed call `latency=967.9s` |
| 10:32:09–10:32:10 | clients | **14 kills, 5 profiles incl. hscc-orch** — exactly 900 s after RENEW 10:17:37 |
| 10:32:03–10:32:43 | engines | POST completions cluster on .244/.246 — the half-dead streams finally unbind when clients kill them |
| 10:34–10:39 | clients | F5, app worker, hscc-orch first completed calls (retries launched after the kill land fine) |
| 10:57:19–10:58:08 | proxy | 3× 408 (RENEW 10:47:35 + 600 s) |
| 11:02:02 | clients | 3 stale-kills (onset ≈ RENEW 10:47) |
| 11:27:03 / 11:54–11:57 / 12:57:21 | proxy | further 408 bursts, each ≈ RENEW(:47) + 600 s |
| 12:32:05 | clients | 25 kills, 5 profiles (onset 12:17 ≈ RENEW 12:17:29) |

Proxy error census: 84 `LiteLLM Proxy:ERROR` blocks in the current log, ~all
`proxy_server.py:5605 async_data_generator ... litellm.Timeout` (one shape, §5.1). Client
stale-kill census (05:00–14:00): ~90 kills, clustered in bursts of 3–25 landing at
`RENEW-mark + 900 s` ± 35 s. In contrast, rotated logs from Aug–Oct show this kill pattern
**does not exist** (3 kills total in the sampled older logs) — the RENEW cadence (1800 s lease)
is new; see §6 "why now".

## 2. Per-layer verdicts

### 2.1 vLLM engines — CLEAN (negative evidence, positive counters)

- `sparkrun status`: 4/4 solo containers Up 7–8 days; no restarts.
- `/metrics` sampled every 10 s over 152 s (script kept at
  `~/.hermes/profiles/backend-engineer/cache/scratch/metrics_watch.py`, output archived): all four
  engines advance `generation_tokens_total` (20–90 tok/s aggregate), `running=1–5`, `waiting=0`.
- Engine 10 s heartbeats across the whole incident: engines kept serving **new** requests through
  the silence windows (`POST completed 200` lines every ~10–60 s at 10:05–10:40, 60–95 per engine).
- During silence windows the engines show the victim requests as `Running: N` with ~0 throughput —
  i.e. the engine-side socket to the proxy had gone dead-without-RST too; the POST only logs its
  completion when the client stale-kill tears the chain (cluster at 10:32:03–10:32:43).
- `run=0` + frozen counters during the incident = idle keepalives, as the operator suspected:
  live `lsof` on the proxy pid shows 1–3 long-lived ESTABLISHED sockets per engine at idle.

### 2.2 sparkrun_balancer — CLEAN as cause (card hypothesis refuted; one bounded real issue)

Deployed copy `~/.cache/sparkrun/proxy/sparkrun_balancer.py` (440 lines) **DRIFTS from**
`sparkrun/src/sparkrun/plugins/litellm/balancer.py` (325 lines): the deployed file carries the
operator fork's session-affinity tier (`session_key()` = explicit `x-litellm-session-id` else
blake2b(system-prompt-head+first-user), `AFFINITY_SLACK=2`, rendezvous hash, `pick_stats`).

- The card's premise "line ~421 logs the 408" is **wrong**: every 408 traceback originates in
  litellm's `aiohttp_transport.py`/`streaming_handler.py` and surfaces at
  `proxy_server.py:5605 async_data_generator`; balancer.py:421 (`async for chunk in response:
  yield chunk`) merely lets the exception propagate.
- No permanent in-flight leak is possible: `STALE_AFTER_S=900` (`_expire_locked`) reaps any
  reservation a hung stream never released; `balancer_stats.json` only writes cumulative pick
  counts (no in-flight field to drift).
- The "LeastInFlight keeps re-picking wedged nodes" mechanism is inverted: a phantom in-flight
  entry makes LeastInFlight *avoid* that backend for ≤900 s, not prefer it.
- Real-but-bounded: a silently-dead stream holds its slot + affinity for up to 900 s (worst case
  misplaces ~6 concurrent long streams onto other engines per RENEW event). Fix proposal in §7.3.

### 2.3 litellm proxy — faithful messenger, but 600 s too slow and silent

- One error shape only (§5.1): `aiohttp SocketTimeoutError` → `httpx.ReadTimeout` →
  `litellm.Timeout: APITimeoutError` → client sees `POST /v1/chat/completions 408` (or a truncated
  chunked body under a 200 — the shape hermes `stream_diag` caught at 10:32:09:
  `RemoteProtocolError: peer closed connection without sending complete message body
  (incomplete chunked read) http_status=200 upstream=[server=uvicorn]`).
- Upstream read timeout = litellm default **600 s** (`litellm/main.py:1403
  timeout = timeout or kwargs.get("request_timeout", 600) or 600`; no `request_timeout`/
  `REQUEST_TIMEOUT` set in `litellm_config.yaml` or the proxy env). Consequence: after a stream
  dies silently the proxy waits 600 s before emitting anything — the 408 bursts land at
  `RENEW + ≤600 s` throughout the day (§1). Not root cause, but the reason the fleet sits blind
  for 10 minutes per event.
- litellm 1.82.6 pinned via `uvx --from litellm[proxy]==1.82.6` (state.yaml pid 91271, running since Nov 3 04:49 — proxy did **not** restart around the incident; config files untouched since Nov 3 06:49).

### 2.4 client (hermes) — CLEAN; detection is correct, one blind spot worth keeping

- `agent/chat_completion_helpers.py:3587` stale-kill at 900 s of zero chunks is what *found* the
  problem; the kills happen in synchronized bursts because the disease is shared, not the client.
- `agent.stream_diag` (10:32:09–10:32:10 samples) proves the streams had streamed normally
  (chunks=203–1969, bytes 10–99 KB, ttfb 4.7–6.2 s) and died mid-body — i.e. **silent death after
  a healthy start**, not a connect/stall-start failure.
- Retry behavior confirms recovery ≠ healing: every retry after a kill completed in 60–120 s
  because it launched *after* the RENEW mark (fresh connections are never affected).

### 2.5 The silent layer: gateway en0 on its DHCP-renew clock (POSITIVE evidence)

`ipconfig getpacket en0`: `lease_time = 0x708 = 1800 s` → macOS `configd` renews at T1=½lease,
measured every ~899.0 s: `DHCP en0: RENEW` at 05:32:56, 05:47:55, 06:02:54, 06:17:53, 06:32:52,
06:47:51, 07:02:50, 07:17:49, 07:32:48, 07:47:47, 08:02:46, 08:17:45, 08:32:44, 08:47:43,
09:17:41, 10:17:37 (each ~1 s shorter each cycle), 11:32:32, 11:47:31, 12:02:30, 12:17:29,
12:32:28, 12:47:27 — i.e. marks at :02/:17/:32/:47 each hour. (Sample lines carry
`RENEW/REBIND waiting at 0 for 337.000000` then `BOUND` within ~1 ms.)

Correlation (the whole point):

| Client kill burst | onset = kill − 900 s | RENEW mark | Δ |
|---|---|---|---|
| 07:02:00–04 (8 profiles' streams) | 06:47:0x | 06:47:51 | ≤ 49 s |
| 08:01:52–55 | 07:46:5x | 07:47:47 | ≤ 54 s |
| 09:32:05–09:33:10 | 09:17:0x | 09:17:41 | ≤ 36 s |
| 10:02:02–04 | 09:47:0x | ~09:47:39 | ≤ 37 s |
| 10:32:09–10 (14 kills) | 10:17:0x | 10:17:37 | ≤ 28 s |
| 11:02:02 | 10:47:0x | ~10:47:35 | ≤ 35 s |
| 12:32:05 (25 kills) | 12:17:0x | 12:17:29 | ≤ 24 s |

and every proxy 408 burst is a stream's 600 s sock_read timer expiring on a stream that died at
the preceding RENEW mark (§1). The kills are **synchronized across different sessions, different
engines, different profiles within 1–2 s** — independent engine wedges cannot do that; a shared
point on the gateway host can. New connections + /health stay green → the path is not "down";
**established flows** stop delivering bytes without FIN/RST (so neither endpoint errors until its
own read-timeout). The proxy↔engine hop is the only LAN path in the chain (client↔proxy is
localhost), which is exactly where the frozen counters of §2.1 were observed.

Why only every *second* RENEW was fatal in the observed window (the kills land on :02/:32 =
onsets :17/:47): unexplained with available logs — candidate explanations (ARP/forwarding-state
refresh period of the switch/AP at `10.0.0.1`, macOS re-publishing interface state only on
changed ACKs, `nw_path_libinfo_path_check` re-evaluation) are in the "next probe" list §7.5.
Half-cycle fatality does not weaken the correlation; it halves its frequency.

Not the machine sleeping: `kern.boottime` Oct 8, uptime 28 days, no `Sleep`/`Wake` events in
`pmset -g log` for Nov 5 (only `Assertions` noise). Not a proxy restart (state.yaml, file mtimes).
Not the load burst (§5).

## 3. Reproduction recipe (bounded — no live-traffic risk required)

**A. Safe OBSERVATION probe (catches every occurrence, read-only):**

1. `log show --style compact --start <T-2h> --end now --predicate 'process == "configd" AND
   eventMessage CONTAINS "DHCP en0"' | grep -E "RENEW|BOUND"` → note marks (:02/:17/:32/:47 pattern).
2. Cross-profile kill scan (script committed alongside this report's notes at
   `~/.hermes/profiles/backend-engineer/cache/scratch/stream_forensics.py`): stale-kills +
   `Stream drop` per minute. **Signature = bursts of ≥3 stale-kills at `mark + 900 s ± 40 s`
   across ≥2 profiles.**
3. Proxy 408 census: bursts at `mark + ≤600 s` (`grep -a "LiteLLM Proxy:ERROR" litellm.log |
   sed 's/\x1b\[[0-9;]*m//g' | cut -c1-5`).
4. `sparkrun logs <job> | grep "POST completed"` during a window: engines kept completing NEW
   requests (green) — the discriminator against an engine wedge.

**B. Deterministic trigger (ORCH/OPERATOR ONLY — mutates gateway networking, do NOT run from a worker):**
open one long streaming request against `http://localhost:4000/v1/chat/completions`
(`worker-model`, ~80K-token prompt, `stream: true`, `max_tokens: 2048`), then renew the lease:
`sudo ipconfig set en0 DHCP` (or `ipconfig set en0 MANUAL` + `DHCP`). Expected failure: stream
delivers normally then goes silent within seconds of the RENEW; no FIN/RST; client hermes kills at
900 s (or `curl` just hangs); proxy emits the §5.1 traceback ~600 s after the last chunk; other
sessions' concurrent streams die at the same instant. A cheaper variant with no root: hold the
same stream open across a :17/:47 mark and watch it die — zero config writes, still deterministic.

**C. Bounded canary (safe today):** the stream-silence watchdog canary from §6 — a 60 K-token
streaming request every :15 (between marks); if it goes silent it has caught a mark hit with 1
affected request instead of 14.

## 4. Why every health check stayed green

`/health` (vLLM) and proxy `:4000` answers are NEW short-lived connections; the fault only
breaks **established** flows, so: engine-wedge check 4/4, proxy 200 in 0.5 s, new small requests
fine — while every already-streaming agent is dead-in-the-water. This is the generalizable lesson:
connection-establishment health checks are blind to established-flow blackholes (see §6).

## 5. The load burst was a red herring

The burst (13 injects + 2 /compress at 10:12–10:17) merely maximized the population of
mid-stream requests at the 10:17:37 RENEW mark — the same shape recurs unattended at 07:02,
08:01, 09:32, 12:32 (and 408 bursts from 05:58 on). Pre-incident 408s (05:58–06:44) predate any
burst, as the card noted.

## 6. Watchdog implications (the parked daemon card)

Design guidance from this finding:
1. Detect **chunk-progress silence**, not call completion — a running card/orch with an open
   model call and zero chunks for > ~120–180 s while `:4000` answers `:4000/health` is the exact
   signature here; 900 s (hermes' own stale-kill) is 5–7× too slow to be a watchdog.
2. Make it **RENEW-correlated**: the daemon should stamp `configd DHCP en0: RENEW` marks (cheap
   `log show` scrape or `ipconfig getpacket` lease math) and *predict* exposure — if a stream's
   last chunk predates a mark by seconds, it is dead; no need to wait for any timeout.
3. Pair with an active canary: one short streaming request (few-KB prompt, `max_tokens=8`,
   `stream=true`) per ~5 min — new connections are unaffected by this fault class, so the canary
   covers the gap between marks, and a canary *failure* means the fault class changed (escalate
   differently: that would be a real wedge).
4. The watchdog must not conclude "engine wedge" from frozen `/metrics` counters — per §2.1 idle
   keepalives + half-dead streams produce exactly that; the discriminator is whether NEW small
   completions advance the counters (§2.1 probe, 2×10 s sampling).

## 7. Smallest fix proposals per layer (NOT implemented — card scope)

1. **Network/host (root-cause owner; operator network config, outside this repo)**: give the
   gateway a static lease / long lease (≥24 h) on the LAN router for `10.0.0.245`, or a static IP
   — removes the 899 s renew clock entirely. Verify per §3B afterwards. This is the smallest true
   fix if the correlation survives the tcpdump probe (§7.5).
2. **litellm proxy (sparkrun fork)**: set `request_timeout: 180` (or `sock_read_timeout`
   equivalent) for streaming deployments so a dead stream surfaces as 408 in ~3 min instead of
   10 min, and the client's retry loop recovers in minutes; pair with (3).
3. **sparkrun_balancer (fork)**: in `async_post_call_streaming_iterator_hook` track
   `last_chunk_at` and treat a chunk gap > proxy read-timeout/2 as a dead stream
   (`release_request(request_data, fault=True)` + raise) — converts silent death into a fast,
   attributable backend fault instead of a 900 s phantom slot.
4. **hermes client (cluster card)**: on a mid-stream silent death, retry with jitter — today's
   retries succeeded because they landed off-mark; a retry fired immediately at the kill could
   hit the same mark class again. A 30–60 s jitter before retry is one line.
5. **Next probe (mechanism completion, ORCH/OPERATOR)**: `sudo tcpdump -i en0 -w … host 10.0.0.244
   or host 10.0.0.246 or …` for 40 min spanning two marks with one canary stream open each cycle;
   the capture answers (a) does the death coincide with RENEW to the second, (b) is any direction
   black-holed (ARP? TCP keepalive? RST lost?), (c) why every second mark. If tcpdump shows the
   Mac still receiving engine bytes after the stall, the fault is inside the Mac (socket not
   drained) and shifts the fix to litellm/uvicorn side.

## 8. What was NOT proven (honest gaps)

- Mechanism at packet level (why established flows black-hole) — pending tcpdump (§7.5); the
  report deliberately stops at "correlated to ±35 s with the 899 s RENEW clock, refuted the four
  card layers".
- Why exactly every 2nd mark kills (half-cycle cadence) — same probe answers.
- Whether the 18–23 min incident window vs the 30-min chronic cadence had an extra multiplier:
  the operator's 10:16→10:34 window equals exactly 10:17:37 mark → 10:32 kills → recovery, so the
  "23 min" is one RENEW cycle + retry ramp, consistent with the chronic mechanism; no separate
  event is needed to explain the incident, and none is excluded either.
- Older months: current rotated logs cannot say when the 1800 s lease began (router-side change);
  `lease_time` is 1800 s today. Worth a router config diff by the operator.
