# t_7a0e9c4e — Live Activity re-hydration: end leftover wakes after a process kill

Branch: audit/liveactivity-rehydration-t_7a0e9c4e (from dev @ 98da7a5)
Date: 2026-09-03

Follow-up to t_10bc37be (deep audit, `docs/reports/t_10bc37be_live_activity_audit.md`).
That audit fixed the ownership-teardown orphan (deinit ends in-flight activity on
both drivers) but left one residual risk: a *process kill* (force-quit / OS
suspension eviction) mid-wake means `deinit` never runs, so ActivityKit keeps the
"Waking the fleet" bubble + growing timer alive forever. This card implements the
launch-time re-hydration sweep that ends those leftovers.

NOT run on iOS hardware — no iOS runtime on this host. Findings below are labelled
EXECUTED (compiled / decider-logic run headlessly) vs REASONING (traced statically).

## What I changed

1. **`Sources/HSCC/LiveActivityManager.swift`** — added a static
   `sweepLeftoverWakes()` that iterates `Activity<WakingActivityAttributes>.activities`
   and ends everything that is NOT provably still in flight.
2. **`Sources/HSCC/SessionActivityDriver.swift`** — added a static
   `sweepLeftoverSessions()` that ends every leftover session activity.
3. **`Sources/HSCC/ContentView.swift`** — wired both sweeps into the root
   `TabView.onAppear`, so they run once at app launch.
4. **`scripts/live_activity_rehydration_check/` + `.sh`** — headless proof of the
   wake staleness decider (all cases EXECUTED).

## The wake staleness heuristic (`sweepLeftoverWakes`)

A found wake activity `a` is a genuine orphan (→ end it) UNLESS it is *provably
still in flight*. Provably in flight = `a.content.state.state == "waking"` AND
`a.content.state.startedAt` is recent (≤ 10 minutes old).

| found activity | verdict | action |
|---|---|---|
| `state != "waking"` (up/down/other) | settled-but-orphaned | END |
| `state == "waking"`, `startedAt` == nil | anomalous (a real wake always sets it) | END |
| `state == "waking"`, `startedAt` recent (≤ 600s) | genuinely in flight | KEEP (do not kill a legitimate wake) |
| `state == "waking"`, `startedAt` older than 600s | stuck orphan (poll loop is gone) | END |

Threshold: `wakeMaxInflight = 10 * 60` seconds (600s). A wake takes up to ~9
minutes, so something still "waking" past 600s has long exceeded the real budget
and can only be a dead process's orphan — the app's poll loop would have settled
and ended it long before. Keeping the budget above the true maximum guarantees we
never cut a wake that is genuinely in progress.

Evidence (EXECUTED): `live_activity_rehydration_check.sh` — all rows of the
decision table pass (settled states end; recent "waking" kept; nil startedAt ends;
over-budget "waking" ends; exact-boundary 600s kept, 600.001s ends).

## The session sweep (`sweepLeftoverSessions`)

A session activity is a *passive mirror*: it exists only while the operator is
actively viewing a project's live chat, and is re-derived fresh from the chat rows
every time that view is on screen. There is no long-lived in-flight operation that
must survive a restart — a frozen session bubble is never legitimate. So on a
fresh launch, when nothing is being reflected yet, every leftover session activity
is an orphan → end them all. If the operator reopens the live chat,
`SessionActivityDriver.reflect` lazily starts a fresh activity, so ending the
orphan loses nothing. (That is why the session side needs no age heuristic — the
"in flight operation" property the wake heuristic protects simply does not exist
for a mirror.)

## Why onAppear and not App.init

`HSCCApp.init` runs before the scene appears; ContentView's `TabView.onAppear` is
the earliest reliable launch hook and is where the other one-shot launch setup
(connection probe, approval/reply-watcher wiring) already lives, so the sweep sits
next to them. The sweep methods are synchronous (they spawn their own background
Tasks for the async `end`), matching the existing `endCurrentSilently` pattern, so
the call is a plain synchronous statement — no `.task` cancellation semantics to
reason about. Both sweeps are idempotent and safe to run once at launch.

## Caveat: what we deliberately do NOT do

- We do NOT re-attach and re-drive a genuinely-in-flight wake's poll loop. If the
  process is killed 2 minutes into a wake, the sweep leaves the (legitimate)
  bubble alive, but the new process has no handle to keep updating it and no poll
  loop. It keeps showing elapsed time (the extension renders it from `startedAt`),
  and the actual server-side wake still completes on its own. This is the explicit
  trade-off the card describes ("naive 'end everything' could kill a legitimate
  wake") — we prioritise not killing a legitimate wake over perfect re-attach.
  Re-attach would require the sweep to know the HSCC client and would conflate
  launch-side cleanup with live driving; it's out of scope here and larger than the
  orphan problem this card targets.
- We do NOT add `staleDate` (deliberate non-fix carried from t_10bc37be): it does
  not end an orphan and would falsely mark a healthy self-polling wake stale
  between polls.

## Verification

- build_check.sh (EXECUTED): 4/4 targets clean — HSCC (58 files),
  HSCCWidgets, HSCCLiveActivity, HSCCLiveActivitySession — 0 errors, 0 warnings.
- live_activity_rehydration_check.sh (EXECUTED): decision table all pass.
- session_activity_check.sh (regression, see below): run to confirm the session
  derivation is untouched.
- Cannot be runtime-proven on this host: no ActivityKit / iOS runtime, so the
  actual `Activity.activities` sweep cannot execute here. The compile proves the
  API surface (`Activity<...>.activities`, `.content.state`, `.end`) is used
  correctly; the headless check proves the decision logic. On-device runtime
  verification is listed in smoke test notes.
