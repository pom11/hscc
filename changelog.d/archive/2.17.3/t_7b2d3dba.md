kind: Fixed
task: t_7b2d3dba

* **A board whose orchestrator has no pane and is never expected to get one is
  now SKIPPED, not retried forever.** The waker nudges the orchestrator pane
  every `tick_s` (30 s), and on `general` — the install's own main board — every
  one of those nudges failed identically for as long as the daemon ran: 138
  byte-identical `no pane for profile 'general-orch'` audit lines at the time of
  writing, one per tick, with `pending` climbing and never draining. The target
  is not a dead pane that a restart would fix: `general-orch` is the **retired
  cluster-wide identity** `hscc-roles/main_orch.py` describes — "every
  pre-migration install still has `general-orch` on disk (retired, never
  deleted)" — whose own `SOUL.md` says do not create, dispatch or claim work
  here, and which has NEVER run a task (`task_runs.profile` across every board
  DB: zero rows). `resolve_orch` approves a `<board>-orch` directory on
  existence alone, so the waker kept nudging an identity that can never answer,
  and the per-board backoff (5 attempts / 900 s) just restarted forever — every
  30 s a `FAILED` audit line, a `general -> general-orch: FAILED` on the board
  and a `fails: 1` in the health stamp: noise that reads as a fault while the
  real condition (this board is muted) is never named.
* The waker now asks the question the dead-pane detector cannot: **is a pane
  expected here at all?** A skip requires TWO independent halves, because each
  one alone has a live failure mode: (a) the pane sweep ran successfully and
  found zero target panes — a failed sweep is indistinguishable from an absent
  pane to `repl_inject`, so an `shutil.which` hiccup must never be read as
  "gone", and `orch_waker` already refuses to declare a pane dead on a failed
  sweep for exactly this reason; and (b) hscc's own records say no pane should
  exist — the orchestrator profile's `SOUL.md` carries `main_orch`'s retirement
  marker, or the target is the legacy cluster-wide identity on the board the
  main orchestrator itself owns (`main_orch.orchestrator_for("general") ==
  "general"`), which is the `general` case, where the identity was never retired
  at all and is still the board's orchestrator. Either half alone falls through
  to the pre-existing retry path: a pane-resident orchestrator whose tmux
  session died keeps its retry AND its pane-recovery restart, and a
  dispatcher-dispatched orchestrator that DOES have a pane (the pane the waker
  nudges) is untouched.
* **The skip is a skip, not a failure**: the pending batch drains into the skip
  record instead of accumulating (the events are permanently undeliverable to
  this target; a `claim_release` would hand them back and rebuild the backlog
  the card is trying to end), no audit line is written (that audit line is what
  the operator reads as a fault — the skip says itself, in the state file and in
  `hscc` health status, and does not need to shout every 30 s), no `sent` stamp,
  no wake claim, and no `decision.ok` that would count as delivered.
* **A permanently muted board cannot be silent** (t_54c5d165 / t_628c62f7's
  lesson, learned here on the same board class): the skip is one `WARNING` per
  `backoff_s` window, not per tick, and the tick's stream entry carries a
  machine-readable `paneless` list plus a message that NAMES the board and the
  reason — so `steady` can never again be what a muted board reports.
  `waker_status` exposes `paneless_skip` entries (`board`, `profile`, `reason`,
  `ticks`, `since`) and `tick`'s `report["paneless"]` the same, and a board that
  later gets its pane back clears its own record on the first delivery.
* Not a config entry: no board or profile slug is special-cased anywhere in the
  fix. `skip_boards` stays exactly as it was — the card forbids adding a slug
  there because the dispatch path (`hscc_api.wakeups` → `main_wakeups`) reads
  the same set and `general`'s wake-ups depend on dispatch keeping working; the
  new state key is merged by `stamp_entry` (t_fa9a2faf's collision rule: the
  stream stamp and the waker's own state file are the same document) so the
  announcement window cannot be silently erased each tick.
---
kind: Verified
task: t_7b2d3dba

* Measured on the live install before the change (`NOTES-t_7b2d3dba.md`):
  `orch_waker.jsonl` holds 138 `general -> general-orch` lines, all the same
  failure, one per ~30 s tick; the pane sweep at the time of the report found
  panes for `general`, `hscc-orch`, `pontaje-orch` and others and none for
  `general-orch`; `general`'s own orchestrator duty is dispatcher-dispatched
  (`task_runs.profile` for that board: `general=2`, `hscc-orch=2`).
* A retired-identity sweep over every profile `SOUL.md` found exactly one
  profile carrying `main_orch.RETIRED_MARKER` (`general-orch`) and nineteen other
  `<board>-orch` profiles unretired — the fix is structural precisely because it
  must not fire on any of those nineteen.
* `hscc_daemon/tests/test_orch_waker_paneless_skip.py`, 31 tests, hermetic: fake
  profile dirs and tmp board DBs (no live tmux, no live boards, no real
  profiles, no host address of any kind — `resolve_orch` is always injected and
  `paneless` is a seam). Pinned: both halves required and neither alone enough;
  pending drains on skip with no audit line, no `sent` stamp and no wake-claim
  row; a pane-resident orchestrator with a failed sweep still retries and still
  gets its pane-recovery restart; `hscc`'s real `main_orch.RETIRED_MARKER` is
  what the reader looks for, so a marker edit fails this suite rather than
  silently disabling the skip; dispatch (the board's `tasks` / `task_runs`
  claim columns) is byte-identical across a skipped nudge and a delivered nudge
  and survives a failed sweep; one log line per window while `n` keeps counting;
  the skip survives the stream stamp.
