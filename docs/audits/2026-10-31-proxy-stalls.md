# LiteLLM proxy :4000 stall investigation — 2026-10-30/31

Card: `t_447dd0aa`. Scope: **investigation + audit only; no fixes were applied.**
Three stall episodes affecting the sparkrun LiteLLM gateway on port 4000 on the
Mac (the sparkrun host), plus the daemon's failed repair and the "FLEET
VISIBILITY LOST" sparkrun shell-out timeouts.

**BANKED STATE (operator directive 19:5x local, 2026-10-31).** This document is a
mid-run bank, not a finished audit. Sections 1–5 hold what is actually backed by
a timestamped log line or a `file:line` code citation; every claim that is not
so backed carries an explicit `UNPROVEN` marker. Section 6 lists what remains
open per question, and follow-up cards exist for each (ids in the card result).

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
