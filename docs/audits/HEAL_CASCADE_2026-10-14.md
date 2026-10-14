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

### F1 — `check_workers` must not relaunch a unit still inside its load grace

**Commit:** (this commit) `fix(daemon): share the load grace with the gentle worker relaunch`
**Files:** `hscc_daemon/health.py`, `hscc_daemon/tests/test_health.py`

Root cause, sharper than the report's framing: the gentle relaunch path *did* have a
grace (`_worker_relaunch_at`, `VLLM_LOAD_GRACE_MINUTES` = 20 min) but it is keyed on
**relaunches that path itself issued**. It knows nothing about a container that wedge
recovery, `cluster up` or a template apply created seconds earlier. The per-unit cadence
is therefore not "every minute" — after its own relaunch a unit is left alone for 20
minutes — but any container created elsewhere is fair game on the next 60s check.

Log proof the predicate existed but was unreachable from this path: at 07:37:58 auto-heal
correctly declined (line 421) while the same kind of young container was relaunched at
07:19:34 / 07:24:29 / 07:25:29 / 07:47:28.

Fix: `_suppress_autoheal_for_startup_grace(...)` → `_unit_is_still_loading(key, node,
port, now_wall, *, fail_closed=...)`, called from BOTH down-repair paths. The flag splits
the two paths' deliberately different treatment of *unknown* age:

| Path | Action | `fail_closed` | Unknown age means |
|---|---|---|---|
| debounced auto-heal | force-recreate (destroys the container) | `True` | assume loading, shield (unchanged behaviour) |
| gentle relaunch | stop + re-issue the unit | `False` | no container evidence → no load to destroy → repair proceeds |

Fail-open on the relaunch path is not an oversight: the created-ts map returns a stale
cache rather than an empty map on query failure, so a genuinely missing entry means a
missing container — and shielding there would strand a dead unit unrepaired for 20 min.

Tests (4 new, in `TestWorkersStartupGrace`): a young never-served container created by
another path is NOT relaunched (this one FAILS pre-fix, reproducing the incident line
`Worker w-10.0.0.2 down — relaunching`, verified by reverting `health.py`); the grace is
bounded so an old container still gets repaired; unknown age does not block the gentle
path; the destructive path still fails closed. 13/13 in the class pass.

### F2 — one repair at a time (durable heal lease)

**Commit:** (this commit) `fix(daemon): single-flight heal lease across the four repair paths`
**Files:** `hscc_daemon/heal_lease.py` (new), `health.py`, `recover.py`,
`tests/test_heal_lease.py` (new), `tests/test_health.py`, `tests/test_recover.py`,
`tests/conftest.py`

Root cause: four mechanisms could act on one fault at once, and the only exclusivity
that existed (`autodown.AUTODOWN_LOCK`, also held by engine-wedge recovery) covered just
two of them. Worker relaunch and the proxy relaunch never consult it, so 07:19:34–07:19:35
had wedge recovery running `hscc cluster up` on the same unit that `check_workers` had
relaunched one second earlier, while the proxy check SIGTERM-restarted the LB.

Why a **lease** and not a second lock: `autodown`'s rule is "a LIVE holder is never
broken, no matter its age", which is right for a lock whose holder is a process and
exactly wrong here, because our holders are **threads inside one long-lived daemon**. A
thread that dies inside the section, or a `break`/`continue` that skips a release, would
then suppress every repair for the rest of the daemon's life — a worse failure than the
one being fixed. So the lease is:

| Property | Why it matters here |
|---|---|
| durable file `~/.hscc/heal-lease.json` | the incident's strike counters were in-memory and reset on restart; who-is-repairing must not |
| bounded TTL (`HSCC_HEAL_LEASE_TTL_S`, 600s) | a leaked lease expires on its own; healing can never be disabled permanently |
| non-blocking | a second mechanism logs what is in flight and backs off ONE tick; skipping a tick is cheap, two concurrent repairs are not |
| owner-scoped release | a late `finally` cannot drop someone else's fresh lease |
| re-entrant per `who` | one repair's second step is not blocked by its own claim |

Wired into all four paths — `engine-wedge-recovery` (in `recover.py`, after the O_EXCL
lock), `worker-relaunch`, `auto-heal` and `proxy-relaunch` (in `health.py`). The lease is
claimed only around the action itself, not around the whole check, and the proxy claims it
*after* the cooldown test so a cooldown tick neither claims nor releases anything.

Deferral is reported honestly, not as failure: the proxy stream gains `deferred: true` and
is excluded from `relaunch_failed`, and a deferred tick carries the ORIGINAL cooldown stamp
forward — re-stamping would make the window perpetual and the proxy would never recover
(the same rule the existing `suppressed` path follows).

Deadlock/starvation: the only path holding both primitives takes the blocking O_EXCL lock
FIRST and the non-blocking lease second, so it can never sit holding a lease while waiting
on the lock. Pinned by `test_lease_is_taken_after_the_lock_not_before`.

Test isolation: `tests/conftest.py`'s autouse redirect now includes `heal_lease.HEAL_LEASE_FILE`.
Without that, every `check_workers`/`check_proxy` test would write a LIVE lease into the
operator's real `~/.hscc` and suppress the running daemon's heals for the whole TTL — a
test-poisoning class this repo has already been bitten by (`git status` in a test, patched
PID globals).

Tests: 14 in `test_heal_lease.py` (mutual exclusion, durability across a simulated restart
via fresh re-import, bounded expiry incl. a leaked lease, corrupt-file and unparseable-
expiry both read as free, release-by-owner, renew) plus 8 integration tests across
`test_health.py` (relaunch defers, auto-heal defers, no leak after a repair, no leak when
the heal action raises, proxy defers, deferral does not extend the cooldown, release after
attempt, release when `run_cmd` raises) and `test_recover.py` (defer, claim-and-release
across `cluster up`, release on raise, ordering). Mutation-checked: reverting the wiring in
`health.py` + `recover.py` and re-running makes **7 of those 12 fail**; the 5 that pass
either way are the "must not leak" guards, which are trivially true with no lease code.

## 4. Node hang investigation (F7)

_pending_
