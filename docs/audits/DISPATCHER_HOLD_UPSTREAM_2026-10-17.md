# Upstream report — the Hermes "dispatcher stuck" warning counts benign holds as stuck

Date: 2026-10-17 · Card: t_160d306f · Status: REPORTED (hscc side mitigated; Hermes NOT patched)

## TL;DR

The gateway dispatcher's own health warning
(`kanban dispatcher stuck: ready queue non-empty for N consecutive ticks but 0
workers spawned`) increments its bad-tick streak whenever the ready queue is
non-empty and nothing spawned — **including the ticks where the dispatcher
holds every ready card back by design** (`active_pr`, `recent_success`,
`rate_limited`, `skipped_locked`, `memory_pressure`).
`describe_suppression()` only *annotates* the warning with the hold payload
("Last tick held back: active_pr=1."); it does not influence the counter. A
benign steady state therefore emits an operator-facing WARN forever, and any
downstream watchdog keying off the warning (hscc's dispatcher-wedge watchdog
did, on 2026-10-17) inherits the false positive.

## Evidence (from the live gateway.log, 2026-10-17, local time)

Card t_4e0f0509 (foreign board) sat READY with an open PR in its recent
comments. The respawn guard (`hermes_cli/kanban_db_dispatch.py`
`check_respawn_guard`, reason `active_pr`) correctly refused to respawn it on
every tick. The dispatcher still climbed the stuck ladder:

```
12:49:30 WARNING gateway.run: kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks but 0 workers spawned. Last tick held back: active_pr=1. ...
12:54:32 ... 11 consecutive ticks ... Last tick held back: active_pr=1.
12:59:34 ... 16 consecutive ticks ... Last tick held back: active_pr=1.
13:04:36 ... 21 consecutive ticks ... Last tick held back: active_pr=1.
13:09:38 ... 26 consecutive ticks ... Last tick held back: active_pr=1.
```

The ladder (and the earlier warnings without a hold clause) is exactly what
the hscc watchdog consumed; it restarted the gateway at ~12:13, ~12:43 and
~13:17 — three `launchctl kickstart` cycles in ~65 minutes for a dispatcher
that was working perfectly. (hscc-side fixture, redacted:
`hscc_daemon/tests/fixtures/gateway_wedge_incident_20261017.log`.)

## Where it lives in Hermes (read-only, as of v2026.9.14 line refs)

* `gateway/kanban_watchers.py` ~line 305:
  `bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0`
  — no hold awareness. The WARN at ~line 308-315 appends
  `f" Last tick held back: {held}."` but the streak already counted the tick.
* `hermes_cli/kanban_ops.py` ~line 214-236: the CLI-daemon twin has the same
  shape (`ready_pending and not res.spawned` → `bad_ticks += 1`, annotation
  only via `kbd.describe_suppression([res])`).
* `hermes_cli/kanban_db_dispatch.py` `DispatchResult` /
  `describe_suppression()`: the hold vocabulary —
  `respawn_guarded ∈ {blocker_auth, recent_success, active_pr}`,
  `rate_limited`, `skipped_locked`, `memory_pressure ∈ {critical, elevated}`,
  plus `skipped_nonspawnable` / `skipped_per_profile_capped` which are NOT
  in the suppression string at all.

## Proposed upstream change (smallest)

In both warn sites, when the tick produced zero spawns **only because of
holds**, do not count the tick (or reset the streak), e.g.:

```python
holds = _kbd.describe_suppression(...)          # already computed
benign_only = holds and all(
    name.split("=")[0] in _BENIGN_HOLD_REASONS  # active_pr, recent_success,
    for name in holds.split(", "))              # rate_limited, skipped_locked,
                                                # memory_pressure
bad_ticks = 0 if benign_only else bad_ticks + 1  # (blocker_auth stays stuck-worthy)
```

Design notes for whoever picks this up:

1. `blocker_auth` is a credential/quota wall — it SHOULD keep counting (the
   warning's "check credentials" advice is correct there). Fail closed on any
   unknown reason name: a future hold type should keep warning until it is
   deliberately classified.
2. `skipped_locked` / `memory_pressure` are tick-level and transient;
   counting them is also noise (the lock holder IS making progress).
3. `skipped_per_profile_capped` / `skipped_nonspawnable` never reach the
   warning today — the per-profile-cap case (board roomy globally, every
   waiting profile capped) would deserve the same treatment; hscc already
   suppresses it on its side.
4. If the warning stops firing for benign holds, keep emitting the
   suppression line somewhere (e.g. INFO on the same cadence) — it is the
   only place `hermes kanban tail` consumers learn WHY nothing spawned.

## hscc-side mitigation (shipped, this repo)

`hscc_daemon/dispatcher_wedge.py` no longer treats a held-back card as a
wedge, in two independent, fail-closed layers:

* **Layer 1 (probe)** — `classify_dispatcher_hold()` tails the gateway log
  for the newest stuck warning at-or-before now, inside
  `HSCC_DISPATCHER_HOLD_LOOKBACK_SECONDS` (900 s). If every hold name in the
  clause is benign AND the claimed hold counts reconcile against the boards'
  pending queue, the zero-spawn tick is explained and no stall is declared
  (held != wedged). Unknown names, `blocker_auth`, missing clause, stale
  warning or unreconcilable counts stay wedge-worthy.
* **Layer 2 (recovery)** — before any `launchctl kickstart`,
  `board_state_explains_stall()` re-verifies through the sanctioned
  `hermes kanban list/show --json` that every ready card is explainably held
  (fleet cap / per-profile cap / fresh benign `respawn_guarded` event /
  rate-limited release). Explainable ⇒ suppressed + operator notified;
  unverifiable ⇒ the restart proceeds (original purpose intact).

Regression replay of the exact incident ladder (6→11→16→21→26, fixture from
the real log, redacted) asserts ZERO restart actions on the benign hold and
a fired restart with the clause stripped — mutation-probed both directions.

Until upstream fixes the counter, the WARN will keep firing on benign holds;
that is log noise, not an action item, and hscc's watchdog now says so on
the `dispatcher` stream (`hold_explained`, `hold_reasons`).
