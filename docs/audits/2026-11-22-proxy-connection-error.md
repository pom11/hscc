# :4000 proxy outage, 2026-11-22 — completions 500 "Connection error" while /v1/models 200

Card: t_f54f735a · Type: read-only forensics. **No fixes implemented; the live proxy was not touched.**

Redaction: LAN addresses are `192.0.2.x` placeholders (TEST-NET-1); home tree is `<HOME>`; the
sparkrun cache is `<CACHE>`; the hermes log tree is `<HERMES>/logs`; the hscc state dir is `<STATE>`.
Times below are stated in both local (EET, UTC+2) and UTC; the daemon log is UTC, the hermes logs
and the sparkrun proxy logs are local.

---

## 1. Verdict (one paragraph)

The proxy process that served the window was a **long-lived litellm whose upstream connection pool
was poisoned by node churn**, not a misconfigured one. Its router catalogue stayed fully populated,
so both endpoints that the daemon probes — `/v1/models` and `/v1/model/info` — kept answering `200`
because **neither ever dials an upstream**; every `/v1/chat/completions` did, and failed
`InternalServerError: OpenAIException - Connection error. Received Model Group=orchestrator-model`.
Two daemon repair attempts in the same evening **neither replaced the process**, and the one check
that does probe a real completion through `:4000` (`proxy_stall`) *requires that probe to succeed*
as a precondition of its alarm — so the fast-500 shape is in **no check's domain by construction**.
The suspected macOS Local Network / errno-65 failure mode is **not supported** by the evidence.
The definitive litellm-side proof is **unrecoverable**: the remediation truncated the only log that
held it.

---

## 2. Corrected timeline (local = UTC+2)

| Local | UTC | Event | Source |
|---|---|---|---|
| 21:10:40 | 19:10:40 | `proxy_stall` fires: `relaunching with [244,246,247,248] (attempt 1 of 3 before stall alarm)` | `<STATE>/daemon.log` |
| 21:10:54 | 19:10:54 | `proxy repair on :4000 FAILED (relaunch failed)` — **first repair attempt, no replacement** | `<STATE>/daemon.log` |
| 21:54:16 | 19:54:16 | daemon SIGTERM → `start-daemon invoked (service-supervised mode)` (launchd-supervised restart) | `<STATE>/daemon.log` |
| ~21:53–21:55 | ~19:53–19:55 | unit `…-246-8003` down; node `192.0.2.246` is **paused** → auto-heal suppressed (`down 3x … no stop and no relaunch`) | `<STATE>/daemon.log` |
| **21:58** | **19:58** | **first** `API call failed … OpenAIException - Connection error … Model Group=orchestrator-model` (`base_url=http://localhost:4000/v1`) | `<HERMES>/logs/gateway.error.log` |
| 22:02:21 | 20:02:21 | `Worker proxy unhealthy on :4000 (live router missing backend(s) ['192.0.2.246:8003'])` → drift repair started (node resume re-added .246) | `<STATE>/daemon.log` |
| 22:02:35 | 20:02:35 | `proxy repair UNVERIFIED on :4000 — port owner 15706, sparkrun tracks 15701` → `repair FAILED (repair incomplete (unverified))` | `<STATE>/daemon.log` |
| 22:15:45 | 20:15:45 | hermes: `kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks but 0 workers spawned` | `<HERMES>/logs/errors.log` |
| 22:16:08 | 20:16:08 | daemon restart #2 (bootstrap 2.17.3); `Started proxy check thread (interval=60s)` — **proxy untouched by the restart** | `<STATE>/daemon.log` |
| 22:18–22:44 | 20:18–20:44 | dense failures, ~11–35 log lines/min; `Dispatcher-wedge check: GENUINE STALL detected` ×8 across 20:15–20:39 | both logs |
| 22:55:51 | 20:55:53 | forced `--restart` from a terminal → wrapper **85087**, listener **85090**, autodiscover **85097** | `ps`, `<CACHE>/sparkrun/proxy/state.yaml` |
| 22:55 | 20:55 | **last** `Connection error` line; nothing after | `<HERMES>/logs/gateway.error.log` |
| 20:56→21:12 | 20:56→21:12 | `GENUINE STALL detected` count drops to **0** | `<STATE>/daemon.log` |

Two corrections to the task premise: the outage **began at 21:58 local, not ~22:10** (12 min earlier),
and it was **not continuous** — there is a clean gap 22:03–22:17 with zero failures, bracketing the
22:02 repair attempt.

---

## 3. Root cause

### 3.1 What is proven

- **Backends were healthy while the proxy failed.** The daemon's own `engine-wedge` check — which
  POSTs a real streaming completion **directly to each node**, `health.py:4666`
  (`http://{node}:{port}/v1/chat/completions`) — reported `4/4 streaming ok` at 20:02:34, 20:06:12
  and 20:09:37 UTC, i.e. *inside* the outage, from the same host. The failure is scoped to the proxy
  process, not to the fleet or the network path in general.
- **The router catalogue was populated.** `check_proxy` requires every expected alias to appear in
  the `/v1/models` body (`_proxy_models_healthy`, `health.py:1176-1196`) and reports
  `"empty model catalogue (HTTP ok, data: [])"` otherwise. It produced **no unhealthy verdict for
  ~45 min** ⇒ the catalogue was non-empty and served `worker-model` + `orchestrator-model`. This
  rules out the 2026-10-11 shape (empty catalogue) as the cause here.
- **The live router was pointing at healthy backends, not at the paused node, by 20:02.**
  `_proxy_backend_probe` reads `/v1/model/info` from the *running* proxy — the only endpoint that
  reveals the live `litellm_params.api_base` set (`health.py:1583-1591`). At 20:02:21 it reported
  the router **missing** `192.0.2.246:8003`, i.e. autodiscover had already *removed* the dead node.
  So at 20:02 the router held only live bases and completions still failed.
- **The 22:02 repair did not replace the process.** It logged `port owner 15706, sparkrun tracks
  15701`. A real replacement would have moved sparkrun's tracked PID to a *new* wrapper; instead the
  tracked PID (15701) and the listener (15706) are a **wrapper/child pair from the same launch** —
  the same shape as the current healthy proxy (wrapper 85087, child 85090, `PPID(85090)==85087`).
  The tracked PID never moved ⇒ no new process. This matches the earlier explicit
  `FAILED (relaunch failed)` at 19:10:54 and the known stale-PID refusal path
  (`_start_refused_already_running`, `health.py:1859-1877`).
- **Nothing else restarted the proxy.** The daemon restart at 20:16:08 started new check threads but
  produced no proxy stop/start lines; the outage continued unchanged until the forced `--restart`.
- **The fix that worked was a process replacement**, from a terminal, with `--restart`.

### 3.2 Leading mechanism (by elimination and timeline — **not directly evidenced**)

The proxy process kept a **stale/poisoned upstream connection pool**: litellm reuses a per-deployment
httpx client, and once those sockets are dead every request fails immediately on connection reuse
while `/v1/models` and `/v1/model/info` — answered locally by litellm, never touching upstream —
keep returning `200`. That explains all observations at once: fast (not hanging) 500s, the whole
model group failing rather than one deployment, models-200 persisting indefinitely, direct probes
from other processes succeeding in 0.35 s, and a forced process replacement being the only thing that
cleared it. The trigger fits: `192.0.2.246` was paused and its unit went down ~19:53–19:55 UTC, and
the first client failure lands **19:58 UTC**, within minutes of the churn.

**Open alternative** (weaker, not excluded): at onset (19:55–19:58) the live router still pointed at
the just-paused `192.0.2.246:8003` and litellm failed to fail over across the group. See §4.3 — the
daemon was structurally unable to see that direction while the host was paused.

### 3.3 Hypothesis explicitly **not supported**: macOS Local Network / errno 65

`log show` over 21:55–23:05 local, filtered on `tccd`, `Local Network`,
`kTCCServiceSystemPolicyLocalNetwork` and network-extension senders (87 KB captured): **0** Local
Network mentions, **0** denials naming our binaries, only 3 `tccd` lines total. The daemon process
itself reached all four LAN backends successfully inside the window. The known errno-65 failure mode
is **not** what happened here.

### 3.4 Evidence destroyed by the remediation (this is a finding, not a footnote)

The forced restart at 20:55:53 UTC **truncated or rewrote every artifact that could have proved
§3.2 directly**:

| Artifact | State |
|---|---|
| `<CACHE>/sparkrun/proxy/litellm.log` | truncated — first line is `INFO: Started server process [85090]`; `grep -cE 'Connection error\|InternalServerError'` → **0** |
| `<CACHE>/sparkrun/proxy/autodiscover.log` | truncated — first line `22:55:53 … Running script in parallel on 4 hosts` |
| `litellm_config.yaml`, `discovery.json`, `state.yaml`, `autodiscover.yaml`, `sparkrun_balancer.py` | all rewritten 22:55 (mtime) |

No rotated copy exists anywhere (`find` over the sparkrun cache and home for `litellm*log*` returns
only the live file plus an unrelated Oct-31 incident copy). **57 minutes of the only litellm-side
record were destroyed by the act of fixing it.**

---

## 4. Why the daemon never noticed

Three proxy-side checks exist; the outage falls in the hole between them.

### 4.1 `check_proxy` (60 s) — never asks for a completion

`_proxy_models_probe` (`health.py:1113`) GETs `/v1/models` and parses the body; `_proxy_models_healthy`
(`health.py:1176`) requires the expected aliases. `_proxy_backend_probe` (`health.py:1583`) GETs
`/v1/model/info`. Both are **local-to-litellm endpoints**: neither opens an upstream connection, so
a proxy whose every upstream dial fails is green on both, permanently. The module already documents
this class of blindness in its own words at `health.py:1201-1210` ("`check_proxy` stayed GREEN all
afternoon"), and `health.py:1206-1210` states the doctrine "the catalogue lists the ids is not the
backend serves them" — yet the extension that was made was *from* `/v1/models` *to*
`/v1/model/info`, still not to a completion.

**Proof it held indefinitely:** `Running proxy check` appears every 60 s from 19:54:17 to 21:12 UTC
(~80 ticks). Across 20:03–21:12 UTC there is **not one** unhealthy proxy verdict — the only non-`Running`
proxy lines in the whole period are the 20:02 drift burst and the two `Started … thread` boot lines.
A models-200 / completions-500 split can stand forever, and did.

### 4.2 `check_proxy_stall` (30 s) — requires the completion probe to **succeed**

The stall watchdog *does* send a 5-token completion **through :4000**, but as condition **(c)** it is
a *precondition for declaring a stall*: `proxy_stall.py:14-22` defines a stall as ALL THREE of
(a) ≥1 ESTABLISHED socket on :4000 (`_cond_a`, `:237-239`), (b) fleet `running+waiting == 0`
sustained ≥120 s (`_cond_b`, `:242`), (c) **the through-proxy probe still SUCCEEDS** (`_cond_c`,
`:247-255`, strict `is True`). Its own docstring closes the loop: *"a fully-sick proxy is
`check_proxy`'s domain, not the held-stream shape"* (`proxy_stall.py:250-252`).

So `check_proxy` deliberately excludes completions and `proxy_stall` deliberately excludes *failing*
completions. **The intersection of the two exclusions is exactly this outage shape.** The stream ran
normally the whole window (30 s cadence, started 20:16:08) and stayed inert, which is correct per its
spec — that is the problem.

### 4.3 The paused-node exclusion hid the one direction that could have mattered at onset

`_proxy_backend_drift` (`health.py:1713`) computes both `missing` (`:1746`) and `dead` (`:1747`), but
subtracts `expected_absent` — every backend **whose host is paused** — from **both** (`:1743-1745`).
The docstring is explicit that this exclusion exists because reporting DEAD for a parked host once
"stopped the fleet's only entrypoint". Consequence: while `192.0.2.246` was paused, a router still
pointing at `192.0.2.246:8003` was **invisible by construction**. Across the whole `<STATE>/daemon.log`
there are **2** `live router missing backend(s)` verdicts ever and **0** stale/extra-backend verdicts
of any kind — the DEAD direction has never fired.

### 4.4 Repair verification cannot succeed under a `uvx` launcher

`_proxy_tracked_pid` (`health.py:1793`) reads sparkrun's own `proxy status --json`, which reports the
**wrapper** PID. `_proxy_listener` (`health.py:1775`) reads the **port owner** via `lsof`. When the
proxy is launched as `uv tool uvx … litellm`, those are different processes — **always**. Reproduced
live against the *current, healthy* proxy:

```
<CACHE>/sparkrun/proxy/state.yaml : pid: 85087            (sparkrun's tracked PID)
ps                                  : 85087 = uv tool uvx --from litellm[proxy]==1.82.6 …
                                      85090 = python …/bin/litellm …   PPID 85090 → 85087
lsof -iTCP:4000 -sTCP:LISTEN        : python3.1 85090  TCP *:4000 (LISTEN)
```

Tracked 85087 ≠ owner 85090 on a **perfectly healthy** proxy. Therefore the verification predicate
can never pass, every repair logs `repair UNVERIFIED … port owner X, sparkrun tracks Y` and reports
`FAILED (repair incomplete (unverified))` — and the fleet has been living with a repair path whose
success condition is unsatisfiable. That is the descendant-PID follow-up referenced in the card body,
now confirmed with a live reproduction.

**Corollary, higher severity:** `_reap_orphan_proxy_listener` (`health.py:1810-1856`) uses the *same*
predicate as its "is this mine?" guard — `if tracked_pid is not None and pid == tracked_pid`
(`:1826`) — and otherwise, once the cmdline contains `litellm`, it **SIGTERMs and then SIGKILLs the
listener** (`:1835-1852`). Under `uvx` the healthy listener is *never* the tracked PID, so this guard
does not protect it: the reaper is armed to kill a healthy production proxy and call it an orphan.

---

## 5. Impact (measured, 20:10–20:56 UTC window)

- **157** `API call failed (attempt N/3)` lines through `base_url=http://localhost:4000/v1` with
  `model=orchestrator-model` (the only model affected in-window).
- **31** of them are `attempt 3/3` — turns that exhausted all retries and **failed outright**.
- **8** `Dispatcher-wedge check: GENUINE STALL detected` events during the window; **0** in the
  16 min after the fix.
- **6** hermes `kanban dispatcher stuck: ready queue non-empty … but 0 workers spawned` warnings
  (22:15:45: 6 ticks; 22:20:51: 11 ticks, `rate_limit_cooldown=3`).
- Every agent whose model resolves through `:4000` was affected; the orchestrator role took the
  visible damage (`Model Group=orchestrator-model`, surfaced as "waiting on provider / Provider
  temporarily unavailable / Transient APIConnectionError").
- **Not attributable to the outage** from these logs: the dispatcher's own hold reasons in-window
  were `active_pr=1` / `active_pr=2`, and no kanban run record in the reachable logs names the
  proxy as the cause. Counting *requeued cards* requires a board read that this read-only pass did
  not perform (see LEFT).

---

## 6. Recommended fix cards (atomic, one seam each — NOT implemented here)

Ordered by value. Each names the single seam so two cards never edit the same decision.
**Carded on the board 2026-11-22 as children of t_f54f735a** (ids below); none is implemented here.

1. **Completion-probe health for `check_proxy`** — `t_f0b8051b` *(closes §4.1 — the outage)*.
   Add one tiny non-streaming completion (`max_tokens=1`) through
   `http://localhost:PROXY_PORT/v1/chat/completions` to the `check_proxy` verdict
   (`health.py:2098`), unhealthy on transport failure or 5xx, keeping `/v1/models` as the cheap
   pre-check. Reuse the engine-wedge probe body shape (`health.py:4650-4670`) so the probe is known
   good against vLLM. Guard against probing a deliberately-down proxy (autodown/pause).
2. **Make repair verification descendant-aware** — `t_65f77a97` *(§4.4, the verify predicate only)*.
   `health.py:1793/1942`: accept a listener that **is** the tracked PID **or a descendant of it**
   (walk `ppid` a bounded number of levels), so a `uvx`-launched proxy verifies instead of always
   reporting `FAILED (repair incomplete (unverified))`.
3. **Orphan-reaper must not kill a descendant of the tracked PID** — `t_af7d5486` *(§4.4 corollary —
   separate card because it is a kill path)*. `health.py:1826`: the "is this mine?" guard must apply the same
   descendant rule as card 2 before any `SIGTERM`/`SIGKILL`.
4. **Preserve the outgoing proxy log across restarts** — `t_9713c765` *(§3.4 — forensics survival)*.
   The repair/restart path should archive `<CACHE>/sparkrun/proxy/litellm.log` (timestamped copy,
   bounded retention) before the new process truncates it. This is sparkrun-owned territory; prefer
   the smallest upstream sparkrun addition and file it, rather than forking log handling in hscc.
5. **Restore visibility of paused-host backends in the live router** — `t_892378c1` *(§4.3, visibility only)*.
   `health.py:1743-1747`: keep the paused-host exclusion from *triggering* a repair (that is
   t_90958eb8's fix and must not regress), but log a WARN naming backends the live router still
   holds for paused hosts, so the direction is observable instead of silently subtracted.
6. **Log proxy-check state transitions** — `t_e26aaa56` *(forensics)*. `check_proxy` logs `Running proxy check`
   every tick and nothing when green, so "green for 45 minutes" is only provable from *absence* of
   lines — exactly what made this investigation slow. Emit a line on green→unhealthy and
   unhealthy→green transitions (and a low-frequency heartbeat verdict), so a future window is
   positively evidenced.

## 7. LEFT for the next worker

- **§3.2 is a hypothesis, not a proven mechanism.** The litellm-side exception and upstream host are
  gone. To settle it, reproduce deliberately in a maintenance window: `sparkrun proxy status`, note
  the PID, then pause the node of one pooled backend and wait for the pool to rot; capture
  `lsof -nP -iTCP:<port> -sTCP:LISTEN -a -i 4 -c python` for *established* upstream sockets before
  and after, plus a copy of `litellm.log` **before** any restart.
- **Requeued-card count** (§5): needs a board read of runs in 20:10–20:56 UTC — not done here to
  keep the pass read-only on logs and state.
- The `proxy_stall` stream ran clean and inert all window; no change proposed (its spec is correct
  for its own failure shape). Card 1 is what covers this shape.
