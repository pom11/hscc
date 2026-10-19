# Proxy :4000 refused 3h09m while the 5-min relaunch loop no-opped — 2026-10-18

Status: **ROOT-CAUSED + FIXED (hscc side) + UPSTREAM REPORTED (§4; sparkrun
is operator-owned — this audit IS the filing)** — evidence from
`~/.hscc/daemon.log` (window 2026-10-18T01:38Z..04:55Z), sparkrun's own
`~/.cache/sparkrun/proxy/{state.yaml,litellm.log,autodiscover.log}` and
`~/.hscc/heal-breaker.json`. All probing was read-only against log/state files;
the live `:4000` was never touched. Addresses are placeholders
(`10.0.0.x` = LAN nodes; sparkrun source is in the operator's checkout,
read-only — operator owns it).

## 1. What happened (evidence; local times = UTC+3, daemon log is UTC)

| # | Time (Z) | Evidence | Meaning |
|---|---|---|---|
| 1 | 01:38:34 | `proxy unhealthy (live router points at unhealthy backend(s) ['10.0.0.1:8000'])` → `relaunch deferred — heal in progress: engine-wedge-recovery` | Wedge recovery had just stopped the orch unit (:8000); the proxy (PID 12079 + litellm child 12084) was still answering, correctly deferring on the heal lease. |
| 2 | 01:39:34 | `probe failed: Connection refused` → `relaunching with ['10.0.0.1','10.0.0.3','10.0.0.4']` (no `--restart`) → sparkrun `autodiscover.log`: `04:39:38 Auto-discover started ... proxy_pid=12079, hosts=[.1,.3,.4]` | The old proxy had died. hscc's plain `proxy start` **succeeded** here: launched proxy PID 12079 + auto-discover sidecar 12099. |
| 3 | 01:40:34 | `Running proxy check` with no unhealthy line | :4000 was healthy again (12079's litellm child served). |
| 4 | 01:41:07-10 | `autodiscover.log`: `Proxy config updated: +3` → `Restarting proxy: SIGTERM to PID 12079` → `Proxy restarted (PID 12477)` | sparkrun's own reconciler restarted the proxy for a config change. **`_restart_proxy()` SIGTERMs only the tracked PID — the uvx wrapper — and its litellm child 12084 survived, orphaned, still holding :4000.** The replacement 12477's own child could not bind (EADDRINUSE) and died; 12477 (wrapper) stayed alive, serving nothing. |
| 5 | 01:41:38 | `unhealthy (live router missing backend(s) ['10.0.0.1:8000'])` → drift path: `repairing proxy backend drift` → `reaping ORPHAN proxy listener PID 12084 (sparkrun tracks 12477)` → `proxy repair UNVERIFIED — port owner None, sparkrun tracks 12477 (None)` | The orphan 12084 answered :4000 with the **old** config (missing the re-added orch backend) → drift fired correctly. The repair stopped/reaped correctly but its final `proxy start` was **refused** — `is_running()==True` because the hollow wrapper 12477 was alive — so the tracked PID in state stayed 12477 and verification failed. |
| 6 | 01:42:42 → 04:50:04 | ~36 × `relaunching with [...]` (plain, no `--restart`), ~3.5 s each, every 5 min; `probe failed: Connection refused` every tick in between; no ERROR line, no notification, 0 ERROR entries in `events.jsonl` for the window | **The no-op loop.** state.yaml pinned PID 12477 (alive, hollow) → `engine.py:400`: `if self.is_running(): "Proxy already running"; return 1` → `cli/_proxy.py:135` → exit 1 every time. hscc logged the command, **discarded `r["output"]`** ("Error: ... already running ... Use --restart"), recorded `relaunch_failed: true` and moved on. The 300 s per-attempt cooldown (working as designed) is why the cadence was 5 min. |
| 7 | 04:49:41 | `Engine-wedge recovery: ... relaunching missing units via hscc cluster up` | The card's assumed fixer. It is **not** the fixer — `cmd_cluster_up`/`fleet_up_plan` contain no proxy step (grep: zero `proxy` occurrences). |
| 8 | 04:50:09-04:52:30 | `autodiscover.log`: `07:50:39 Restarting proxy: SIGTERM to PID 12477` → 59030; `07:52:27 PID 59030 did not exit within 15s; escalating to SIGKILL` → `07:52:30 Proxy restarted (PID 59540)`; `state.yaml started_at=04:52:30Z`; `litellm.log` = 59540's clean boot serving worker-model etc. | **What actually restored the proxy was sparkrun's own auto-discover reconciler** (~3h09m after the state was pinned): it SIGTERM'd the hollow wrapper 12477, which finally exited, and re-bound :4000. hscc's checks went green 04:51:04 / 04:52:05 / 04:53:05. |

Victims as reported: every profile served through :4000 (ecofire-orch,
sphoin-orch) died on connection errors across the window.

Honest limit: `autodiscover.log` shows `Auto-discover shutting down` at
01:41:40 (hscc's repair called `proxy stop`, which SIGTERMs the sidecar), yet a
sidecar sweep is logged at 04:50:09 — surviving artifacts are consistent with
sidecar 12099 having survived the SIGTERM mid-sweep (the NUL padding in the log
file — a second writer at a nonzero offset — corroborates fd confusion). Why the
sidecar survived, and why its reconciling pace left a 3h gap, is sparkrun-side
and named in §4; it cannot be fully reconstructed from the surviving files.

## 2. Root cause

**Primary (confirmed): a hollow tracked PID pins the stream permanently, and
hscc's only relaunch verb cannot clear it.**

sparkrun's proxy liveness is `is_running() = process_exists(state-file PID)`
(`_supervisor.py:570`), and its restart/stop paths signal **only the tracked
PID** — which for `uvx litellm` is a wrapper, while the **socket owner is a
child**. Sequence: SIGTERM wrapper → child orphaned holding the port →
replacement cannot bind and its wrapper stays alive (hollow) →
`is_running()` true forever → plain `proxy start` refuses forever (exit 1),
`--restart` only SIGTERMs the same wrapper again. hscc passed `--restart` only
when the proxy *answered with an empty catalogue* (`_proxy_needs_restart`),
never during a refused-connection outage — so all 36 attempts were in the
refusal shape. The same weakness is already tracked upstream as `t_bbce7f58`
("is_running() checks only the state-file PID"); this incident is its second
manifestation: 10-14 was "untracked orphan holds the port", 10-18 is "tracked
PID alive, port held by its orphaned child / then port free but state pinned".

**Amplifier (hscc-side, the card's actual failure mode): silence.**
`check_proxy` logged `relaunching with [...]`, ran the command, and threw away
the result text (`health.py`, `r = run_cmd(...)` → only `r.get("ok")`
consumed). The refusal message — literally "Error: proxy already running ...
Use --restart ... or 'sparkrun proxy stop' first" — would have named the fix in
the first 5 minutes. `relaunch_failed` went into the state file, but no rule,
trigger or notification consulted it; `events.jsonl` stayed empty all night. A
repair loop that fails the same way 36 times must be unable to stay silent.

**Recovery asymmetry, resolved with evidence:** the card assumed
`hscc cluster up` restored the proxy. It did not (no proxy step anywhere in
cluster-up); the sparkrun sidecar did, by coincidence of timing. The proxy
stream's recovery was never actually routed through anything that could fix a
hollow-pinned state: the plain relaunch cannot (refusal), and the F4 repair
path — which *can* (stop → reap → verified start) — was gated on
`backend_drift`, which only fires while the proxy **answers** (the brief
01:41:38 orphan window), never during the refused-connections steady state.

## 3. Hypotheses from the card — verdicts

| ID | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H1 | restart-vs-start semantics on a half-dead container | **TRUE — root cause** | §2; `engine.py:399-401` refusal + state.yaml pinned to 12477 across the window (proven by the sidecar SIGTERMing *12477* at 04:50:39). |
| H2 | "running proxy serves nothing" `--restart` no-op | FALSE for this window | No relaunch line in 01:38–04:52 carried `(--restart: ...)`; that variant is from 10-14. The hollow state was real but at the *wrapper-process* level, and `--restart` was never the code path reached. |
| H3 | host-list change (.246 dropped) made relaunches fail | FALSE | `relaunching with ['10.0.0.1','10.0.0.3','10.0.0.4']` identical on all ~36 attempts; the command was refused before host discovery mattered; aliases stayed served from these 3. `.246`'s unit was already out of the serving set before the window. Not causal. |
| H4 | relaunch threw into an except that only WARNs | NOT the mechanism | `run_cmd` never raises (returns `ok=False` + folded stderr); there is no bare-except swallowing on this path. The silence is a **discarded return value**, fixed in §5-1. |

## 4. sparkrun upstream report (filed, not patched — operator owns sparkrun)

Smallest fixes, in order of value (all in `sparkrun/proxy/`):

1. **Port-aware liveness in `is_running()`**: PID alive AND the bound port
   answers a TCP connect (short timeout). On "PID alive, port dead", `start()`
   should reap the stale state (the gateway was launched with
   `start_new_session=True`, so `os.killpg` can clear the wrapper **and its
   orphaned children**) instead of returning `ProxyAlreadyRunning`. This alone
   would have turned the 3h outage into one 5-min cooldown tick.
2. **Restart/stop must signal the process group** (`_restart_proxy`, `stop`):
   today they SIGTERM only the tracked wrapper, stranding the actual socket
   owner — the exact orphan (child 12084) that caused the drift/rebind failure
   at 01:41 and the F4b shape on 10-14.
3. **`stop_autodiscover()` should confirm the sidecar died** (it logged
   "shutting down" at 01:41:40 yet swept at 04:50:09; either it survived the
   SIGTERM or a second sidecar existed — both are bugs worth a line of
   verification + `SIGKILL` escalation as `_restart_proxy` already does for the
   gateway).

Related: `t_bbce7f58` (10-14 audit §8) tracks the same `is_running()`
weakness; this incident is independent confirmation, not a duplicate symptom.

## 5. hscc-side fix (this card)

All in `hscc_daemon/health.py` (sparkrun untouched); decisions settled here:

1. **Loud on failure.** Every failed relaunch/repair now logs `ERROR` with the
   command's folded output (first 400 chars) and records `relaunch_error` in
   the stream state. (This alone would have made the incident diagnosable from
   one daemon-log line.)
2. **Durable consecutive-failure streak + throttled alert.** `relaunch_streak`
   lives in the proxy state file (survives daemon restart — same reason the F5
   ledger is durable). At `PROXY_RELAUNCH_MAX_CONSECUTIVE` (default **3**)
   consecutive failed attempts the daemon raises the alert **once** per
   `HSCC_PROXY_STALL_NOTIFY_COOLDOWN_S` (default 1800 s, stamped in the state
   file): `desktop.send_macos_notification(priority="high")` +
   `emit_event("proxy.relaunch.stalled", ...)`. Direct notify rather than a
   trigger rule: the live `triggers.json` gains a rule only on a fresh
   bootstrap, and the 10-14 audit already established (F5 alerting) that a
   critical alert must not silently depend on a rule that may not exist.
   Suppressed (cooldown) and deferred (lease/breaker) ticks do **not** count —
   nothing was attempted — and no alert fires during an intentional-autodown
   window.
3. **Escalate after the threshold → the repair path, not `cluster up`.** Once
   the streak reaches the threshold, the next lease+breaker-gated attempt runs
   `_proxy_escalated_repair()`: kill a **hollow tracked PID** (alive per
   `os.kill`, but the proxy probe is failing; cmdline must contain `litellm`
   before any signal — same blast-radius contract as `_reap_orphan_proxy_listener`),
   re-run `sparkrun proxy stop` so sparkrun itself clears the now-stale state,
   then the proven `_proxy_repair_orphan()` (stop → cmdline-vetted reap →
   plain start → **verify port owner == tracked PID**, with a bounded late
   re-verify because `proxy start` returns after a 2 s grace and litellm binds
   later). Why not route the proxy stream through `hscc cluster up`: it
   demonstrably contains no proxy step (§1 #7), it is fleet-wide (restarts
   healthy units — the F3 blast-radius lesson), and the 10-14 fix for exactly
   this asymmetry was to make the *proxy* path smarter, not to make everything
   else proxy-aware. The escalation relaxes one existing guard — "never touch
   the tracked PID" — but only after N failures, only while the probe is
   failing, only for a `litellm`-cmdline process, and loudly.
4. **Exit code is not success.** `relaunch_streak` resets **only** when
   `check_proxy` passes its probes (the healthy branch rewrites state without
   the streak). A relaunch that merely exits 0 (sparkrun's own 2 s grace can
   return before the child dies) neither clears the streak nor defuses the
   alert — the rc-0-then-die shape stays alarm-capable.

Net effect on the incident replay: attempt 1 = plain start (refused, ERROR
logged with "already running"); attempt 2 = same, streak 2; attempt 3 =
streak 3 → **alert + escalated repair** (kill hollow 12477 → stop clears state
→ reaped reap is a no-op → start binds :4000 → verified) → proxy restored in
~15 min with a named cause on the desktop and in `events.jsonl`.

## 6. Tests

`hscc_daemon/tests/test_health.py::TestProxyRelaunchStall` — red on old code
(the same sandboxed loop that produced 36 silent no-ops asserts ERROR-with-
output on every failure, exactly one throttled notify + `proxy.relaunch.stalled`
event from attempt 3 on, escalation selects the repair sequence (never a plain
`proxy start` while pinned), the rc-0-then-die shape keeps the alarm armed, a
healthy check clears the streak, and suppressed/deferred ticks never count or
alert). Mutation-checked per branch; results in the commit message.
