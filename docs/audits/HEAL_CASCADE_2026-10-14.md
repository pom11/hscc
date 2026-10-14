# Heal Cascade — 2026-10-14, 07:17–07:56Z (10:17–12:56 EEST)

Status: **IN PROGRESS** — evidence collected from `~/.hscc/daemon.log`, fixes landing as
atomic commits on `fix/heal-cascade-2026-10-14`.

Addresses in this document are placeholders (`10.0.0.x` = the four LAN nodes, in the order
they appear in the log; `100.64.0.1` = tailnet host). The real addresses are in the log.

## 1. What happened (verified against the log, line numbers into the captured window)

The captured window is `~/.hscc/daemon.log` lines whose timestamp falls in
`2026-10-14T07:17:00Z .. 07:59:34Z` (837 lines).

| # | Time (Z) | Log evidence (verbatim, trimmed) | Meaning |
|---|---|---|---|
| 1 | 07:17:03 | `Engine-wedge check: WEDGED unit(s) detected: family-coding-Qwen3.8-Flash-Next-NVFP4-247-8002` | One unit wedged. 3/4 still streaming. |
| 2 | 07:19:32 | `Engine-wedge recovery: unit '...247-8002' streak=3 — resolved container_id=...` | Wedge recovery armed. |
| 3 | 07:19:34 | `Worker ...247-8002 down — relaunching (...qwen3.8-flash-next-nvfp4-solo-vllm.yaml) on :8002` | **Path (b)** — check_workers relaunch, no load grace. |
| 4 | 07:19:35 | `Engine-wedge recovery: stopped unit '...247-8002' (stop_ok=True)` then `relaunching missing units via `hscc cluster up`` | **Path (a)** — wedge recovery, same fault. |
| 5 | 07:20:32 | `Worker proxy unhealthy on :4000 (probe failed: [Errno 61] Connection refused)` then `relaunching with ['10.0.0.244','10.0.0.246','10.0.0.247','10.0.0.248']` | **Path (c)** — proxy relaunch for all 4 hosts. |
| 6 | 07:21:45 | `Auto-heal: worker ...247-8002 (10.0.0.247:8002) down 3x consecutively — force-recreate via template apply` + `force-recreating via template apply '4node-flash-next-all' --force-recreate` | **Path (d)** — whole template torn down for one unit. |
| 7 | 07:22:41 | `Gateway check: ok=False (job=True vllm=False mux=True)` | Gateway starts reporting down. |
| 8 | 07:24:29 | `Worker ...248-8003 down — relaunching` / `Workers check: 1/3 online` | Collateral: healthy unit now "down". |
| 9 | 07:25:29 | `Worker ...246-8001 down — relaunching` / `Workers check: 0/3 online` | All three workers being recycled. |
| 10 | 07:25:42 | `relaunching with [...] (--restart: running proxy serves nothing)` | Second proxy relaunch. |
| 11 | 07:32:40 | `Auto-heal: ... down 8x consecutively — force-recreate via template apply` | Second whole-template recreate. |
| 12 | 07:43:19 | `Auto-heal: ... down 7x consecutively — force-recreate via template apply` | Third whole-template recreate. |

Note the *down* counts (3x, 8x, 7x) are non-monotonic across the window: they are per-worker
counters on different check cycles, and the counters are in-memory (see F2).

### The bug F1 fixes is visible in the log itself

Line 421 proves the load-grace predicate exists and works — but only on the auto-heal path:

```
07:37:58 Auto-heal: worker ...248-8003 (10.0.0.248:8003) down 3x but container <1200s old
         and has never served — still loading, NOT auto-healing
```

Meanwhile lines 3, 8, 9, 12 show the plain `check_workers` path relaunching the very same
kind of container with no such check. A model takes ~3 minutes to load; the relaunch path
fires every 60s. Every relaunch therefore destroyed a load in progress, which is what made
`0/3 online` self-sustaining.

## 2. Root causes

| ID | Root cause | Where |
|---|---|---|
| F1 | Two heal paths, only one of which knows about load grace. The predicate is private to the auto-heal path. | `health.py` `_suppress_autoheal_for_startup_grace` (~1492-1543) vs the `down — relaunching` path |
| F2 | No mutual exclusion. Four independent mechanisms can act on one fault at once; state (strike counters) is in-memory so a restart also resets it. | wedge recovery / `check_workers` / proxy check / auto-heal |
| F3 | Template auto-heal recreates the whole template for one down unit. | auto-heal `force-recreating via template apply ... --force-recreate` |
| F4 | Proxy health = "does :4000 answer", so a live proxy routing to 1 of 4 healthy units reads healthy. `proxy status`/`sync` read the config file, not the running router. | proxy check |
| F4b | Auto-discover rewrites the config file but the live router keeps its old backend set — a unit returning after a reboot is never re-added. | proxy (sparkrun-side router) |
| F5 | No bound on heal actions per node; the log shows 3 whole-template recreates + N relaunches on the same node in 25 min. | auto-heal / relaunch |
| F6 | `hscc cluster up --help` executed the command; `hscc start` leaves the caller's stdout open. | `cli.py` |
| F7 | Unknown — see §4. | nodes |

## 3. Fixes

_(append per commit)_

## 4. Node hang investigation (F7)

_pending_
