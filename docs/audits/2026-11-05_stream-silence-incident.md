# Incident 2026-11-05 — model streams silent while every health check is green

- Card: t_22b546e3 (hscc board) · Author: backend-engineer · Status: **FINAL** — verdict: *not reproduced / not located*; four card layers ruled out, one primary suspect named with an explicit confidence statement
- Scope: locate the silent layer among client (hermes stream reader) / litellm proxy / sparkrun_balancer in-flight accounting / vLLM engines. Fixes are out of scope; fix proposals per layer only.
- Redaction: LAN engines `10.0.0.244/.246/.247/.248`, gateway Mac `10.0.0.245` (documented placeholders; real addresses deliberately omitted — this repo is public). No tokens anywhere.

## 0. Verdict (READ THIS FIRST)

**NOT REPRODUCED — NOT LOCATED.** No layer can be named with direct evidence, so this report
does not claim a root cause. What it does establish, with independently re-verified evidence
(provenance in §0.1), is a **fleet-wide synchronized silence on a ~1800 s clock**: bursts of
8–13 stale-kills from 4–7 different profiles land within 2–8 s of each other, and their implied
onsets (kill − 900 s) fall on a ~30-minute cadence at :17/:47 every hour. Independent per-engine
faults cannot produce that; a shared element on the gateway Mac ↔ engine LAN path can.

**Primary suspect (clock: moderate-high, ~75%; packet-level mechanism: low):** established TCP
flows between the proxy on the gateway Mac and the vLLM engines stop delivering bytes **without
FIN/RST**, in events locked to the gateway's DHCP/T1 lease clock (`en0` lease measured 1800 s →
`configd` logs `DHCP en0: RENEW` every ~899 s; the fatal onsets land on every **second** mark,
i.e. on a 1800 s cadence). New connections and `/health` are unaffected, which is why every
health check stayed green.

Why the clock is the strong half of that claim: the stale-kill fires exactly 900 s after the last
chunk received, so the *last chunk* is a hard anchor and the death must fall in
[`last chunk`, `last chunk` + 900 s]. Across the 11 well-fitting bursts that window's start sits
**+11…+54 s before the RENEW mark every time** (§2.5; 9 of those marks are logged lines, 2 are
interpolated). If the renew were irrelevant, that offset would be spread over the full 900 s;
clustering 11/11 inside a ~45 s band is not chance. What
remains unproven is *why the deaths precede the log line by half a minute*: either `configd`
timestamps the transaction after the on-wire event, or the trigger is a sibling action on the same
lease clock (router-side ARP / forwarding-state refresh at T1, macOS pre-renew path
re-evaluation). Naming the clock is supported; naming the mechanism is not, and this report does
not. One burst of the day does not fit the clock at all and is recorded against the model in §8.

**Ruled out (each with the one-line evidence that refutes it):**

| Ruled out | One-line evidence |
|---|---|
| vLLM engines globally wedged | `/metrics` counters advance 20–90 tok/s aggregate under traffic while agents are silent; engines keep logging `POST … completed 200` for **new** requests every 10–60 s through the silence windows (§2.1) |
| `run=0` + frozen counters = hung streams | `lsof` on the proxy pid shows 1–3 long-lived ESTABLISHED sockets per engine at **idle** — keepalives, as the operator suspected (§2.1) |
| sparkrun_balancer in-flight leak as the cause | `STALE_AFTER_S=900` reaps any un-released reservation, so no permanent leak exists; and a phantom entry makes LeastInFlight **avoid** the backend, inverting the card's mechanism (§2.2) |
| balancer line ~421 as the emitter of the 408s | every 408 traceback originates in litellm `aiohttp_transport.py`/`streaming_handler.py` and surfaces at `proxy_server.py:5605`; :421 only propagates (§2.2) |
| hermes client-side stall | `stream_diag` shows healthy starts (203–1969 chunks, 10–99 KB, ttfb 4.7–6.2 s) then mid-body death; the 900 s stale-kill is detection, and its retries all succeeded in 60–120 s (§2.4) |
| proxy restart / config change around the incident | proxy pid running since Nov 3 04:49; config mtimes Nov 3 06:49 (§2.3) |
| operator load burst as the cause | the identical burst shape recurs unattended at 07:02, 08:01, 09:32, 12:32 and the 408s start at 05:58, hours before any burst (§5) |
| the Mac sleeping | `kern.boottime` Oct 8, uptime 28 d, no Sleep/Wake events in `pmset -g log` for Nov 5 (§2.5) |

**NOT ruled out (candidates that survive):** a litellm/uvicorn-side stall that stops draining
established upstream sockets; a per-stream engine-side stall (vLLM's asyncio model makes this
unlikely but it was not disprovable from the gateway); and the chronic pre-incident 408s, which
are a **separate** mechanism — long decode gaps hitting litellm's ~600 s upstream read-timeout —
that act as the *amplifier* (10 min of blindness per event) rather than the disease.

### 0.1 Provenance of the claims in this report

Two worker runs touched this card. Claims are split so a reviewer knows what was re-checked:

- **Re-verified in run 1372 (this session), independently:** `ipconfig getpacket en0` →
  `lease_time 0x708` (1800 s); `configd` RENEW marks real and ~899 s apart (10:02:38, 10:17:37,
  10:32:36, 10:47:35 with `BOUND`/`publish success` within ~1 ms); stale-kill census **236 kills
  across 15 profiles today**, in bursts of 8–25 spanning 4–7 profiles within 2–8 s, with onsets
  on a :17/:47 ~30-min cadence; no `hermes cron` jobs, no user `crontab` (so the cadence is not
  a scheduler artifact). Script: `verify_renew_correlation2.py` in the profile scratch dir.
- **Recorded in run 1369, not re-checked in run 1372** (budget stop; treat as cited, not fresh):
  the `/metrics` 10 s × 152 s watch output, per-engine `POST completed` counts, the
  `stream_diag` chunk counts, the `litellm/main.py:1403` 600 s default, `state.yaml` proxy pid /
  mtimes, and the `lsof` idle-socket calibration. Each carries its source in the section where it
  appears and is reproducible with the §3 probes.
- **Inconclusive probe (does not change the verdict):** a first-pass burst↔mark correlation
  script in run 1372 returned "0 correlated" because its `log show` predicate over the full day
  yielded zero marks (query bug — the same predicate works over a 1 h window, shown above). That
  null result is a tooling failure, **not** evidence against the correlation, and is not used
  anywhere in this report. The manual 1 h extraction plus the per-burst offsets in §2.5 are the
  usable numbers.


## 1. Timeline (log-verified)

Sources: proxy = `~/.cache/sparkrun/proxy/litellm.log` (gateway-local; the 408s are proxy→engine
read-timeouts, no upstream host printed). Clients = `~/.hermes/profiles/*/logs/agent.log`.
Engines = `sparkrun logs <job>` (vLLM APIserver logs). DHCP = `log show --predicate 'process=="configd"'`.

| Time (local) | Source | Event |
|---|---|---|
| 05:14–05:19 | clients | fleet-wide stale cluster, 15 kills (first of the day in the current logs) |
| 05:32:56, 05:47:55, 06:02:54, … | configd | `DHCP en0: RENEW` every ~899 s (lease 0x708 = 1800 s) |
| 05:58:33–06:13:36 | proxy | 6× `litellm.Timeout: APITimeoutError` → 408 (05:58 = 05:47:55 RENEW + ~638 s) |
| 06:39:02–06:44:41 | proxy | 11× 408 burst (RENEW 06:32:52 + 370–709 s) — self-triggered, no operator load |
| 07:02:00–07:02:01 | clients | **11 simultaneous** stale-kills across 7 profiles (onset 06:47 ≈ RENEW 06:47:51) |
| 07:29:47 | clients | 13 simultaneous kills — weakest fit: onset 07:14:47 vs mark 07:17:49 (+182 s, outside the ±60 s band; see §8) |
| 08:01:47–08:01:55 | clients | 11 simultaneous kills, 6 profiles (≈ RENEW 07:47:47) |
| 08:24:28, 08:25:38 | proxy | 2× 408 (RENEW 08:17:45 + 385/473 s) |
| 08:32:04–08:32:08 | clients | 11 kills, 5 profiles (onset 08:17 ≈ RENEW 08:17:45) |
| 09:25:32–09:26:51 | proxy | 3× 408 (RENEW 09:17:41 + 471 s) |
| 09:33:07–09:33:10 | clients | 9 kills, 4 profiles (onset 09:17 ≈ RENEW 09:17:41); also 9 kills at 09:02:05–07 (≈ RENEW 08:47:43) |
| 09:57:04 | proxy | 1× 408 (≈ RENEW 09:47 + 600 s) |
| 10:02:02–10:02:04 | clients | 8 kills, 4 profiles (onset 09:47 ≈ RENEW 09:47:39) |
| ~10:12–10:17 | operator | load burst (13 injects + 2 /compress) — red herring, see §5 |
| ~10:16 | operator | perceived fleet silence begins (streams launched pre-RENEW die at RENEW 10:17:37) |
| 10:25:57 | proxy | 1× 408 (RENEW 10:17:37 + 500 s) |
| 10:29:17 | client | worker `173fa3` stale-kill; retry completes in ~68 s; failed call `latency=967.9s` |
| 10:32:05–10:32:10 | clients | **9 kills, 6 profiles incl. hscc-orch** — exactly 900 s after RENEW 10:17:37 |
| 10:32:03–10:32:43 | engines | POST completions cluster on .244/.246 — the half-dead streams finally unbind when clients kill them |
| 10:34–10:39 | clients | F5, app worker, hscc-orch first completed calls (retries launched after the kill land fine) |
| 10:57:19–10:58:08 | proxy | 3× 408 (RENEW 10:47:35 + 600 s) |
| 11:02:02 | clients | 3 stale-kills (onset ≈ RENEW 10:47) |
| 11:27:03 / 11:54–11:57 / 12:57:21 | proxy | further 408 bursts, each ≈ RENEW(:47) + 600 s |
| 12:32:02–12:32:05 | clients | 8 kills, 6 profiles (onset 12:17 ≈ RENEW 12:17:29) |

Proxy error census: 84 `LiteLLM Proxy:ERROR` blocks in the current log, ~all
`proxy_server.py:5605 async_data_generator ... litellm.Timeout` (one shape, §2.3). Client
stale-kill census (run 1372 re-sweep of `~/.hermes/profiles/*/logs/agent.log`, current logs only):
**236 kills on Nov 5 across 15 profiles**, clustered in bursts of 3–13 landing at
`RENEW-mark + 900 s`. Run 1369 reported larger per-burst counts because its sweep also included
rotated logs; where the two disagree, the re-sweep above is used. In contrast, rotated logs from
Aug–Oct show this kill pattern
**does not exist** (3 kills total in the sampled older logs) — the RENEW cadence (1800 s lease)
is new; when it began is an open gap (§8), and it needs a router-side config diff to answer.

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

- One error shape only: `aiohttp SocketTimeoutError` → `httpx.ReadTimeout` →
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

### 2.5 The shared clock: gateway en0 on its DHCP-renew cycle (strongest signal found, mechanism unproven)

`ipconfig getpacket en0`: `lease_time = 0x708 = 1800 s` → macOS `configd` renews at T1=½lease,
measured every ~899.0 s: `DHCP en0: RENEW` at 05:32:56, 05:47:55, 06:02:54, 06:17:53, 06:32:52,
06:47:51, 07:02:50, 07:17:49, 07:32:48, 07:47:47, 08:02:46, 08:17:45, 08:32:44, 08:47:43,
09:17:41, 10:17:37 (each ~1 s shorter each cycle), 11:32:32, 11:47:31, 12:02:30, 12:17:29,
12:32:28, 12:47:27 — i.e. marks at :02/:17/:32/:47 each hour. (Sample lines carry
`RENEW/REBIND waiting at 0 for 337.000000` then `BOUND` within ~1 ms.)

Correlation (the whole point). Kill counts are re-derived in run 1372 by grepping
`~/.hermes/profiles/*/logs/agent.log` for `Stream stale for 900s` (569 hits all-time; 236 on
Nov 5 across 15 profiles). Run 1369's per-burst counts were higher because its scan also swept
rotated logs — the direction and the offsets are unaffected, and the lower (re-derived) counts
are used here. The 11:32/12:02 rows use marks **interpolated** from the measured 899 s period (the
run-1369 extract skipped that stretch); they are the only rows not anchored to a logged line, and
their +11/+19 s offsets should be re-checked against `log show` before being quoted elsewhere.

| Client kill burst | onset = kill − 900 s | RENEW mark | mark − onset |
|---|---|---|---|
| 07:02:00–01 (11 kills, 7 profiles) | 06:47:0x | 06:47:51 | +49 s |
| 08:01:47–55 (11 kills, 6 profiles) | 07:46:4x | 07:47:47 | +54 s |
| 08:32:04–08 (11 kills, 5 profiles) | 08:17:0x | 08:17:45 | +41 s |
| 09:02:05–07 (9 kills, 4 profiles) | 08:47:0x | 08:47:43 | +36 s |
| 09:33:07–10 (9 kills, 4 profiles) | 09:17:0x | 09:17:41 | +34 s |
| 10:02:02–04 (8 kills, 4 profiles) | 09:47:0x | ~09:47:39 | +37 s |
| 10:32:05–10 (9 kills, 6 profiles incl. hscc-orch) | 10:17:0x | 10:17:37 | +28 s |
| 11:02:02 (3 kills, 3 profiles) | 10:47:0x | ~10:47:35 | +35 s |
| 11:32:20–23 (3 kills, 3 profiles) | 11:17:2x | ~11:17:31 | +11 s |
| 12:02:12 (3 kills, 3 profiles) | 11:47:1x | ~11:47:31 | +19 s |
| 12:32:02–05 (8 kills, 6 profiles) | 12:17:0x | 12:17:29 | +27 s |

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
changed ACKs, `nw_path_libinfo_path_check` re-evaluation) are in the next-probe item, §7 item 5.
Half-cycle fatality does not weaken the correlation; it halves its frequency.

Not the machine sleeping: `kern.boottime` Oct 8, uptime 28 days, no `Sleep`/`Wake` events in
`pmset -g log` for Nov 5 (only `Assertions` noise). Not a proxy restart (state.yaml, file mtimes).
Not the load burst (§5).

## 3. Reproduction recipe — **NOT VALIDATED** (acceptance bar not met)

Honest status first: **the fault was not reproduced in this card.** No request sent during either
run died on this signature while under observation, so nothing below marked *unvalidated* has
been shown to trigger it. The card's acceptance bar ("reproduce before fixing") is therefore
**unmet**, and per §0 the layer is not located. What is provided instead is (A) a probe that will
catch the next occurrence with per-socket evidence, and (B/C) triggers whose shape is inferred
from the correlation, not demonstrated.

**A. Safe OBSERVATION probe (read-only; partially validated — steps 1–2 and 4 were run and
returned real data, step 3 was run in run 1369):**

1. `log show --style compact --start <T-2h> --end now --predicate 'process == "configd" AND
   eventMessage CONTAINS "DHCP en0"' | grep -E "RENEW|BOUND"` → note marks (:02/:17/:32/:47 pattern).
   **Query tip (learned the hard way):** run this over ≤3 h chunks, not the whole day — a full-day
   `log show` with a `CONTAINS` predicate returned zero marks in run 1372 while the same predicate
   over a 1 h window returned them in 1.2 s. `docs/audits/t_22b546e3_renew_correlation.py` does the
   chunking for you and prints the per-burst offset table.
2. Cross-profile kill scan: `python3 docs/audits/t_22b546e3_stream_forensics.py` (stale-kills +
   `Stream drop` per minute across `~/.hermes/profiles/*/logs/agent.log`). **Signature = bursts of
   ≥3 stale-kills at `mark + 900 s` across ≥2 profiles.**
3. Proxy 408 census: bursts at `mark + ≤600 s` (`grep -a "LiteLLM Proxy:ERROR" litellm.log |
   sed 's/\x1b\[[0-9;]*m//g' | cut -c1-5`).
4. `sparkrun logs <job> | grep "POST completed"` during a window: engines kept completing NEW
   requests (green) — the discriminator against an engine wedge.

**B. Deterministic trigger — UNVALIDATED (inferred from the correlation; never demonstrated).
ORCH/OPERATOR ONLY — mutates gateway networking, do NOT run from a worker.**
open one long streaming request against `http://localhost:4000/v1/chat/completions`
(`worker-model`, ~80K-token prompt, `stream: true`, `max_tokens: 2048`), then renew the lease:
`sudo ipconfig set en0 DHCP` (or `ipconfig set en0 MANUAL` + `DHCP`). *Expected* failure if the
primary suspect is right: stream delivers normally then goes silent within seconds of the renew;
no FIN/RST; hermes kills at 900 s (or `curl` just hangs); proxy emits the §2.3 traceback ~600 s
after the last chunk; other sessions' concurrent streams die at the same instant. If instead the
stream survives the forced renew, the primary suspect is **falsified** — that is the useful result,
which is why this is worth running under operator consent. A cheaper variant with no root: hold the
same stream open across a :17/:47 mark and watch it die — zero config writes. Note the caveat from
§0: the observed deaths precede the logged renew by ~30 s, so "within seconds of the renew" is
optimistic; treat ±60 s as the window.

**C. Bounded canary — UNVALIDATED but safe to run today:** the stream-silence watchdog canary
from §6 — a long streaming request held open across a :17/:47 mark (started ~10 min before), so
the canary is *in flight* when the mark passes. If it goes silent it has caught the fault with 1
affected request instead of 9. A short request launched *between* marks is useless for this fault
class (new connections were never affected — see §4), which is why the canary must be long-lived,
not frequent.

## 4. Why every health check stayed green

`/health` (vLLM) and proxy `:4000` answers are NEW short-lived connections, so under the primary
suspect (established-flow blackhole) they are exactly the checks that cannot see the fault:
engine-wedge check 4/4, proxy 200 in 0.5 s, new small requests fine — while every
already-streaming agent is dead-in-the-water. This reasoning is conditional on §0's suspect not
yet being proven; but the *observation* it explains is solid regardless of the mechanism, and the
generalizable lesson holds for the watchdog design either way: **connection-establishment health
checks are blind to established-flow loss.** If a later probe falsifies §0, this section is the
first thing that must be re-examined, because it is the section that assumes the fault is
flow-scoped rather than service-scoped.

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
2. Make it **lease-clock aware** (cheap, and worth doing even though §0's mechanism is unproven):
   the daemon should stamp `configd DHCP en0: RENEW` marks, or derive them from
   `ipconfig getpacket en0` lease math (T1 = ½ lease). If a stream's last chunk predates a mark by
   ~60 s and nothing has arrived since, treat it as a suspect stream and surface it early rather
   than waiting the full stale-timeout. Do *not* hard-kill on the mark alone — §0 keeps the
   mechanism unproven, so the mark is a prior that shortens the alarm, not a verdict.
3. Pair with an active canary — but see §3C: for *this* fault class a short, frequent request is
   blind (new connections were unaffected), so the canary must be a **long-lived stream held open
   across a mark**. A canary that fails on a *fresh* connection is a different, more serious
   fault class (a real proxy/engine wedge) and should escalate on a separate path.
4. The watchdog must not conclude "engine wedge" from frozen `/metrics` counters — per §2.1 idle
   keepalives + half-dead streams produce exactly that; the discriminator is whether NEW small
   completions advance the counters (§2.1 probe, 2×10 s sampling).

## 7. Smallest fix proposals per layer (NOT implemented — card scope)

1. **Network/host (root-cause owner; operator network config, outside this repo)**: give the
   gateway a static lease / long lease (≥24 h) on the LAN router for `10.0.0.245`, or a static IP
   — removes the 899 s renew clock entirely. Verify per §3B afterwards. This is the smallest true
   fix if the correlation survives the tcpdump probe (§7 item 5).
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
5. **LEFT — next probe (mechanism completion, ORCH/OPERATOR)**: `sudo tcpdump -i en0 -w … host 10.0.0.244
   or host 10.0.0.246 or …` for 40 min spanning two marks with one canary stream open each cycle;
   the capture answers (a) does the death coincide with RENEW to the second, (b) is any direction
   black-holed (ARP? TCP keepalive? RST lost?), (c) why every second mark. If tcpdump shows the
   Mac still receiving engine bytes after the stall, the fault is inside the Mac (socket not
   drained) and shifts the fix to litellm/uvicorn side.

## 8. What was NOT proven (honest gaps)

- Mechanism at packet level (why established flows black-hole) — pending tcpdump (§7 item 5); the
  report deliberately stops at "correlated to ±35 s with the 899 s RENEW clock, refuted the four
  card layers".
- Why exactly every 2nd mark kills (half-cycle cadence) — same probe answers.
- Whether the 18–23 min incident window vs the 30-min chronic cadence had an extra multiplier:
  the operator's 10:16→10:34 window equals exactly 10:17:37 mark → 10:32 kills → recovery, so the
  "23 min" is one RENEW cycle + retry ramp, consistent with the chronic mechanism; no separate
  event is needed to explain the incident, and none is excluded either.
- Older months: current rotated logs cannot say when the 1800 s lease began (router-side change);
  `lease_time` is 1800 s today. Worth a router config diff by the operator.
- **One burst does not fit the clock**: 07:29:47 (13 kills) implies onset 07:14:47, which is
  +182 s from the nearest mark (07:17:49) — outside the ±60 s band every other burst sits in, and
  it is the second-largest burst of the day. Either those streams died of a different mechanism
  (e.g. the chronic decode-gap/600 s timeout class listed in §0), or the clock model is incomplete.
  Any theory that explains the other 9 bursts must also account for this one; the committed
  `t_22b546e3_renew_correlation.py` prints the full burst table so this can be re-checked cheaply
  rather than taken on trust.
- Not probed at all (say-so, not "clean"): the balancer's per-pick history for the victim sessions
  (`balancer_stats.json` holds cumulative pick counts only), and anything on the DGX nodes beyond
  `/metrics` + `sparkrun logs` — no engine-side `ss`/`netstat` was reachable from the gateway, so
  "the engine-side socket went dead too" is inferred from the POST-completion-cluster timing, not seen.

## 9. LEFT — ready-to-card next steps (not done here; card is at budget)

1. **Packet-level mechanism (OPERATOR, privileged):** capture `sudo tcpdump -i en0` for 40 min
   spanning two :17/:47 marks with one long-lived canary stream in flight, to establish whether
   engine→Mac bytes stop arriving (network fault) or arrive and are not drained (Mac/litellm-side
   fault) — the only step that converts "correlated clock" into "located layer".
2. **Falsification test (OPERATOR, privileged):** the §3B forced `ipconfig set en0 DHCP` while a
   long stream is open — a stream that *survives* a forced renew falsifies the primary suspect and
   sends the next worker back to §2.3 (litellm/uvicorn drain stall).
3. **Router-side lease audit (OPERATOR):** find why the gateway lease is 1800 s and when that
   changed, since Aug–Oct logs show this kill pattern essentially absent — a lease of ≥24 h is the
   cheapest possible mitigation and doubles as the confirmation experiment.
4. **Proxy timeout hardening (sparkrun fork):** set an explicit streaming read timeout (~180 s)
   instead of litellm's 600 s default so any silent stream death surfaces as a 408 in ~3 min
   rather than ~10, independent of which layer ultimately causes it (§7 items 2–3).
5. **Watchdog design card (existing parked card, t_291ced43 lineage):** adopt §6 — chunk-progress
   silence at 120–180 s, lease-clock awareness as a prior only, and a long-lived (not frequent)
   canary stream held across a mark.
6. **Balancer observability (sparkrun fork):** add `last_chunk_at` to the streaming hook's
   bookkeeping so a future incident can be attributed per-backend from `balancer_stats` instead of
   reconstructed from client kill timestamps.
