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

### F3 — auto-heal must recreate ONLY the down unit, never the whole template

**Files:** `hscc_daemon/health.py`, `hscc_daemon/tests/test_health.py`

The most destructive of the four actors. `_default_autoheal_worker(key, label, node, port)`
is handed the exact unit that is down — node, port, recipe, span — and then discarded that
information and ran:

```
cmd_cluster_template(["apply", <applied-template>, "--confirm", "--force-recreate"])
```

That stops and re-runs **every** unit in the fleet to heal one. The log records it firing at
`07:21:45` and `07:43:19` for `...-247-8002` alone, and between those two calls `.246` and
`.248` — both healthy — were torn down and re-launched, so neither ever finished its ~3 min
model load and both were then seen as "down", feeding the next round.

The wedge is one unit whose vLLM never answers; a fleet-wide apply is never the right
treatment for it. The heal now recreates exactly that unit:

1. `sparkrun stop <recipe> --hosts <span>` — scoped to the unit's own span, never `--all`,
   so a co-located sibling on another port survives. The whole span is stopped (not just
   this node) so two workloads can never bind the same `host:port`.
2. Re-issue the unit **as defined** through `serving._unit_run_cmd(unit)` — the single
   source of truth shared with `fleet_up_plan`, so sparkrun derives the SAME `cluster_id`
   — with `--ensure` **stripped**. `--ensure` no-ops on a container that is `Up` but wedged,
   which is precisely the failure class this path exists for (that is the documented reason
   the auto-heal path exists at all). Stopping first plus no `--ensure` makes the recreate
   real; a leftover container then collides loudly in the heal log rather than being
   silently skipped.
3. Detached (`start_new_session`) into `~/.hscc/heal-<node>-<port>.log`, because the
   recreate re-stages weights for minutes and blocking the health check that long would
   stall every other stream. The relaunch timestamp is recorded **before** launching, so
   the F1 grace shields the container this path just created from the gentle relaunch —
   the exact pairing F1 was written to protect.

No new mechanism and no sparkrun change: `stop --hosts` and `run` are the same sanctioned
primitives the gentle relaunch already uses. `sparkrun run` has no `--force-recreate` flag
(verified against `sparkrun run --help`), which is why stop-then-run-without-`--ensure` is
the correct expression of "recreate one unit". `_autoheal_cluster_dir()` became dead and was
removed. Log wording changed from "force-recreate via template apply" to "recreating this
unit only" so the log tells the truth about the blast radius.

Tests (6, in `TestCheckWorkersAutoHeal`): one stop + one launch for the down unit only, with
the healthy sibling's recipe **and** host absent from both; a TP unit recreates as a group
(`--hosts a,b --tp 2`); a `cmd_cluster_template` that raises proves the apply path is never
reached; the relaunch timestamp is recorded; an unknown `(node, port)` stops/launches
nothing and reports loudly. Mutation-checked: reverting `health.py` to the pre-F3 version
makes **all 6 fail**. `TestCheckWorkersAutoHeal::test_action_logged_distinctly` was updated
because it asserted the old "force-recreate" wording.

A test-isolation bug found while writing these (fixed separately in `b94b9377`, before this
commit): the F2 lease was not redirected in `conftest.py`, so tests wrote a live lease into
the operator's real `~/.hscc` — verified by stashing the conftest change and watching
`heal-lease.json` appear with a 600s TTL while the suite stayed green.

### F4 — proxy health must compare the LIVE router to actually-healthy units

**Files:** `hscc_daemon/health.py`, `hscc_daemon/tests/test_health.py`

The incident's second failure ran for ~2 hours while every check said OK. After
recovery, `:4000` was an **orphan litellm** routing to ONE backend (`.246`)
although three units were healthy:

* `sparkrun proxy status` and `sparkrun proxy sync` both reported "in sync" —
  both read the **config file on disk**, which listed all 4 backends.
* `sparkrun proxy start --restart` did not fix it: the orphan was not the
  tracked PID, so sparkrun SIGTERMs nothing that matters, and the new instance
  **silently bound `:1708`** while the orphan kept `:4000`.
* The existing hscc check could not see it either: `_proxy_models_probe` reads
  `/v1/models`, which lists model **ids** only. A proxy serving every expected
  alias through ONE backend passes the alias test — the catalogue was genuinely
  correct, the routing was not.

**Root cause, read out of sparkrun's source** (`src/sparkrun/proxy/`, not
edited here). `apply_desired_state()` writes the config and then:

```python
if not self.is_running():
    logger.debug("Proxy not running — new config applies on next start")
    return added, removed
```

and `is_running()` is only `process_exists(_read_pid())` — **the state-file
PID, never the actual owner of the port**. With a live proxy on `:4000` that
sparkrun does not recognise, every auto-discover sweep rewrites the config and
then skips the restart that would have applied it. That single early-return
explains the whole F4b symptom (below), and it is not hscc-visible state we can
fix — so hscc stops trusting it.

**What hscc does now.** `check_proxy` keeps the catalogue check and adds a
second, live one:

* `_proxy_backend_probe()` GETs **`/v1/model/info`** on the running proxy — the
  only endpoint that reveals `litellm_params.api_base` — and reduces it to a
  `{"host:port", ...}` set. Verified against the running fleet: it answers
  without auth and returns the real api_base per model.
* `_fleet_unit_backends()` probes each unit's `/health` with a **2s** timeout —
  deliberately stricter than the workers check's 5s, because this is a
  consistency check and the cost of demanding too much is a needless LB restart.
  The authoritative down verdict (load grace + debounce + cooldown) stays in
  `check_workers`, so F1's protection is not bypassed.
* `_proxy_backend_drift()` compares the two, scoped to hscc's own fleet:
  a healthy unit missing from the live router, or a router pointing at an
  unhealthy fleet backend, is unhealthy. Backends hscc does not supervise are
  ignored (demanding them would relaunch forever); an empty live set is *not*
  drift here, because the catalogue check already owns "answers but serves
  nothing" and reporting one fault twice makes it look like two.
* A probe failure (`/v1/model/info` unavailable) **fails open** — it is logged
  and the drift check is skipped, because the catalogue probe just succeeded and
  restarting a working LB on an endpoint change is worse than a missed drift.

**Heal = `_proxy_repair_orphan()`, not `proxy start`.** Drift selects a different
path on purpose: plain `proxy start` REFUSES a running proxy (sparkrun returns
1 on `ProxyAlreadyRunning`), and `--restart` only SIGTERMs the PID in sparkrun's
own state file — which is exactly what was wrong. So the repair is:
`sparkrun proxy stop` → reap any listener sparkrun does **not** track,
**cmdline-vetted for `litellm` before any signal** (same blast-radius contract as
the orphaned-serve heal; a foreign listener on `:4000` is reported and left
alone; the tracked PID is never killed) → plain `sparkrun proxy start --hosts
<fleet>` → **verify the port owner equals the tracked PID**. Exit code is not
accepted as success: `proxy start --restart` exited 0 during the incident.

### F4b — node reboot: the proxy MUST re-add a returned unit

This is the path the operator actually took: power-cycling `.244`/`.247` while
the proxy was running. The proxy was (re)started at 10:25 local while only
`.246` was up; when the rebooted nodes came back and their units turned healthy
(10:52), the running proxy **never picked them up**, even though
`Auto-discover enabled (every 30s)` was logged and the config file on disk was
rewritten with all 4 backends.

That is the same early-return above, not a separate bug: auto-discover's sweep
*does* discover the returned units and *does* rewrite the config, then skips the
restart because `is_running()` is false for a proxy it does not track. The
config file and the live router permanently diverge, and both `proxy status`
and `proxy sync` read the file — so the divergence is invisible by construction.

F4's live-vs-healthy comparison catches it either way, without touching
sparkrun: the unit is healthy again, the live router has no backend for it, that
is drift, and the repair re-registers it. Pinned by
`test_returned_unit_is_readded_within_one_sweep`, which walks the operator's
sequence: unit leaves (no drift demanded on a rebooting node — a healthy-set
with one backend matches a router with one backend), unit returns and is
healthy, the *next single sweep* must detect it and repair.

Tests (14): api_base parsing from `/v1/model/info`; each drift direction
(missing / dead / out-of-scope / match); probe-failure-is-not-drift;
`check_proxy` routes catalogue-healthy-but-single-backend into the repair path
and records `backend_drift` in state; drift never falls back to plain `proxy
start`; the reboot scenario above; the three reap guards (tracked PID refused,
non-litellm refused, untracked litellm reaped); and repair verifying port-owner
== tracked PID plus the stop → reap → start order with `--restart` absent.
Mutation-checked by disabling only the wiring in `check_proxy` (helpers kept):
**2 fail** — the two end-to-end integration tests — while the 40 helper/unit
tests pass either way, which is the expected shape.

Test-isolation note: `TestCheckProxy._setup` now stubs `_proxy_backend_probe`
**by default**. The first run of these tests failed on
`test_both_aliases_served_is_healthy` because the new path reached the
operator's live `:4000`. That class of ambient-state leak is what the class
docstring already forbids for `_proxy_models_probe`; the default is now
"could not ask the proxy" (the documented fail-open), so every pre-F4
expectation is preserved unchanged and no test can reach the running fleet.

### F5 — per-node heal circuit breaker (finite, not just sequential)

**Files:** `hscc_daemon/heal_breaker.py` (new), `hscc_daemon/health.py`,
`hscc_daemon/cli.py`, `hscc_daemon/tests/conftest.py`,
`hscc_daemon/tests/test_heal_breaker.py` (new, 25 tests)

F2's lease made repairs **sequential**. It did not make them **finite**, and
finiteness is what this incident needed: four sequential-but-unbounded paths
fired every ~60s for ~35 minutes against degrading hardware until two nodes
hard-hung and needed a manual power-cycle. A repair loop that never gives up is
not persistence — on a dying node it *is* the fault.

**Design decisions (and why):**

* **Per-node, not per-unit.** The per-unit cooldowns cannot see a node being
  hammered across several of its units, and `check_proxy`'s cooldown was
  per-*(host,port)* — which is why it never fired during a 35-minute storm
  (§2.4). Strikes are keyed on the node.
* **Per-node also means a tripped node never disables the fleet.** Pinned by a
  test: node A suspended, node B keeps getting repaired.
* **Durable ledger, not a dict.** The incident's in-memory
  `_worker_down_streak`/`_worker_last_autoheal` reset on every daemon restart —
  and a heal storm is exactly what restarts a daemon, so the counters could
  never reach their threshold. `~/.hscc/heal-breaker.json` survives, and there
  is a test that reloads the module with `HOME` moved to prove the count
  survives (a plain in-memory counter would come back empty there).
* **Self-clearing suspension.** `suspended()` returns seconds until the oldest
  strike ages out of the window, so the node recovers without operator action.
  A latch with no expiry would strand a node forever — the mirror-image failure
  — and the incident already proved nobody is watching at 07:00.
* **Suspension does not re-count.** A tick arriving during suspension returns
  through `suspended()` *without* adding a strike; re-counting would extend the
  suspension forever and the node could never come back.
* **Threshold 6, not the incident's 3.** A legitimate `cluster up` recovery can
  fan out across several units on one node in a single tick; 6 sits above that
  and well below the incident's pace (dozens of actions in 35 min). Overridable
  via `HSCC_HEAL_BREAKER_MAX` / `_WINDOW_S` / `_SUSPEND_S`.
* **Count the attempt, withhold the action.** A strike is recorded when a heal
  is attempted, and the attempt that crosses the line is refused — so a heal
  that *fails* still counts (a failing heal is precisely the storm signal), but
  the trip never itself performs a destructive action.
* **Gate placement = after the lease, inside the try.** A tick deferred by the
  lease attempts nothing and must not strike; `continue` inside the `try` runs
  the `finally` that releases the lease.

**Alerting — and why not Telegram.** The breaker alerts **once** per latch
(`should_alert`, separate from the strike ledger; the 2026-10-08 NAS rule fired
178 notifications in 11 minutes by conflating them) via a **direct**
`send_macos_notification(priority="high")` plus an `emit_event` ledger record.
Telegram is deliberately NOT implemented here, and this is an honest gap rather
than an oversight: `grep -ri telegram` across `hscc_daemon/` finds **no sender
at all** (it appears only in iOS/docs/tests), the documented design is "health
check emits an event; the orchestrator forwards via Telegram", and the webhook
platform that would carry it is disabled (`hermes webhook list` → "Webhook
platform is not enabled"; enabling it needs a gateway restart, which is out of
scope for this card and would disturb the co-tenant workload). So the operator
gets a desktop notification and a durable `events.jsonl` record, and a
Telegram/mobile push remains **open work**: it needs either the webhook platform
enabled or a small `alert_sink` module, plus a `heal_breaker.tripped` rule in
`triggers.default.json`. I did not invent a bot token or a second transport.

**Why a direct notify instead of a trigger rule:** `autodown.py`,
`lifecycle.py` and `escalate_watcher.py` all notify directly for critical
signals, and the live `~/.hscc/triggers.json` only gains a rule on a fresh
bootstrap — a critical alert that silently depends on a rule that may not exist
is the same class of silent failure that left the 2026-10-08 NAS outage
un-noticed.

**`hscc heal-clear <node>` / `hscc heal-status` (`cmd_heal`).** The alert text
tells the operator to run a command; an alert pointing at a command that does
not exist is worse than no alert, so the override ships with the breaker.
Clearing also calls `reset_alert`, so a node that heals and later fails again
can raise a *new* alert instead of being silenced forever.

**Test isolation.** `conftest.py` redirects `HEAL_BREAKER_FILE` into the per-test
tmp dir, for the same reason the lease must be: `check_workers`/`check_proxy` now
strike on every heal, so an unredirected path could suspend healing of a **live**
node from a test run.

**Two test bugs found and fixed while writing these** (both recorded because
they are the kind that make a suite lie):

1. Asserting `heals == [... a[0] ...]` compares against `_autoheal_worker_fn`'s
   **key tuple** `(node, port)`, so `a[0] == "10.0.0.2"` is never true and the
   assertion passes vacuously — *worse* than failing, because it looks like
   coverage. The signature is `(key, label, node, port)`; the node is `a[2]`.
2. The restart test used `monkeypatch.setattr(MODULE, "HEAL_BREAKER_MAX", 3)`
   and then `importlib.reload`: reload re-executes the module and recomputes the
   constant from `os.environ`, silently reverting the patch. Constants that
   `reload` recomputes must be set via the **environment**.

**Mutation check:** see §5 (run separately, since it temporarily neuters the
gate in the same worktree the suite runs from).

## 4. Node hang investigation (F7)

Read-only forensics on the two hard-hung nodes, driven through sparkrun's own
`resolve_hosts` + `build_ssh_kwargs` + `run_remote_command` (no hand-rolled ssh).
Hosts are shown as `10.0.0.x` placeholders. `journalctl -b -1` is the boot that
hung; boot 0 starts at the power-cycle.

**Baseline first — the fleet runs ONE unit per host.** `sparkrun`'s own status
(`classify_cluster_status(...).to_dict()`, keys `groups / solo_entries /
total_containers / host_count`) reports `total_containers=4`, `host_count=4`,
`solo_entries` one per host on ports 8000/8001/8002/8003. So two vLLM containers
on one node is an anomaly, not normal operation. This matters because the F7
question was exactly "did two containers load on one node at once".

### .247 — hard lockup, and by design nothing was written

Boot `-1` runs `2026-10-08 14:15:58` → `2026-10-14 10:20:33` EEST, i.e. the
journal **stops at 07:20:33Z**, about three minutes into the cascade and
~70 s before the first whole-template force-recreate (`07:21:45`). Boot `-1`
contains **no** oom-kill, **no** Xid and **no** hung-task record — grep count 0
for those terms apart from two boot-start banner lines.

The silence is explained by the boot banner itself:

```
kernel: watchdog: NMI not fully supported
kernel: watchdog: Hard watchdog permanently disabled
```

On this SoC the hard lockup detector is **permanently disabled**, so a hard
lockup produces no panic, no stack dump, no OOPS — the journal simply ends.
That is why the operator saw ping + TCP/22 answering (the NIC stack was still
alive) with never an SSH banner, and why a power-cycle was the only recovery.
**The absence of evidence on .247 is a property of the platform, not evidence
that nothing happened.** Any future hscc-side diagnosis must not expect a
node-side trace after this class of hang.

Last lines before it died are a `snap.firmware-updater.firmware-notifier`
restart loop at restart counter **38,300** (~12 s cadence for six days, each
failing `timeout waiting for snap system profiles to get updated`) interleaved
with an sshd disconnect every few seconds from the orchestrator. That snap loop
is background load worth cleaning up on the nodes, but it is not the hang
mechanism — it survives on the healthy nodes too.

### .244 — the mechanism is visible: memory pressure + NVIDIA rw-semaphore deadlock

Boot `-1` on .244 has **104** matches for oom-kill / OOM / Xid / hung-task /
blocked-for-more-than. The decisive lines:

```
kernel: INFO: task nvidia-smi:485292 <reader> blocked on an rw-semaphore
        likely owned by task VLLM::EngineCor:482133 <writer>
kernel: task:VLLM::EngineCor state:R  running task
systemd-journald[625]: Under memory pressure, flushing caches.   (repeated)
systemd[3237600]: wireplumber.service: Failed with result 'oom-kill'.
```

So it is not "the model was too big". A `nvidia-smi` reader was blocked on a
driver rw-semaphore whose holder was a **live vLLM engine core**, while the host
was already under enough memory pressure to journald-flush caches and OOM-kill
user services. That is the "GPU work never completes, and every process that
touches the driver queues behind it" state — which is exactly what makes a node
look alive on TCP and never answer an SSH banner: sshd needs a fork + PAM +
memory, and the box cannot make progress.

And the cascade put a second workload on that node. In the `10:15–10:25` window
dockerd deactivates **two** container scopes at `10:22:46`, then starts **two**
new solo containers 60 ms apart at `10:22:48`, both `net=host`:

```
Started docker-f828420f….scope      → sparkrun_03fae933e3df92bf_…_solo net=host
Started docker-ff034b55e….scope     → sparkrun_bf3f4c53163359c1_…_solo net=host
```

`…03fae933e3df92bf…` is the declared unit for that host (it is what
`solo_entries` still reports there on port 8000). The second one is not part of
that host's declared set — against a one-unit-per-host baseline, .244 was
staging weights for **two** vLLM instances at once, on a host already
OOM-killing services. Two simultaneous NVFP4 weight stagings on one SoC is the
plausible trigger for the memory-pressure + driver-semaphore pile-up above.

### Honest limits of this finding

I could **not** name the second container's recipe. `docker ps -a` on .244 now
shows only the declared unit plus one exited 8 weeks ago — the cascade-era
container is gone and the journal does not record its image or command.
The intent id `bf3f4c53163359c1` is not a usable fingerprint either: the daemon
log reports that SAME id as "no running workload matches" for three different
units (`247-8002`, `248-8003`, `246-8001`), so it looks like a shared
recipe/pool id rather than a per-unit one. So the claim stands at "a second,
undeclared solo workload was started on .244 during the cascade", not "unit X
was placed on the wrong host".

### What hscc should conclude

1. The hang is not a model-size problem and not fixable from hscc. It was
   *induced*: F3's whole-template force-recreate plus F1's per-minute relaunch
   created concurrent weight stagings on hosts that hold one unit each, on a
   platform that cannot survive that without leaving a trace. F1–F3 remove the
   actor; that is the fix.
2. hscc has no invariant "one vLLM per host outside the declared span". Every
   launch path trusts serving.json. Adding that check before any launch would
   have refused the second container outright. Proposed as a follow-up card
   rather than folded into this one, since it needs a decision on what the
   daemon should do when the invariant is violated (refuse vs. alert).
3. Do not rely on node-side forensics after a hang on these nodes. With the
   hard watchdog permanently disabled there is nothing to read. Persisting a
   trace (ramoops/pstore or netconsole) is a node-side operator change, outside
   this card's scope, and hscc should not pretend otherwise.
