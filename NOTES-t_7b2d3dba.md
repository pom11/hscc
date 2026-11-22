# t_7b2d3dba — orch-waker FAILED-loops on a board whose orchestrator is retired

Result: **root-caused to a retired profile that `resolve_orch` still approves;
fixed structurally (pane-expectation detection), not by a config entry.**

Every number below is from the live host's own state files and the live board
DBs, read-only, and from a clean worktree of `main`. No fleet node is involved
(the waker, its state and the panes are all on this host), and no test in this
card touches tmux, a live board or a real profile.

## 1. The symptom, as measured

`orch_waker.jsonl` (the waker's own audit) — 138 lines for board `general`, all
of them the same failure, one every ~30 s (the stream cadence):

```
{"board": "general", "profile": "general-orch", "ok": false,
 "outcome": "failed: ... tmux pane running a Hermes REPL for profile
             'general-orch' (found: ecofire-app-orch@0:1.0, ...,
              general@0:2.0).", ...}
```

The `found:` list is the live pane inventory: **a `general` pane exists, a
`general-orch` pane does not and never will.** The failing kind is
`repl_inject.InjectError(kind="no_panes")` — a permanent fact, not a tmux
hiccup. Pending therefore never drains (the core keeps `pending` for retry, by
design) and the evidence trail grows one identical line per tick.

## 2. Why the waker targets a profile that can never answer

`resolve_orch` (orch_waker.py:1371) resolves a board to `<board>-orch` if that
**profile directory exists** — existence on disk is the whole test. It has no
idea about panes, and it has no idea the profile was retired.

`hscc-roles/main_orch.py`'s own docstring is the design record:

> The 2026-11-19 design replaces the two old cluster-wide agents
> (`general-orch` and the `orchestrator` role profile) with ONE main
> orchestrator named at bootstrap, default `general` … The default `general` is
> exempt because **every pre-migration install still has `general-orch` on disk
> (retired, never deleted)**.

So `general-orch` is a *deliberately kept, deliberately retired* directory. It
is the one profile on this install that carries `main_orch.RETIRED_MARKER` in
its `SOUL.md`:

```
### profiles carrying the retired marker        (probe over every profile dir)
  RETIRED: general-orch
```

and its SOUL text says plainly: *"This profile is kept on disk only for history
— do not create, dispatch or claim work here."*

Is its board's orchestrator duty served elsewhere? Yes, and the board DB proves
it (`task_runs.profile` is the dispatcher's own record of who ran each card):

```
  general: orch-ish=['hscc-orch=2', 'general=2']   # general-orch: never
```

`general-orch` appears in **zero** runs on any board; the orchestrator work on
board `general` is executed **dispatcher-dispatched** (profile `general`, plus
`hscc-orch`), exactly the shape the card describes. Dispatch itself is
unaffected by all of this — the waker never dispatches; it only ever writes one
additive `kanban_wake_claims` row when it sends.

The dead-pane backoff (t_f1122b7a) already limits the noise to one attempt per
`backoff_s` (900 s), and `skip_boards` was used for exactly this class twice
(`gateclerk`, `pontaje`, `ecofire-pontaje` in the live config). But it is not
retired-aware: it cannot tell "pane gone, operator will restart it" from
"this identity is retired and will never have a pane".

## 3. The fix, and why it is structural

The card's rule: a board whose orchestrator has **no pane AND no pane
expected** is skipped/backoffed, pending drained — and `skip_boards` must not
gain a slug, because that knob feeds `run_tick`'s `paused` set
(orch_waker.py:2390), which also drops the board's wake path.

Decision: add a **pane-expectation verdict** consulted at the one moment it
matters — after every delivery gate, immediately before `inject`:

```
skip the nudge  ⟺  (A) CONFIRMED zero panes for the resolved orchestrator
                       profile — from a pane sweep that SUCCEEDED
                       (blindness is unknown, and unknown never skips), and
                    (B) no pane is EXPECTED for that profile, evidenced by
                       hscc's own retirement record: its SOUL.md carries
                       `main_orch.RETIRED_MARKER`, or it is one of
                       `main_orch`'s exported legacy cluster-wide identities
                       for the board the main orchestrator owns.
```

Both halves are required, for opposite failure modes:

* (A) alone would silence a board whose *live* orchestrator pane is briefly
  down — the operator reopens the pane and expects the batch to go out. That
  stays on the existing dead-pane backoff, retry and all.
* (B) alone would blind a nudge that still works: a retired profile with a pane
  open in tmux can still be talked to, and a working nudge must not be
  switched off by a label.

Because (B) is read from the marker hscc's own migration writes
(`main_orch.RETIRED_MARKER`, the documented idempotence key of both writers) and
from `main_orch`'s exported legacy-identity constants, no board slug appears in
code. The measured discriminator is clean on this install: the detector fires
for exactly `general` and for none of the 19 other boards that have a
`<board>-orch` profile.

On a skip the waker does nothing but drain: `pending.pop(board)`, no `inject`
call, no audit line, **no wake-claim taken** (a claim taken by a waker that will
not speak mutes the gateway notifier for `lease_s` — the "dropped wake dressed
as dedupe" this module already documents), and the `last` watermark still
advances so the drained events are never re-examined. The verdict is re-evaluated
every tick, so a board whose pane appears later resumes nudging on that tick;
only the log line is rate-limited to one per `backoff_s` window, and the set of
skipped boards is stamped on the stream (`report["paneless"]` → stream entry →
the doctor's `message`), because a waker that is skipping a board must never
stamp a bare `steady`.

## 4. Alternatives considered and rejected

* **Add `general` to `skip_boards`.** Forbidden by the card and wrong on the
  merits: `run_tick` ORs `skip_boards` into `paused`, which drops pending *and*
  is the knob an operator uses to mute a board wholesale. A retired identity is
  a property of the *profile*, not a request to stop caring about the board.
* **Re-resolve `general` → the `general` pane and nudge that instead.** Tempting
  (that pane is at `0:2.0`) and rejected: the main orchestrator's board already
  gets its wakes from the dispatcher and the gateway notifier, so a second
  emitter into the same pane is a double wake, and the card's directive is
  skip/backoff. Named here so the next worker does not "fix" it that way.
* **Detect "dispatcher-dispatched" from `task_runs` forensics.** Rejected as a
  *primary* signal: run rows are a moving target (a quiet orchestrator looks
  un-dispatched), so the verdict would flicker, and flicker in a skip path means
  occasionally silencing a live board. Kept as the *measurement* that proves the
  retired identity is inert (§2), not as the predicate.

## 5. Left open (deliberately not in this diff)

* A board whose orchestrator profile is **live** but permanently pane-less and
  dispatcher-dispatched (not retired) still lands on the t_f1122b7a backoff —
  one failed attempt per `backoff_s`, not per tick. Nothing on this install is
  in that state today (measured: exactly one retired profile, no live
  pane-less-and-dispatched orchestrator), and the honest signal for it is the
  dispatcher's own "this profile is dispatched" fact, which hscc does not
  expose yet. Card it if a second board starts failing this way.
* `doctor` has no row for the paneless-skip set. The stream `message` carries it
  (so `hscc verify`/`doctor` print it), and a dedicated row is its own card in
  the `hscc-doctor-row` shape.
