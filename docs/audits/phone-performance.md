# HSCC Phone Performance Audit

Task: t_14a2958b — "Performance: what makes the app feel slow on a phone"

Status: EXECUTING. Findings below are evidence-backed (file:line + live measurement
commands). Source tree = this worktree (branch t_14a2958b off dev).

Repro commands:
  python3 scripts/api_route_sweep.py             # single-pass reachability + elapsed
  python3 scripts/perf_measure.py --passes 5     # NEW: 5-pass median/p95 latency table

Both tools share ONE set of probe rules, and that set lives in
`hscc_daemon/api_route_sweep.py` — read its tables (`NOT_SWEPT`,
`NOT_SWEPT_STATUS`, `HEAVY_ROUTES`, `PROBE_TIMEOUT`, `NEEDS_PARAM`,
`MUTATION_SUFFIXES`) rather than re-deriving them here. Three of them change
what the second command fires, so the latency table in section 1 is not what a
modern run prints. The rules are PR #485 (`80146fb4`, the sweep) and PR #490
(`918caded`, which merges #485 and makes `perf_measure.py` import them instead
of keeping a second copy); both were still OPEN when this note was written
(2026-11-20), so `main`'s copy of the script still does the old thing:

  * `/v1/verify` is NEVER fired. Its handler IS `hscc_daemon.verify.run_all()` —
    the fleet smoke test, doing real chat round-trips against the GPU units — so
    it now gets a `skip` row with no timings instead of five live smoke tests
    per run.
  * `/v1/health` gets a `curl -m 120` of its own (`HEAVY_ROUTES`) instead of the
    blanket `-m 30` (`PROBE_TIMEOUT`), because its cold start runs one inline
    verify — measured 64.4 s — which the 30 s cap cut off mid-request, ranking a
    route that was answering as if it were fast.
  * The POST-only `/stop` `/up` `/down` family (`MUTATION_SUFFIXES`) is excluded
    up front, not fired at and dropped after a 405.

---

## 1. Slowest endpoints (measured read-only, median of 5 passes, live API)

Command: `python3 scripts/perf_measure.py --passes 5`
Address derived dynamically (never hardcoded) from `hscc api status`.
Host masked as `***.*.*.*` (script prints no IP; repo has an AddressGuard).

### Where these numbers come from — read this before quoting them

This table is a verbatim paste of ONE run of the command above, taken at the
revision that introduced both this file and the script: `d26a7fb0` (2026-09-02,
branch `t_14a2958b`), which is still the ONLY revision of
`scripts/perf_measure.py` on `main` — nothing touched the script until PR #490.
That revision kept its own copy of the sweep's route discovery and probe loop:
`client_routes()` returned every literal `/v1/...` path in
`ios-app/Sources/HSCC/HSCCClient.swift` with no exclusions, and `measure()`
looped a plain `curl -s -m 30` once per pass.

So the "measured read-only" in the heading above was NOT true of the run that
produced this table. While it was timing, that run also:

  * fired `/v1/verify` 5 times — five fleet smoke tests doing real chat
    round-trips on the operator's GPU units, each cut off at 30 s as a
    *measurement* only, the verify behind it running on to completion;
  * fired the POST-only `/stop` `/up` `/down` routes and dropped them on the
    resulting 405;
  * timed `/v1/logs` without the `?source=daemon&limit=1` the app always sends,
    so its row was the latency of a 400 rejection, not of a read;
  * cut `/v1/health`'s cold start off at 30 s.

None of the six routes ranked here is one of those — they are read paths, so the
ranking is not invalidated. What is NOT safe is reading the table as a
measurement a modern run reproduces byte for byte: a current `--passes 5` fires
no verify at all, so it never loads the daemon or the GPU units the way this run
did while it was taking the timings. (That those five concurrent verifies made
the read paths slower is an inference, not a measured correction — the numbers
were never retaken without them.) Re-measure with the current script before
quoting any row as today's latency. The audit's conclusion does not depend on
the precision: `/v1/cluster/monitor`'s ~3 s is a subprocess-over-SSH cost
(section 1 root cause), an order of magnitude above everything else.

status   median    p95    max    route
--------------------------------------------------------------------------------
200        3.03s   3.03s   3.04s   /v1/cluster/monitor      <- dominant outlier
200        0.76s   0.76s   0.77s   /v1/review/queue
200        0.71s   0.71s   0.75s   /v1/cluster/hosts
200        0.58s   0.58s   0.59s   /v1/cluster/jobs
200        0.58s   0.58s   0.58s   /v1/cluster/status
200        0.25s   0.25s   0.27s   /v1/cluster/info
(everything else: <=0.1s; most <0.01s)

Cold-start straggler: /v1/autodown/status — median 0.02s BUT first call took
9.5s (api_route_sweep single pass) / 5.33s (perf_measure max). Repeat calls are
fast (config + watchdog read, likely cached). So autodown/status is fast after
the first hit, slow on first hit.

### Root cause of the 3s monitor
Route: hscc-api/routes_cluster.py:88-92 `_backing_cluster_monitor()` -> eng.cmd_monitor().
Implementation: hscc-cluster/hscc.py:184-202 `cmd_monitor()` runs
  `timeout 3 sparkrun cluster monitor --simple --json`
over SSH to the fleet and shells out via subprocess. The ~3.0s is the subprocess
collecting live CPU/RAM/GPU metrics across all nodes — the 3s `timeout 3` budget
is effectively always consumed (remote SSH round-trips). This is a real fleet-wide
SSH sweep, inherently costly.

### /v1/cluster/* subprocess shells
cmd_hosts (hscc.py:143-181) runs `sparkrun cluster list` + `sparkrun status`
subprocesses (~0.71s). cmd_jobs (hscc.py:205-207) runs `sparkrun cluster status`
(~0.58s). cmd_cluster_status (hscc.py:73) subprocess (~0.58s). cmd_info
(hscc.py:335) (~0.25s). These are spawn-SSH-command latencies, not app bugs.

---

## 2. Does the UI show progress, or look frozen?

Findings (source-reasoned — NO iOS runtime here; state plainly as such):
- AutodownView.swift:101-102 — .loading shows ProgressView (a spinner). Not frozen.
- SessionView, ActivityFeedView, FleetView — all show ProgressView/HSLoading on .loading.
- ClusterView.swift:110-113 — topology strip shows ProgressView while status is
  .loading with no value. So the 0.7s wait shows a spinner, not frozen.
- ApprovalsView/BoardHygieneView — HSLoading("Loading…") on loading.

So NO screen looks frozen during a read — every slow read surfaces a spinner.
The exception context: the Chat tab (orchestrator chat) legitimately waits 30-90s
for a model reply by design. StreamingChatView handles this well: a status banner
that always states the phase (idle / loading history / connecting / connected /
reconnecting / failed) + live token-by-token streaming with a caret, tool chips that
show per-tool duration, and a ConnectionPhase signal. So even the slowest surface
is honest about progress. The "slow" perception there is genuine server LLM work,
not a client bug.

---

## 3. Over-fetching / re-fetch-on-appearance / caching

### 3a. NO positive cache exists — only offline fallback (CONFIRMED, highest-confidence finding)
- HSCCClient.get (HSCCClient.swift:198-231, 240-270): ALWAYS performs
  `session.data(for: req)` — the network round-trip — on every call, then caches
  the response under StateCache for OFFLINE fallback only.
- StateCache (HSCCClient.swift:13-76) is a last-known-state store in UserDefaults;
  it is ONLY read on failure via Offline.load (LoadState.swift:133-143). There is
  NO TTL that would serve a cached response without hitting the network.
- Implication: every view appearance where a fetch fires = fresh network round-trip,
  even if the data is seconds old. No positive cache, no request coalescing/dedup.

### 3b. ClusterView fetches /v1/cluster/hosts and DISCARDS it (CONFIRMED — dead fetch)
- ClusterView.swift:33 `@State private var hosts`; :84-86 `loadHosts` fires
  `/v1/cluster/hosts` (0.71s).
- BUT `hosts` is NEVER referenced by any rendered view. The topology strip
  (topologyStrip :107, nodeState :166-181) and fleetStatusLine (:135) derive
  everything from `status` alone. Doc comment (:21-22) CLAIMS /v1/cluster/hosts
  "drives the topology", but code does not — the doc and code disagree.
- Effect: every first load of the Cluster tab fires a 0.71s network request
  whose response is thrown away. = "fetching more than it renders" (Q3 yes).

### 3c. Re-fetch-on-appearance — mostly GUARDED, one nuance
- Most screens guard .task with `value == nil && !isLoading` (ClusterView:53,
  Autodown:52, Sessions:44, ActivityFeed:38, FleetView:71, ProjectOverview:272).
  Within a tab's lifetime the view keeps @State, so re-appearance does NOT refetch.
- CardDetailView.swift:40 uses a bare `.task { await load() }` — refetches on
  EVERY appearance of that card detail (each navigation push recreates the view).
  Since the card list only goes to detail on tap, that's a per-tap reload of a
  single card — minor, but it is a re-fetch-on-every-appearance.
- The offline fallback returns .stale with a live fetch each time — it does NOT
  avoid the network call, so "re-fetching on every appearance" is only prevented
  by each view's value==nil guard, not by any cache.

---

## 4. N+1 patterns

Checked: ApprovalsView, BoardHygieneView, SessionsView, ActivityFeedView,
ProjectsView (list, detail, board), CardsView, ClusterView, FleetView, AutodownView.
NO N+1 found. Every list screen fetches ONE list payload and renders rows from it;
the only per-row detail fetch (CardDetailView `/v1/cards/{id}`) is on-demand via
tap, not a per-row loop. Board tab loads cards+blocked+stale CONCURRENTLY via
withTaskGroup (ProjectsView.swift:775-783) — 3 parallel reads, not serial.
No list-then-request-per-row pattern exists.

Residual duplicate (not N+1): a project's Overview and Settings sections each
fire their own `/v1/projects/{name}` detail fetch (ProjectsView ProjectDetailView
segmented picker); switching Overview<->Settings re-fetches the same project
detail (ProjectSettingsView.swift:912-918). Minor duplicate, not a per-row loop.

---

## 5. Ranked fixes by operator-visible impact

P0 — Fix ClusterView dead /v1/cluster/hosts fetch (3b). Removes a 0.7s wasted
     round-trip from the FIRST screen the operator sees every time they open the
     app. Zero risk (delete state + loadHosts path; topology already derives from
     status). Note the doc comments (ClusterView.swift:21-22, 100-103) must be
     updated too — they claim hosts drives the topology and are currently wrong.
P0/P1 — Add a short positive TTL cache (e.g. 10-30s) keyed by endpoint so
     re-appearing screens render cached data instantly and refresh in the
     background (3a). The biggest systemic lever: most screens refetch identical
     data seconds apart, and the offline-fallback (StateCache) never short-circuits
     a fetch. Target: /v1/cluster/status, /v1/cluster/info — the Cluster strip.
P1 — CardDetailView bare `.task` refetch (3c). Guard with value==nil like every
     other screen. Trivial, removes a per-tap reload.
P1 — Project Overview<->Settings duplicate detail fetch (4). Share one shared
     detail LoadState across the two panes. Trivial.
P2 — The 3s /v1/cluster/monitor is NOT currently on any phone screen (client
     method clusterMonitor() at HSCCClient.swift:387 is uncalled). It is the
     slowest endpoint but only reachable via the client API / server. If/when a
     screen adopts it, it will add a flat ~3s — recommend the daemon capture a
     cached metrics snapshot (poll every Ns server-side) so the endpoint returns
     fast, and do NOT wire it into the Cluster tab until cached.
P2 — /v1/autodown/status cold-start straggler (9.5s first call, 0.02s repeat).
     The server computes the status context on first hit; warm it on daemon start
     or cache. Currently on the phone only after the first hit (AutodownView
     loads once), so impact is a single first-open delay.

Verdict: the app does NOT feel slow because of N+1 (none exist) or a frozen UI
(every screen shows progress). It feels slow for three concrete reasons, in order:
(1) the Cluster landing surface wastedly fetches /v1/cluster/hosts (~0.7s) and
discards it on every launch; (2) there is NO positive cache, so every returning
screen re-pays full network latency even for unchanged data; (3) the slowest real
endpoints (cluster/*) shell out to SSH subprocesses that cost 0.25-3.0s server-side,
which a phone over Tailscale only makes worse. The dead /v1/cluster/hosts fetch is
the cheapest, highest-leverage fix.

---

## What I deliberately did NOT fix / why
- Did not touch the orchestrator chat 30-90s latency: it is genuine server LLM
  work, not a client slowness; server measures floor 16.8s for a two-word reply.
- Did not re-architect StateCache into a positive cache blindly: offline-fallback
  semantics are intentional and documented; a positive TTL is a NEW opt-in layer.

## Verification / evidence notes
- perf_measure.py derives the address at runtime (api_base()), never hardcodes IP.
- All endpoint timings were measured against the LIVE API. They were NOT
  read-only GETs: the run quoted in section 1 fired `/v1/verify` five times as
  well, which is what the provenance note there is for. Corrected 2026-11-20
  (t_0a9bdc40); the script PR #490 produces never fires it.
- NO iOS runtime on this host: UI findings are source-reasoned, not executed.
  Backend endpoint timings are executed proof.
