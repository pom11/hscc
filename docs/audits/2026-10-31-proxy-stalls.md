# LiteLLM proxy :4000 stall investigation — 2026-10-30/31

Card: `t_447dd0aa`. Scope: **investigation + audit only; no fixes were applied.**
Three stall episodes affecting the sparkrun LiteLLM gateway on port 4000 on the
Mac (the sparkrun host), plus the daemon's failed repair and the "FLEET
VISIBILITY LOST" sparkrun shell-out timeouts.

**BANKED STATE (operator directive 19:5x local, 2026-10-31).** This document was
banked mid-run. **Question (a) is now answered** — §6 (added by follow-up card
`t_c95a2137`) names the per-request component and verdicts each candidate per
failure shape, leaving only its *magnitude and trigger* unmeasured (§6.5 gives
the capture recipe). Questions (b) and (c) remain partial: the window-A
escalation forensics went to `t_cc580973` and the sweep-starvation cause to
`t_9328b385` (ids in the parent card result). Sections 1–6 hold only what is
backed by a timestamped log line or a `file:line` code citation; everything else
carries an explicit `UNPROVEN` marker.

Conventions: `local` = EET = UTC+2. Daemon and `daemon_proxy_events.txt` logs
are **UTC**; `litellm.log`, `autodiscover.log` and `ps.txt` are **local**. All
LAN/tailnet addresses are replaced with the documented placeholders
(`10.0.0.x` = a LAN node, `100.64.0.1` = a tailnet host); real values live in
the operator's evidence dir. Node placeholders used below:
`10.0.0.1:8000`, `10.0.0.2:8001`, `10.0.0.3:8002`, `10.0.0.4:8003`.

Evidence sources (paths as captured by the operator):

- `~/fleet-check/proxy-stall-2026-10-31/` — `litellm.log` (17,421 lines),
  `autodiscover.log` (3,172 lines), `daemon_proxy_events.txt` (162 KB, the
  daemon's proxy-topic slice), `litellm_config.yaml`, `sparkrun_balancer.py`,
  `ps.txt`, `status.txt`, `tcp_states.txt`, `tcp_count.txt`. Capture time
  15:54:xx local (directory mtime).
- `~/.hscc/daemon.log` — the daemon SOT log (UTC), covering 2026-10-26 to
  2026-10-31T18:02Z.
- `hscc` repo @ `origin/main` `928e1aa3` for the daemon repair path.
- `/Users/desac/sparkrun/src/sparkrun` (operator fork, **read-only here**) for
  the gateway/status/discovery paths.

---

## 1. Timeline

### 1.1 Process lifetime

| Fact | Value | Evidence |
|---|---|---|
| litellm wrapper PID | 91693 (parent 1) | `ps.txt` |
| litellm real server PID | 91757 (child of 91693) | `ps.txt` |
| Both started | **local 2026-10-31 01:50:29 / 01:50:46** = **UTC 23:50:29 / 23:50:46 on 2026-10-30** | `ps.txt` (`STARTED` + `ELAPSED 14:03:45` at the 15:54 capture) |
| sparkrun state-file start stamp | `2026-10-30T23:50:33.257935+00:00` | `status.txt` |
| PID sparkrun tracks | 91693 (the **uvx wrapper**, not 91757) | `status.txt` `PID: 91693`; `ps.txt` parent/child |
| Autodiscover | `running (process)` | `status.txt` |
| Listener on :4000 at 15:54 capture | **present** — `1 (LISTEN) *:4000` | `tcp_states.txt` line 1 |

Correction to the operator's framing, stated plainly: the operator read
"litellm pid 91757 up since 01:50 local" as a lifetime that begins *after* the
first stall window (00:2x–03:19 local). Both PIDs are the **same process pair**
recorded twice under different clock labels — 01:50 local is 23:50 UTC on
2026-10-30. The pair therefore **spans every stall window** in this audit, and
so does the operator's "restart at 15:55" (see 1.3): the restarted gateway is a
*new* process, and the pid pair in `ps.txt` predates that restart, so `ps.txt`
was captured **before** the 15:55 operator restart. That ordering is what makes
`tcp_states.txt` sayable (§2.1).

The pair's start time also lands **inside** the daemon's own repair sequence:
`2026-10-30T23:50:05 … escalating to orphan repair`, then
`23:51:05 proxy repair UNVERIFIED — port owner None, sparkrun tracks 91693`
(`~/.hscc/daemon.log` line 159287 area) — i.e. the daemon's escalated repair is
what brought this pid pair up, and the repair declared itself unverified while
sparkrun already tracked 91693.

### 1.2 Stall windows (evidence-backed)

"Stall" here = the daemon's `/v1/models` probe failed. The probe is
`urllib.urlopen("http://localhost:4000/v1/models", timeout=5)`
(`hscc_daemon/health.py:1378`, `:519-543`), so **every recorded failure costs
the daemon ~5 s** and two distinct failure modes are distinguishable in the
log: `timed out` (listener accepts, no response) vs
`[Errno 61] Connection refused` (no listener at all).

| # | UTC window (2026-10-30/31) | local (UTC+2) | Failure mode | Anchor daemon lines (`~/.hscc/daemon.log`) |
|---|---|---|---|---|
| pre-incident | 10-30 12:40:23 | 10-30 14:40 | timed out | isolated single unhealthy tick + 1 failed relaunch |
| pre-incident | 10-30 22:54:59 | 10-31 00:54 | timed out | 1 tick, relaunch failed |
| **A** | 10-30 **23:12:53 → 23:54:32** | 10-31 01:12 → 01:54 | timed out, then `Connection refused` from 23:46:00 | 30 unhealthy ticks; 17 relaunch attempts; 16 `proxy repair … FAILED (relaunch failed)`; 3 `escalating to orphan repair` (23:29:07, 23:41:04, 23:50:05); 3 `repair UNVERIFIED` (23:34:12, 23:44:59, 23:51:05) |
| **B** | 10-31 **00:12:07 → 02:06:55** | 10-31 02:12 → 04:06 | timed out (exclusively) | 12 unhealthy ticks, all `relaunch failed: Discovering inference endpoints…` + `Error: Proxy is already running (PID 91693)` |
| **C** | 10-31 **07:46:29 → 07:46:44** | 10-31 09:46 → 09:46 | timed out | 1 unhealthy tick, 1 relaunch, `repair … FAILED (relaunch failed)` |
| operator restart | 10-31 **13:54:21, 13:55:24** | 15:54, 15:55 | `Connection refused` ×2 | last two `Worker proxy unhealthy` lines in the whole log (line 178366, 178398) |
| stability | 10-31 13:55 → 18:02 (log end) | 15:55 → 20:02 | **zero proxy events** | 180 `Running proxy check` ticks in 13:55Z–15:53Z window with no unhealthy/repair line following any of them |

The operator's own three local windows (00:2x–03:19, ~04:xx, 09:46) map to
windows A/B/C above with a consistent +2 h offset, confirming the EET↔UTC
anchoring.

### 1.3 The operator restart at 15:55 local (operator-reported, not logged here)

`sparkrun proxy stop` reported success; litellm kept running with **no
listener** (graceful drain on open streams), ignored `SIGTERM` for 20 s and
needed `kill -9`; `sparkrun proxy start` then served both aliases 200 in <1 s.
The two daemon ticks immediately before it are the only `Connection refused`
proxy events after window A, at 13:54:21Z and 13:55:24Z — i.e. the daemon
independently observed the listener-less-but-alive state the operator then
fixed by hand. **The kill -9 itself is operator testimony, not in any log I
read** — the daemon logged nothing about it (see §2.2 for why it could not).

### 1.4 The stability window is evidence

From 13:55Z to the end of the log (18:02Z) — 4h07m — the proxy check ran every
60 s (`hscc_daemon/daemon_ops.py:33` cadence `proxy: 60`; confirmed by the
`Started proxy check thread (interval=60s)` banner in
`daemon_proxy_events.txt`) and produced **no** unhealthy tick, **no** repair,
**no** `FLEET VISIBILITY LOST`. Over the same span the engines were under
normal worker load (worker check `3/3 online` continues). Nothing in the fleet
configuration changed in that window except the operator's restart. That is
consistent with "the restart fixed a real process-level defect", and against
"the stalls were load/backpressure artifacts of the engines" (engines were busy
before and after alike).

---

## 2. Root cause per question

### (a) What stalls LiteLLM

**Verdict (partial):** the stall is **inside the gateway process, and it is not
a whole-process / event-loop freeze.** The evidence refutes "the loop blocked"
and refutes "autodiscover restarted the gateway", and narrows the remaining
candidates to a *request-path* block. Which request-path component blocks is
**UNPROVEN** and is the subject of follow-up `t_447dd0aa-A`.

Evidence that the HTTP layer stayed alive during window B
(`litellm.log`, whole-file analysis of the 17,376 uvicorn access lines):

An external poller hits five paths continuously all day — `/version`, `/props`,
`/api/tags`, `/api/v1/models`, `/v1/props` (1,407/1,362 occurrences each; they
are Ollama-dialect probes, so LiteLLM 404s them; nearest match to a
`/api/tags`+`/api/v1/models`+`/props` fan in this repo is
`hscc_daemon/health.py:1661` `check_local`, which probes only `/api/tags` — the
five-path fan-out source is **UNPROVEN**). Because uvicorn writes one access
line **at response time**, the position of those lines in the log is a
liveness trace of the serving loop:

    /version               count= 1407  max_gap=    51  median_gap=11
    /props                 count= 1362  max_gap=   104  median_gap=11
    /api/tags              count= 1407  max_gap=    49  median_gap=11
    /api/v1/models         count= 1407  max_gap=    57  median_gap=11
    /v1/props              count= 1407  max_gap=    49  median_gap=11
    /v1/models             count= 2614  max_gap=    28  median_gap=7
    /v1/chat/completions   count= 4821  max_gap=    76  median_gap=2
    /v1/model/info         count= 2461  max_gap=    83  median_gap=4
    poller-group (5 paths) count= 6990  max_gap=    46  median_gap=1
    poller gaps > 300 lines: 0

Across the whole 14 h lifetime, no path ever goes silent for more than ~100
lines, and the poller group never skips more than 46 lines. A single blocking
call on the event loop long enough to make a 5 s probe time out would have
frozen **all** paths at once and produced a huge positional gap in every path
simultaneously. There is no such gap. **Therefore: no whole-loop block, no
process freeze, no GC-style stop-the-world long enough to explain the
timeouts.**

Also refuted outright:

- **litellm.log contains zero errors.** `grep -ciE
  'error|warning|traceback|timeout|exception'` over `litellm.log` → **0**. All
  17,376 access lines are 200 or 404; no 5xx, no cancelled requests, no
  LiteLLM-side warnings. The gateway logged nothing about being slow.
- **autodiscover did not restart the gateway.** `autodiscover.log` is 1,586
  perfectly-spaced sweep pairs (`Running script in parallel on 4 hosts` +
  `Parallel execution done: 4/4 OK (1.0-2.9s total)`), every ~30 s, with **zero**
  lines matching `reconciled|failed|deferring|restart|error|gone|exiting|shutting`
  — 0 occurrences of `reconciled`. sparkrun only restarts the gateway when the
  desired model set changes (`proxy/engine.py:531-540`: `if not added and not
  removed and not router_changed:` → skip). Steady state ⇒ **no rewrite, no
  restart, no router churn**. The "30 s config reload rebuilds the router and
  blocks in-flight requests" hypothesis is **refuted as a restart mechanism**;
  whether the *read-only* half of a sweep (`discover_endpoints` → SSH health
  probes of 4 nodes, `proxy/autodiscover.py:192-227`, in a **separate process**,
  so it cannot block the gateway's loop at all) correlates with stall
  boundaries remains open.
- **The balancer's lock is a plausible but unconfirmed mechanism.**
  `sparkrun_balancer.py:151` is a plain `threading.Lock` held by `pick()`,
  `release()`, `release_failed_attempt()`, `release_request()` and
  `in_flight()/faulted()`. Every hook that takes it is `async def`
  (`:282-315`) — i.e. `self._lock` is acquired **on the event loop**
  (`async_filter_deployments` → `pick()` → `with self._lock:` at `:223`).
  The critical sections are short and non-blocking in themselves (dict
  arithmetic; `_expire_locked()` at `:206-215` is O(held+tried+faulted), bounded
  by in-flight requests), so contention needs an unusually large `_held`/`_tried`
  map or a slow `_clock`. Nothing in `litellm.log` records balancer state, so
  this is **UNPROVEN** — see FIX-4 (make it observable) before any code change.
  Note also the running config does **not** use the balancer for routing:
  `litellm_config.yaml` has `router_settings: routing_strategy: simple-shuffle`
  with the balancer present only as `litellm_settings.callbacks:
  - sparkrun_balancer.handler`. So the balancer **is** loaded and **is** on the
  request path via callbacks, but the placement decision is LiteLLM's
  shuffle — the balancer's `pick()` result is *not* what routes. That weakens
  (does not eliminate) the balancer-as-culprit hypothesis, and is itself worth
  the orchestrator's attention as a config/intent mismatch (FIX-5).
- **`netstat` at 15:54 shows a healthy listener**, `1 (LISTEN) *:4000`, plus
  14 established proxy→backend sockets and 4 loopback established sockets to
  :4000 (`tcp_states.txt`, `tcp_count.txt` = 31). What that **proves**: at
  capture time the process was bind-and-listening, so the operator's
  "no listener" state had not yet been reached when the capture ran
  (capture 15:54, restart 15:55). What it **does not prove**: anything about
  windows A/B/C — the capture is 13 h after window C. It does, however,
  refute the card's working premise that "0 LISTEN was recorded at capture":
  **one LISTEN was recorded** (`tcp_states.txt` line 1).

**What is therefore established about (a):** a delay *inside* the proxy,
confined to the completion path of some requests (the operator measured 24 s at
09:5x and 16 s at 15:5x with 3-4 running / 0 waiting per node — those timings
are operator measurements, **not present in any log I read**, so recorded as
operator fact), with the serving loop demonstrably still processing every
other request throughout. The remaining candidate class is a *per-request*
await that blocks for tens of seconds somewhere between uvicorn accepting and
LiteLLM logging — LiteLLM internal (health-check state churn, deployment
cooldown, HTTP client pool exhaustion against a backend that holds a
connection), or the balancer's lock under a pathological map size. **UNPROVEN.**

### (b) Why the daemon repair fails and never escalates

**Verdict (proven, three compounding causes).** All three are `file:line`
citable in `hscc_daemon/health.py` (at `origin/main` `928e1aa3`).

**b1 — "already running" refusal, exactly as t_43b6e245 predicted, because
`restart` is False when the probe *timed out*.** The plain relaunch
(`health.py:1541-1545`) becomes
`sparkrun proxy start --cluster … --hosts … --port 4000` with `--restart`
**only** when `_proxy_needs_restart(probe)` is true, which is
(`health.py:715-727`):

    return bool(probe.get("ok")) and not (probe.get("models") or [])

i.e. restart only when the proxy **answered** with a parseable **empty**
catalogue. In windows A/B/C the probe **timed out** → `probe["ok"] is False`
→ `restart = False` → plain start → sparkrun refuses, because
`is_running()` is state-file PID + `os.kill(pid, 0)`
(`/Users/desac/sparkrun/src/sparkrun/proxy/_supervisor.py:570-575`) and the
uvx wrapper 91693 is alive. The daemon even prints the refusal —
`daemon.log` line 162133 (continuation of the 02:06:55Z ERROR):

    Error: Proxy is already running (PID 91693) on port 4000.
    Use --restart to apply new settings, or 'sparkrun proxy stop' first.

So t_43b6e245's loud-failure fix **delivered** the surfaced stderr (that line
is its acceptance criterion, visibly working) but the *decision rule it feeds*
was not widened: a timed-out probe is precisely the
alive-but-serving-nothing shape that needs `--restart`, and `_proxy_needs_restart`
classifies it as "not restartable". This is the gap the earlier fix missed.

**b2 — the escalation ladder resets, so `PROXY_RELAUNCH_MAX_CONSECUTIVE` is
almost never reached.** `escalated = (prev_streak + 1) >= 3`
(`health.py:1509`, `:784-785`), and the streak is cleared **only by the healthy
branch** (`health.py:1590-1594` and the healthy write at `:1428-1433`). In
window A the ladder did fire (3 escalations) and **all three failed**, then the
proxy recovered for ~17 min and the streak cleared; window B restarted at
"attempt 1 of 3" and **never** got past attempt 1 because a healthy tick
cleared it. The pattern in `daemon_proxy_events.txt` is decisive: **17**
`relaunching with [...] (attempt 1 of 3)` lines vs **2** `attempt 2 of 3` and
**0** `attempt 3 of 3`. The design intends 3 strikes; the observed distribution
is one strike, repeatedly.
**Why each escalated repair failed is itself open** — see b3.

**b3 — repair verification can never pass under `uvx`, so the escalated path
reports failure even when it worked.** `_proxy_repair_orphan` verifies with
(`health.py:1176-1182`):

    verified = (owner is not None and tracked_after is not None
                and owner == tracked_after)

`owner` is the **lsof port owner** (`_proxy_listener`, `health.py:1058-1073`) =
the real uvicorn child (91757 class). `tracked_after` is sparkrun's state-file
PID = the **uvx wrapper** (91693 class, `ps.txt`). These are different PIDs by
construction for a `uv tool uvx` launch, so `owner == tracked_after` is
**false whenever the proxy is up and correct**. Whole-log check:
`grep -c "proxy repaired on" ~/.hscc/daemon.log` → **0**, against **12+**
`proxy repair UNVERIFIED` lines including `port owner 35546, sparkrun tracks
32570` (`daemon.log:101786`, two live processes, cmdline visibly litellm). The
escalation is therefore **structurally unable to report success** — it will
always log `repair incomplete (unverified)` and re-strike the breaker.

**b4 (secondary) — the repair's own shell-outs die on timeout, losing the
escalation entirely.** Window A's escalations at 23:29:07Z and 23:41:04Z were
followed 5 min later by `Command timed out after 90s` (23:34:12Z, 23:44:59Z)
— that 90 s is `run_cmd(sparkrun_bin.argv("proxy","start",…), timeout=90)` at
`health.py:1167`. `run_cmd` implements the timeout via
`subprocess.run(..., timeout=…)` (`hscc_daemon/util.py:39-60`), which on
timeout kills **only its direct child**. `sparkrun proxy start` is the
`--foreground`-less path that first prints `Discovering inference endpoints…`
(`/Users/desac/sparkrun/src/sparkrun/cli/_proxy.py:127`) and then runs a
**4-host parallel SSH discovery sweep** (`proxy/autodiscover.py:192-198`,
2.9-3.5 s in the logs) — so the 90 s cap covers a network sweep whose child
processes survive the kill. **UNPROVEN** whether the timed-out sweep or a
starved host caused the timeout (see §4); what is provable is that **no**
`reaping ORPHAN proxy listener` / `killing HOLLOW tracked proxy PID` line
appears anywhere near these windows, while such lines *do* exist on 10-27 and
10-29 (`daemon.log:51435`, `:101034` area) — so the kill paths themselves work
when reached; these windows were either never reached or their output was lost
with the timeout.

**The precise fix location (proposed, NOT implemented):**
`hscc_daemon/health.py:715-727` `_proxy_needs_restart` — widen it to treat an
**unreachable or timed-out** probe as restart-worthy when a listener exists or
sparkrun tracks a live PID (the alive-but-not-serving shape), gated on the
same lease/breaker already at `health.py:1470-1489` so it cannot thrash. Second
location: `health.py:1180-1182` — verification must accept
`owner == tracked_after` **or** `owner is a descendant of tracked_after and
owner's cmdline is litellm` (cmdline vetting already exists at
`health.py:1113-1117` and `:1237-1242`). Third: `health.py:1590-1594` /
`health.py:1428` — the streak must not be cleared by a healthy tick that
occurs *between* failed attempts of the same incident, or the escalation must
also trigger on "N failed attempts within a window" rather than strictly
consecutive ticks.

### (c) Why sparkrun shell-outs time out during episodes

**Verdict: CONFIRMED as a symptom of host/fleet-wide resource starvation, and
REFUTED as an independent fault — and specifically REFUTED as caused by the
proxy.** "FLEET VISIBILITY LOST" does **not** probe :4000, so the card's
hypothesis ("if both probe :4000 they stall with the proxy") is false on the
code:

- Structured path: `health.py:132-148` `_SPARKRUN_STATUS_SCRIPT` →
  `api.status(hosts, ssh_kwargs=…)` →
  `/Users/desac/sparkrun/src/sparkrun/api/_status.py:22-70` →
  `query_status_for_cluster` — an **SSH sweep of the DGX nodes** (docker +
  local executors). `grep -n "4000|proxy" api/_status.py` → **no match**. No
  :4000 traffic. `run_cmd(..., timeout=25)` (`health.py:221`).
- Legacy shell-out: `sparkrun status` → `cli/__init__.py:142-160` → `ctx.invoke(
  cluster_status, …)` — the same SSH sweep. `run_cmd(..., timeout=10)`
  (`health.py:285`).
- Only `sparkrun proxy status` touches the proxy, and it is a *different*
  shell-out: `cli/_proxy.py:237-273` → `api/proxy/_ops.py:496-524` →
  `engine.list_models_via_api()` → `GET http://localhost:4000/model/info`
  with `timeout=10` (`proxy/engine.py:459-478`, `:570-581`). `proxy status`
  deliberately survives that failure (`_ops.py:820-828`, `model_query_error`)
  instead of raising — which is why `status.txt` still printed the full 12-model
  table at 15:54 while the daemon's own probe was failing 13 h earlier.

So: both status paths time out for the **same shared reason that is not the
proxy**. The reasons available in evidence, none yet resolved:
(1) the sweep needs SSH to 4 nodes and windows A/B coincide with the daemon's
NAS probe timing out once (`daemon.log:159004` `[2026-10-30T23:33:09Z] NAS
check: ok=False NAS probe timed out after 5s` — the only NAS timeout in the
log); (2) host-level starvation (the same minute shows a 90 s sparkrun start
timeout); (3) sparkrun-side state/cache contention — **speculative**, the
running snapshot write is a plain atomic JSON file, not SQLite
(`orchestration/job_metadata.py:765-801`), so my earlier lock-contention idea
has no support and is withdrawn. **UNPROVEN which of (1)/(2) it was** — needs
one capture of `sparkrun status --json` wall time plus the daemon's own CPU
during an episode. Note the fallback WARN `structured sparkrun status
unavailable — falling back to \`sparkrun status\` text-parse
(venv_py='/Users/desac/sparkrun/.venv-sparkrun-041/bin/python')` appears ~35×
in the episodes and the venv **resolved fine** (`health.py:151-164`), so the
`[Errno 2]`-class failure of t_b543e530 is **not** what happened here: the
interpreter was found and the SSH sweep is what failed.

**Consequence for the operator:** "FLEET VISIBILITY LOST" during a proxy stall
is **correlated noise, not a second fault**. It should not be escalated as an
independent alarm, and it is not evidence that the fleet is down —
`Workers check: 3/3 online` and `Gateway check: ok=True … 48 profiles`
continue *through* the episodes (`daemon.log` 158961, 158979 and neighbours).

---

## 3. Proposed fixes (FIX-1..N) — for the orchestrator to file

Each is one atomic change. `health.py` = `hscc_daemon/health.py` in the `hscc`
repo at `origin/main` `928e1aa3`. `sparkrun` paths are **UPSTREAM PROPOSALS ONLY**
(operator-owned fork; this card touched nothing there).

| # | Repo / location | Atomic change | Acceptance (one sentence) | Risk |
|---|---|---|---|---|
| **FIX-1** | hscc `health.py:715-727` `_proxy_needs_restart` | Treat a probe that **timed out or was refused**, while a listener exists or sparkrun tracks a live PID, as restart-worthy (so the relaunch carries `--restart`) instead of only an answered-empty-catalogue probe. | A probe timeout against a live-but-listener-less proxy produces a `proxy start --restart` and the proxy is serving again within one cooldown window, pinned by a unit test with a stubbed timeout probe + live `_proxy_listener`. | Medium — restarts a proxy mid-drain, dropping in-flight streams; must stay behind the existing heal lease + F5 breaker (`health.py:1470-1489`). |
| **FIX-2** | hscc `health.py:1180-1182` (verification in `_proxy_repair_orphan`) | Accept verification when `owner == tracked_after` **or** `owner`'s cmdline is litellm **and** `owner` is a child/descendant of `tracked_after`. | An escalated repair that leaves a healthy uvx-launched proxy on :4000 logs `proxy repaired on :4000` instead of `UNVERIFIED` — currently impossible (`grep -c "proxy repaired on" ~/.hscc/daemon.log` = 0). | Low — widens who may be called "repaired"; keeps cmdline vetting as the safety gate. |
| **FIX-3** | hscc `health.py:1590-1594` + `:1428-1433` (streak lifecycle) | Stop an intermittent healthy tick from resetting the failure ladder for the *same* incident (window-relative failure count in addition to strict consecutives). | A stall that recovers for <1 cooldown window and returns still reaches escalation on its next episode instead of restarting at "attempt 1 of 3" (observed: 17× attempt-1 vs 0× attempt-3). | Medium — more escalations, i.e. more kills; must remain gated by lease/breaker and the stall-alert throttle (`health.py:786-787`). |
| **FIX-4** | sparkrun **UPSTREAM** + hscc config | Surface the balancer's own counters (in-flight map size, `_held`/`_tried` cardinality, lock-wait time) via an endpoint or periodic log line so hypothesis (a) becomes measurable. | Next stall episode yields a timestamped balancer snapshot from the live proxy, letting (a) be answered with data instead of reasoning. | Low — observability only; but it is an upstream edit the operator owns, so file it, don't patch it. |
| **FIX-5** | sparkrun config (`proxy.yaml` → generated `litellm_config.yaml`) | Reconcile intent: `litellm_config.yaml` currently ships `routing_strategy: simple-shuffle` with `sparkrun_balancer.handler` loaded **only as a callback**, so the least-in-flight placement decision is computed and discarded. Decide one: make the balancer the strategy, or stop loading it. | `sparkrun proxy status` / the generated config show routing strategy and loaded callbacks that agree with `proxy.yaml` intent. | Medium — changes routing behaviour on a live fleet; needs the operator's decision (relates to t_882b3abd, §5). |
| **FIX-6** | hscc `health.py:1167` (and the 90 s cap it uses) | Bound the `proxy start` shell-out so the repair cannot be swallowed whole by a discovery sweep it does not need (shorter cap, and/or a `--no-discover`/`--skip-discovery` start once the port is already known free after the reap). | A stalled escalation reports its own kill/reap verdict instead of `Command timed out after 90s` (23:34:12Z, 23:44:59Z). | Low-medium — a start without a sweep risks a stale model list; must be paired with the drift check that already runs next tick. |
| **FIX-7** | sparkrun **UPSTREAM** | Have `sparkrun status`/`api.status` report per-host sweep latency + failure reason in `to_dict()` so the daemon's "both paths unavailable" WARN can say *why* (SSH unreachable vs host starvation). | `FLEET VISIBILITY LOST` lines name the failing host and the elapsed sweep time. | Low — additive to a diagnostic path. |
| **FIX-8** | hscc `health.py:238-241` (the `FLEET VISIBILITY LOST` log) | Demote/annotate it during a known proxy-repair window, or key it on the sweep's own host errors, so it stops reading as an independent fault while a proxy heal is in flight (see (c)). | During a proxy stall, the DGX stream still reports DEGRADED but does not assert "sparkrun unreachable" when the sweep's hosts were individually reachable. | Low — wording/annotation only; must not hide a genuine fleet blackout. |

FIX-1..FIX-3 are the daemon-repair escalation set the card asked for; FIX-4/FIX-7
are the sparkrun-side upstream proposals; FIX-5 is the config knob; FIX-6 is the
timeout knob. No code was changed by this card.

---

## 4. UNPROVEN, and what to capture if it recurs

> **Status update (2026-11-01, card `t_c95a2137`):** items **1**, **2** and **5**
> below have since been addressed by §6 — item 1 (the blocking component) is
> answered with a named mechanism and a refined capture recipe at §6.5; item 2
> (the poller's identity) is resolved as client-side Ollama-dialect probing by
> Hermes processes, with `/props` + `/version` still unattributed (§6.3); item 5
> (the 15:55 SIGTERM-ignore mechanism) is given a mechanism fully consistent
> with the operator's testimony — uvicorn's unbounded graceful drain over
> never-finishing streams (§6.4, shape 2) — though the `kill -9` itself remains
> operator testimony. Read §6 before treating any item here as open. Items 3, 4
> and 6 remain as written.

Explicitly unresolved, in priority order:

1. **(a) — the actual blocking component.** Nothing in `litellm.log` times
   anything: uvicorn access lines carry **no timestamp** (`litellm.log` has 41
   non-`INFO:` lines, all banner/uvicorn-lifecycle; zero timestamps anywhere in
   the file). Without per-request timing the 24 s/16 s operator observations
   cannot be attributed to a component. **Capture next time:** run the proxy
   with uvicorn access-log format including `%({...}t)` / request start time
   (or LiteLLM's request-id + duration logging) and keep `py-spy dump --pid
   <real server pid>` (not the wrapper) three times, 10 s apart, *during* the
   stall; plus `curl -w '%{time_total} %{time_starttransfer}\n' -o /dev/null -s
   http://localhost:4000/v1/models` every 5 s so the stall's shape is measured
   rather than inferred.
2. **Who the 5-path poller is.** 1,407 occurrences each of `/version`,
   `/props`, `/api/tags`, `/api/v1/models`, `/v1/props` (all 404) is a
   steady external heartbeat; the only `/api/tags` probe in this repo is
   `health.py:1661` (`check_local`, 11434-targeted, one path). The fan-out
   source is unidentified — and if it is a *worker-side* poller, its cadence
   tells us about worker-side proxy access we currently can't see.
3. **Why the escalated repairs in window A failed** (23:29:07Z → 23:34:12Z
   `Command timed out after 90s`, and 23:41:04Z → 23:44:59Z `UNVERIFIED …
   sparkrun tracks 46269`). The daemon logged no kill/reap line for them.
   **Capture:** `ps -o pid,ppid,command -g $(pgrep -f litellm)` plus
   `lsof -nP -iTCP:4000 -sTCP:LISTEN` immediately before and after a repair.
4. **Why the SSH status sweep timed out.** Only evidence is a single 5 s NAS
   probe timeout in the same minute and a 90 s start timeout. **Capture:**
   `sparkrun status --json` wall time + `ping`/`ssh -o ConnectTimeout=5` to each
   node, run during an episode.
5. **The 15:55 SIGTERM-ignore.** Operator testimony; the daemon logged nothing
   (it was not the actor). The mechanism — uvicorn's graceful drain waiting on
   the 4 loopback ESTABLISHED sockets to :4000 seen in `tcp_states.txt` — is
   plausible and **UNPROVEN**. Relevant to FIX-1: `--restart` may hit the same
   20 s wall.
6. **Whether `simple-shuffle` + balancer-callback was intended** (FIX-5) —
   needs `~/.config/sparkrun/proxy.yaml`, which this card did not read.

Also worth recording: the earlier `tcp_states` reading on the 15:54 capture had
**31** sockets total with **1 LISTEN**, which is the shape of a *working* proxy.
Any future capture that finds `0 LISTEN` while a litellm process is alive is a
different state and should be captured with `lsof -p <pid>` open-file counts
and a `py-spy dump` — that combination distinguishes "closed its listener while
draining" from "wedged before accept".

---

## 5. Related context (do not re-invent)

- **t_882b3abd** — *DESIGN NOTE: real load-aware routing for the proxy — custom
  litellm strategy on vLLM gauges vs per-deployment caps/weights vs hybrid.*
  Board status **blocked** (`needs_input`), one run, superseded by the operator
  on 2026-10-15: the operator self-assigned **both** halves (the sparkrun
  load-aware patch on `feat/proxy-load-aware`, own worktree `~/dev/sparkrun-lb`,
  and the sparkroute least-busy side), and the investigation was stopped **by
  request**. This is the card behind `sparkrun_balancer.py` (`STRATEGY =
  "least-in-flight"`, `STALE_AFTER_S = 900`, `FAULT_AVOID_S = 10`) and its
  module docstring explains the defect it replaces (LiteLLM's `least-busy`
  counter is only incremented from a sync hook `Router.acompletion` never
  calls, so every deployment reads 0). Consequence for this audit: the balancer
  is **operator-owned, deliberately-placed code with a live design decision on
  it** — FIX-4 (observability) and FIX-5 (config reconciliation) must be routed
  through that owner, and any "just remove the lock" idea would re-litigate a
  blocked design card.
- **t_43b6e245** — earlier proxy P0 (the hollow-PID / loud-failure work). Its
  shipped surface is still in the tree and its markers are all visible in
  `health.py`: the `HOLLOW` kill path (`_proxy_kill_hollow_tracked`,
  `health.py:1205-1275`), the escalating alert (`_proxy_stall_alert`,
  `health.py:1296-1323`), the durable streak (`_proxy_relaunch_streak`,
  `health.py:798-813`), and the surfaced command stderr (`health.py:1550-1557`).
  **It covered:** making the failure loud and giving the daemon a way to retire
  a hollow tracked PID. **It missed:** (i) the decision rule that decides
  *when* to pass `--restart` still excludes the timeout shape (b1), (ii) the
  verification predicate assumes PID equality that a uvx launch can never
  satisfy (b3), (iii) strict-consecutive streak arithmetic lets an intermittent
  healthy tick reset the ladder (b2). FIX-1/2/3 are exactly those three gaps —
  the escalation machinery exists and works when reached (proven by the 10-27
  and 10-29 `HOLLOW`/`reaping ORPHAN` lines); the gating logic above it does
  not reach it during these episodes.


---

## 6. Question (a) — follow-up resolution (card `t_c95a2137`)

Added 2026-11-01 by `t_c95a2137` (follow-up to §2(a), which ended `UNPROVEN`).
Scope unchanged: **read-only investigation; no fix applied; no live process,
config or sparkrun file touched.** LiteLLM paths below are the *installed*
1.82.6 tree the proxy actually runs —
`~/.cache/uv/archive-v0/6RVy63Hl0GRkTZTr/lib/python3.12/site-packages/`
(confirmed `litellm-1.82.6.dist-info` in that tree). §2(a) is not re-derived
here, only what it left open.

### 6.1 THE VERDICT

**Named component: LiteLLM's generic per-request retry state machine —
`Router.async_function_with_retries`, the `await asyncio.sleep(retry_after)` at
`litellm/router.py:5664` (and again at `:5729` inside the retry loop), fed by
`Router._time_to_sleep_before_retry` (`:5915-5962`) →
`litellm._calculate_retry_after` (`litellm/utils.py:6709-6732`).**

That is the only component in the installed 1.82.6 per-request path that is
simultaneously (i) **per-request**, so the serving loop keeps completing every
other request exactly as §2(a) proved it does; (ii) able to consume tens of
seconds; and (iii) **provably silent in this capture** (§6.2). Every other
candidate is refuted outright or demoted to a contributor; §6.4 verdicts each
one, per failure shape, as the orchestrator asked.

The arithmetic, from the defaults actually in force here:

```
INITIAL_RETRY_DELAY = 0.5   MAX_RETRY_DELAY = 8.0   JITTER = 0.75   (litellm/constants.py:332-334)
Router(retry_after=0)                                                (router.py:271, :515)
num_retries default = openai.DEFAULT_MAX_RETRIES = 2                 (router.py:501-503; openai/_constants.py:8)
sleep = INITIAL_RETRY_DELAY * 2**(num_retries - remaining_retries)   (utils.py:6726)
        boxed to [min_timeout = retry_after = 0, MAX_RETRY_DELAY = 8.0], + JITTER*random()
```

Ordinary exponential backoff on its own tops out at roughly `0.5 + 1 + 2`
≈ **3.5 s plus ≤2.25 s jitter — about 5.7 s**, i.e. it cannot by itself reach
16–24 s. Three installed-and-configured factors close that gap:

1. **A backend `Retry-After` header is obeyed verbatim, up to 60 s.** If the
   failing response carries `Retry-After: n` with `0 < n ≤ 60`,
   `_calculate_retry_after` returns `n + jitter` and skips the exponential
   branch entirely (`utils.py:6715`, `:6721-6722`). A single 15–20 s header on
   one attempt lands exactly in the observed 16–24 s band. The 8.0 s
   `MAX_RETRY_DELAY` cap applies only to the *computed* branch, not the header
   branch.
2. **The OpenAI SDK's own retries are layered underneath.**
   `OpenAIChatCompletion._get_openai_client` forwards `max_retries` into
   `AsyncOpenAI(...)` (`llms/openai/openai.py:391-392`), default
   `DEFAULT_MAX_RETRIES = 2`, so each router attempt can itself contain up to
   two SDK-retried upstream requests. Router attempts × SDK attempts multiply
   the elapsed budget.
3. **The upstream call has no response timeout at all.**
   `_get_openai_client(…, timeout: Union[float, httpx.Timeout] = httpx.Timeout(None))`
   (`llms/openai/openai.py:359`), and `litellm_config.yaml` sets **no**
   `request_timeout`/`timeout` key — so `litellm.request_timeout` stays `None`
   (`router.py:512`) and `self.timeout` is `None`. A wedged vLLM worker is held
   open indefinitely rather than cut off, so a *slow-then-succeed* attempt costs
   whatever the backend takes, not 5 s.

**Why the loop keeps flying while one request burns 24 s:** `asyncio.sleep`
yields to the loop, so every other request — the poller fan, `/v1/models`, other
completions — is dispatched normally throughout. That is precisely the signature
§2(a) measured (poller never gaps > 46 lines), which is why "the loop blocked"
was refuted and why a per-request *await* is the only surviving shape. The
named component is the one await on that path that is both long and silent.

### 6.2 The silence is proven, not assumed: `litellm.log` cannot contain LiteLLM's own lines

§4 item 1 recorded "zero timestamps" as a limitation. The real constraint is
stronger, and it changes what the file can be asked:

- `litellm.log` holds **17,376 uvicorn access lines + 41 non-`INFO:` lines**, and
  those 41 are only the uvicorn banner and the `uvx` package-install preamble
  (`Downloading botocore…`, `Installed 103 packages in 1.55s`, `Started server
  process [91757]`).
- **Zero** lines come from LiteLLM's own loggers (`verbose_router_logger`,
  `verbose_proxy_logger`). A 14 h file covering retries, cooldown churn and
  router decisions produced not one line from the component named in §6.1.
- Mechanism: `uvx --from litellm[proxy]==1.82.6 litellm --config … --host …
  --port …` is exactly the argv sparkrun builds
  (`sparkrun/src/sparkrun/plugins/litellm/engine.py:548-569`) — **no
  `--detailed_debug`, no `--debug`, no log-level flag** — and the output is
  captured to `litellm.log` (`engine.py:405` `log_name = "litellm.log"`). The
  router's decision lines are `debug`-level (`router.py:5652-5654`
  `Retrying request with num_retries: …`), so at the default level they are
  emitted nowhere.
- **Consequence for FIX-4:** the retry path's observability must be *added*; it
  cannot be recovered by reading the existing capture harder.
- Second, subtler correction: uvicorn writes the access line when the response
  **headers** are sent — `self.access_logger.info(...)` sits inside the
  `http.response.start` branch of `send()`
  (`uvicorn/protocols/http/httptools_impl.py:463-488`) — so the line marks
  **TTFB, not stream completion**. §2(a)'s "uvicorn writes one access line at
  response time" is true but narrower than it sounds: the log is blind to a
  stream that stays open long after its line was written. §6.3(3) uses that
  blindness deliberately.

### 6.3 New from the capture: per-connection analysis (unused until now)

Access lines carry the client's ephemeral source port, which identifies the
HTTP/1.1 connection. §2 used only path-gap analysis. Re-running the capture
through the two scripts that ship beside this doc — `docs/audits/t_c95a2137_analyze_access.py`
and `docs/audits/t_c95a2137_shape_split.py` (read-only over the capture, report to stdout):

| Path | lines | distinct client ports | max reqs/conn | conns with >1 |
|---|---|---|---|---|
| `/v1/chat/completions` | 4821 | 4174 | 4 | 582 |
| `/v1/models` | 2614 | 2446 | 3 | 160 |
| `/v1/model/info` | 2461 | 2304 | 3 | 155 |
| `/api/tags` | 1407 | 1357 | 2 | 50 |
| `/api/v1/models` | 1407 | 1352 | 2 | 55 |
| `/props` | 1362 | 1314 | 2 | 48 |
| `/api/show` | 232 | 232 | 1 | 0 |

**8513 distinct client ports across 14 h** — essentially one connection per
request. Three consequences:

1. **The "5-path Ollama-dialect poller" is not a separate daemon.** `699` of the
   ports that issued poller paths also issued non-poller paths, and the
   surviving poller connections at the tail carry `/v1/chat/completions`,
   `/v1/models` and `/v1/model/info` interleaved with the fan-out. It is
   **ordinary clients probing a LiteLLM endpoint as if it were Ollama/LM Studio**
   — Hermes' local-model probing is the closest match in this codebase
   (`hermes_cli/models_local.py:246` `/api/tags`, `:393` `/api/v1/models`,
   `:606` `/api/show`), which explains `/api/tags`, `/api/v1/models` and
   `/api/show`; **`/props` and `/version` remain UNPROVEN as to origin** (no
   matching literal in Hermes, hscc or sparkrun). §4 item 2 therefore closes as
   "the agents themselves, plus two unattributed paths", not "an external
   heartbeat".
2. **Pool-exhaustion pressure is bounded and low.** Fresh-connection-per-request
   client behaviour means the gateway's *upstream* pool is the one that matters.
   Note the transport actually in force: LiteLLM 1.82.6 defaults to the
   **aiohttp** transport (`llms/custom_httpx/http_handler.py:770-794`
   `_should_use_aiohttp_transport` returns True unless
   `litellm.disable_aiohttp_transport`/`DISABLE_AIOHTTP_TRANSPORT`; the config
   sets neither), and `AiohttpTransport` builds a bare `ClientSession()`
   (`aiohttp_transport.py:179`, `:189`, `:221`, `:228`) with **no connector
   override** → aiohttp defaults `limit=100`, `limit_per_host=0`
   (`aiohttp/connector.py:287-288`, `:317-318`), i.e. **100 simultaneous
   upstream connections shared across all 12 deployments**, and
   `timeout…connect=timeout.get("pool")` (`aiohttp_transport.py:274-278`) with
   the pool timeout `None` from `Timeout(None)` → **an unbounded, silent wait for
   a free connection**. This is the second-ranked candidate (§6.4); the
   `httpx` `DEFAULT_LIMITS = Limits(max_connections=100, max_keepalive_connections=20)`
   (`httpx/_config.py:247`) is *not* the in-force limit, and
   `AsyncHTTPHandler.__init__(concurrent_limit=None)  # …ignored (no limits)`
   (`http_handler.py:350`) confirms no litellm-side cap is applied either.
3. **The four loopback ESTABLISHED sockets are the freshest connections in the
   capture.** Each peer port in `tcp_states.txt` appears in the access log and
   its *last* line is a `POST /v1/chat/completions 200`: `63486` line 17127
   (294 lines before EOF), `64995` line 17210 (211), `65156` line 17225 (196),
   `51164` line **17420 — the last logged request in the whole file**. Capture
   density is ~1240 lines/h (17,376 access lines / 14 h), so the first three
   logged their `200` roughly 9–14 minutes before the 15:54 capture and were
   still ESTABLISHED, and `51164` was seconds old. Two readings are possible and
   **the capture cannot separate them**: (a) the stream was still streaming —
   headers logged at TTFB (§6.2), body still open, the natural shape for a
   multi-thousand-token reasoning generation on a loaded node; or (b) uvicorn's
   `timeout_keep_alive = 5` (`uvicorn/config.py:234`) was mid-drain on an idle
   keep-alive. (b) cannot account for 9–14 minutes, so (a) is the better
   explanation. Note `51164` and `65156` each also appear much earlier (lines
   10023, 9889): those are **recycled ephemeral ports, not the same 6-hour-old
   connection** — the 5 s keep-alive makes a persistent connection that long
   implausible — so no age claim is made from them. **In either reading these are
   client-held connections open past their logged response, which is exactly the
   population a graceful drain waits on** (§6.4, shape 2). The stream state at
   15:54 is **UNPROVEN**; closing it needs `lsof` plus per-connection byte
   counters (`nettop` / `ss -ti`) during an episode.

**Holder identity, captured live (the capture's own open question).** The same
fan-out is still running today, so identity is directly observable: a 0.2 s ×
400-iteration `lsof -nP -iTCP:4000` sample plus per-PID `ps` resolves every
long-lived holder, all `python3.1`:

```
pid 97465   hermes -p hscc-orch chat --continue hscc           (etime 9-15h)
pid 19621   hermes -p ecofire-app-orch chat --yolo             (etime 1-07h)
pid 62953 / 47926 / 54077 / 44892 / 14974   hermes_cli.main -p backend-engineer --cli
pid 85565 / 10048   hermes_cli.main -p ml-engineer --cli
pid 44895 / 91971   hermes_cli.main -p qa --cli
pid 74857   hermes_cli.main gateway run --replace
```

**Every long-lived connection to :4000 is a Hermes process** — worker CLIs,
orchestrator chats, the gateway. The "4 loopback ESTABLISHED to :4000" of
`tcp_states.txt` are a rotating subset of those, each holding its socket for the
duration of a generation. §4 item 2 closes on a live observation rather than an
inference. (The live proxy is pid 29244, a *different* launch from the audited
91693/91757 pair — expected, per §1.3's operator restart.)

### 6.4 Candidate verdicts, split by failure shape

Shape **1** = requests slow **while a listener answers** (windows A, B, C — the
daemon's `timed out` mode, `health.py:1378`, `:519-543`). Shape **2** = **no
listener at all** (`[Errno 61] Connection refused` — window A from 23:46:00Z and
the two ticks at 13:54:21Z / 13:55:24Z).

| Candidate | Shape 1 — slow, listener answers | Shape 2 — no listener |
|---|---|---|
| **Router retry sleep** `router.py:5664`, `:5729` + `_calculate_retry_after` | **PRIMARY.** Mechanism proven present, reachable and silent; per-request; loop keeps serving. Trigger and magnitude **UNPROVEN** (§6.5). | **NOT APPLICABLE.** With no listener there is no request path to delay. Cannot cause shape 2. |
| **aiohttp upstream-pool wait** (`ClientSession()` → `limit=100`, pool timeout `None`) | **SECONDARY / plausible contributor.** Silent and per-request, and unbounded. Demoted because `tcp_states.txt` shows only **14** proxy→backend ESTABLISHED sockets at capture, far below 100, and no capture exists from a shape-1 window; never demonstrated, and shape-1 concurrency is **UNPROVEN**. | **NOT APPLICABLE** (same reason). |
| **Balancer `threading.Lock`** (`sparkrun_balancer.py:151`) | **NOT SUPPORTED — refuted as the primary mechanism** (four grounds below); remains a *contributor* only until FIX-4 supplies map cardinality. | **REFUTED.** The process is not serving anything in shape 2; its lock is irrelevant. |
| **autodiscover reload / router rebuild** | **REFUTED** (unchanged from §2(a)): 1,586 clean sweep pairs, **0** matches for `reconciled\|restart\|error`; it is a separate process and cannot touch the gateway's loop. | **NOT A CANDIDATE.** Zero restarts occurred, so no rebind happened. |
| **Event-loop block / GC / process freeze** | **REFUTED** (unchanged from §2(a)): the poller group never gaps > 46 lines across 17,376 lines; a loop-blocking 5 s call would freeze all paths simultaneously. | **REFUTED, and reclassified** — see the shape-2 correction below: shape 2 is not a wedge at all. |
| **`uvx` + litellm 1.82.6 (version / packaging)** | **NOT SUPPORTED as a root cause.** The version is only the *carrier* of the in-force defaults (aiohttp transport, `Timeout(None)`, `MAX_RETRY_DELAY = 8.0`, `num_retries = 2`). No 1.82.6-specific defect is evidenced. | **CONTRIBUTOR (packaging).** `uvx` spawns wrapper 91693 over real server 91757 (`ps.txt`), so a stop aimed at the tracked PID need not reach the server — the same root as FIX-2/b3, not a latency cause. |

**The balancer refutation, stated properly** (the card asked for proof or
refutation):

1. **Structural — it cannot produce shape 1.** A `threading.Lock` taken from
   `async def` hooks (`:282-315`, calling `pick()` synchronously at `:293`) is
   acquired **on the event-loop thread**. When contended, `acquire()` blocks the
   *whole loop*, which would stall every path at once and register as a
   simultaneous gap in all of them — the exact signature §2(a) refuted
   (poller-group max gap 46 lines, `poller gaps > 300 lines: 0`). Contending
   this lock produces shape "loop blocked", which does not exist here.
2. **No blocking work inside the critical sections.** Every `_lock` region
   (`:168`, `:174`, `:223`, `:247`, `:255`, `:275`) is dict arithmetic plus
   `_expire_locked()` (`:206-215`), bounded by `O(_held + _tried + _faulted)`;
   `_clock` is `time.monotonic` (`:146`). No I/O, no await, no syscall.
3. **The pathological-map-size precondition is unobserved and implausible.**
   Contention needs a huge map or extreme acquire concurrency; with ~1 request in
   flight per client connection (§6.3) and `_stale_after = 900` pruning, the maps
   are small. **Stated honestly as an inference:** `_held`/`_tried` cardinality
   appears nowhere in the capture, so this ground is FIX-4's job, and until
   FIX-4 lands the correct label is "not supported", not "refuted with data".
4. **Its routing role is real — correcting §2(a).** §2(a) concluded the balancer's
   `pick()` result "is *not* what routes". In installed 1.82.6 that is wrong:
   `Router.async_get_healthy_deployments` (`:8949`) calls
   `async_callback_filter_deployments` (`:9010` → `:6498-6523`), which iterates
   `litellm.callbacks` and reassigns
   `returned_healthy_deployments = await _callback.async_filter_deployments(...)`.
   The balancer returns exactly one deployment (`:293`), so **its choice *is*
   the placement** and `routing_strategy: simple-shuffle` is effectively dead
   config for these requests. This strengthens FIX-5's premise (a real
   intent/behaviour mismatch) while leaving the latency verdict unchanged by
   grounds 1–3.

**Contributor worth its own fix card — the cooldown gate.** Before the balancer
runs, `async_get_healthy_deployments` filters on
`_async_get_cooldown_deployments` (`:9000-9006` →
`router_utils/cooldown_handlers.py:323-335`), and cooldown defaults to **5 s per
deployment** (`constants.py:43`). One flaky backend can therefore coarsely shove
traffic onto the other three nodes for 5 s windows — a plausible *amplifier* of
a single slow node, with a bounded per-request contribution. It is not the named
delay.

**Shape-2 correction — `0 LISTEN` is expected behaviour, not a wedge.** Two
findings reframe shape 2 entirely:

- The card's premise "tcp capture 15:54: 0 LISTEN" is **false**:
  `tcp_states.txt` line 1 reads `1 (LISTEN) *:4000`. (The parent already flagged
  this; restated because the dedupe comment re-imports the 0-LISTEN framing.)
- With uvicorn's default `timeout_graceful_shutdown = None`
  (`uvicorn/config.py:236`, `:285`) — sparkrun passes no such flag
  (`engine.py:548-569`) — `Server.shutdown()` closes every listening socket
  first, then `await asyncio.wait_for(self._wait_tasks_to_complete(),
  timeout=self.config.timeout_graceful_shutdown)` (`uvicorn/server.py:281-300`)
  blocks **forever** when the timeout is `None`. So the operator's testimony
  (§1.3: `proxy stop` reported success, process alive with no listener, SIGTERM
  ignored for 20 s, `kill -9` required) is the *predicted* behaviour of a
  graceful drain over never-finishing streams — and §6.3(3) is the population it
  was waiting on. **Shape 2 is therefore not a LiteLLM request-path defect at
  all**: it is a shutdown-configuration gap (no `--timeout-graceful-shutdown`)
  compounded by the uvx wrapper-PID indirection. That puts it with FIX-1/FIX-2,
  not with FIX-4/FIX-5.

**Scope of the verdict — read this before citing §6.1.** The named component
lives on the **completion** path (`router.acompletion` →
`async_function_with_retries`). The daemon's recorded failure is
`urllib.urlopen("http://localhost:4000/v1/models", timeout=5)`
(`health.py:1378`, `:519-543`), and `/v1/models` does **not** traverse
`acompletion` — so §6.1 explains the *operator-measured* 16–24 s on
`/v1/chat/completions` (the shape the operator timed) and does **not** by itself
explain a 5 s timeout on `/v1/models`. Two independent facts bear on that gap,
and both are worth the orchestrator's attention:

- **A request whose handler never finishes emits NO access line at all** — the
  line is written at `http.response.start` (§6.2), so a hung `/v1/models` is
  *invisible* to the log rather than logged as a gap. §2(a)'s "the serving loop
  kept answering" therefore means "other paths kept answering", and is fully
  consistent with some `/v1/models` requests silently not answering. **The
  absence of `/v1/models` gaps in the log is not evidence that `/v1/models`
  never stalled** — it is only evidence that *whichever* requests stalled,
  unrelated traffic flowed around them.
- The `/v1/models` handler is served in-process with no upstream call, so a
  5 s timeout on it points at shared per-request machinery *before* routing —
  the FastAPI auth dependency `user_api_key_auth`
  (`proxy/auth/user_api_key_auth.py:1534`, builder `:519`), the `pre_call` hook
  chain, or an in-flight task backlog — rather than at
  `async_function_with_retries`. Note the captured `litellm_config.yaml` carries
  **no** `general_settings` block (`grep -c 'general_settings\|master_key'` → 0),
  which is consistent with the unauthenticated `/v1/models` **200**s in the log,
  and therefore makes a DB-backed key check an *unlikely* 5 s stall source —
  leaving in-handler/backlog the better explanation. Either way this is a
  **different candidate from §6.1 and is NOT resolved by this card**; the py-spy
  recipe of §6.5 item 3 distinguishes them, because it samples the *whole* task
  set rather than one handler.

### 6.5 What remains UNPROVEN, and the capture that closes it

The mechanism is named; **the trigger and the magnitude are not measured.**
Stated plainly: nothing in the capture times a single request, so 16–24 s is
still operator testimony, and the retry path is proven *reachable and silent*,
not proven *taken*. To close it — all read-only, none requiring a live config
edit:

1. **Turn on the retry path's own lines at the next launch** (`--detailed_debug`,
   the sanctioned switch) so `verbose_router_logger` emits
   `Retrying request with num_retries: N` (`router.py:5652-5654`). One episode
   then yields retry counts and each sleep directly. Size the log rotation first
   — this is why the capture is 17 k lines today.
2. **Timestamped access log** (`--access-log` plus a format carrying request
   start), or LiteLLM request-id + duration logging — §4 item 1's recipe, now
   annotated with the TTFB caveat of §6.2 so the line is not mistaken for stream
   completion.
3. **`py-spy dump` on the real server pid during a stall** — the child
   (`91757` in `ps.txt`), **not** the uvx wrapper (`91693`), ×3 ten seconds
   apart. A task parked in `asyncio.sleep` inside `async_function_with_retries`
   confirms §6.1; a task parked in `acquire()` on the balancer lock would
   falsify §6.4's refutation; a task parked in the aiohttp connector's wait
   promotes §6.4's secondary candidate. Either outcome closes the question,
   which is what makes this the highest-value probe.
4. **Look for `Retry-After` from the backends** — the cheapest route to 16–24 s
   (§6.1 factor 1). Capture the failing upstream's response headers once. If no
   backend ever emits it, the header branch dies and the sleep ceiling collapses
   to ~5.7 s, which would mean §6.1's component is **insufficient** and the
   search reopens with the aiohttp pool wait promoted.
5. **`ss -tan` / `lsof` counts *during* a shape-1 window** to test the
   `limit=100` hypothesis: ≥100 proxy→backend sockets queued near-simultaneously
   would confirm it, ~14 (as at 15:54) would refute it.
6. **Attribute `/props` and `/version`** — the two poller paths with no
   identified emitter. The live method in §6.3 (sample `lsof` when they fire,
   then `ps -o command= -p <pid>`) is sufficient; no new tooling needed.
7. **Balancer map cardinality — FIX-4.** Until it exists, ground 3 of the
   balancer refutation stays an inference.

### 6.6 One-line answer for the fix cards

Shape 1 (slow but answering) is **LiteLLM's per-request retry state machine
sleeping on a failed backend attempt — `router.py:5664`/`:5729` via
`_calculate_retry_after` — amplified by `Timeout(None)` (no upstream response
timeout), SDK-level `max_retries = 2`, and a 5 s per-deployment cooldown gate,
and rendered invisible by a launch configuration that emits none of LiteLLM's
own log lines.** Shape 2 (no listener) is **not a stall at all**: uvicorn's
unbounded graceful drain over never-ending streams plus uvx wrapper-PID
indirection — a shutdown-config gap, not a request-path defect. The two shapes
have different causes and different owners (FIX-4/FIX-5-adjacent versus
FIX-1/FIX-2-adjacent) and must not be fixed by one card.

*New evidence in this section: installed-source citations under
`~/.cache/uv/archive-v0/6RVy63Hl0GRkTZTr/lib/python3.12/site-packages/` for
`litellm` (1.82.6), `uvicorn`, `httpx`, `httpcore`, `aiohttp` and `openai`;
per-connection analysis of `litellm.log` (17,376 access lines, 8513 client
ports); the port→PID correlation of the four `tcp_states.txt` holders; and a
live 0.2 s × 400 socket sample identifying every long-lived :4000 holder as a
Hermes process. Analysis scripts: `docs/audits/t_c95a2137_analyze_access.py`,
`docs/audits/t_c95a2137_shape_split.py`.*
