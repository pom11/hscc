# Overnight 2026-09-23/24 — v2.1.1

Released, deployed, verified. Two cards landed; one of them was cleaning up
after a mistake I made earlier in the evening.

## Released

**v2.1.1** — `c3250de`, tagged `v2.1.1`, pushed. 13 commits since v2.1.0.
Patch, not minor: no commands were added or removed.

| check | command | result |
|---|---|---|
| suite, default interpreter | `bash scripts/run_tests.sh` | 787 passed, 8/8 packages |
| suite, p313 | `python -m pytest <pkg>/tests -q` | hscc-api 787, hscc-roles 101, hscc-bootstrap green |
| address leak, whole range | `git diff v2.1.0..main \| grep -oE '<lan>'` | none |
| secrets | regex over the range | 0 |
| AI attribution | `grep -icE '\bclaude\b\|\banthropic\b'` | 0 |
| pushed | `git rev-list --count origin/main..main` | 0 |
| deployed | `cat ~/.hermes/plugins/VERSION` | 2.1.1, matches repo |
| payload really in sync | `diff` on 3 changed files | all in sync |

## What landed

**t_7a8f126c — the `architect` assignee rewrite, solved after four failed
investigations.** The `hscc-escalate-watcher` cron (`6407ea32e1dd`, active,
every 15 min) reassigned any card with `consecutive_failures >= 3` to a
hardcoded `architect` profile, which cannot execute cards — the worker died
with 0 tool calls, rc=0. Now opt-in; at-threshold failures report to a human.

Verified independently rather than taken from the report: the cron is live,
`escalate.py:41` and `escalate_watcher_run.py:40` both hardcoded `architect`,
and the regression test genuinely fails without the fix — 47 passed with it,
3 failed without, including `test_default_does_not_reassign_to_architect`.

Why it hid so long: card *creation* was always correct. The rewrite happened
later, from a scheduled job, and only to cards that had already failed three
times — so it looked intermittent and content-independent. The standard
recovery (reassign + unblock) worked by resetting the failure count, removing
the trigger and hiding the cause.

**t_f75c13aa — 11 stale tests hardcoding the old 100000 compaction cap.**
This card exists because of my error: I raised the constant, verified
`hscc-roles` (101 passed, both interpreters), and treated that as green without
reading the full suite I had started. It was red — 8 failed across
`hscc-bootstrap` and `hscc-api`. The orchestrator caught it and raised the card
itself.

Also relocated `CARD.md` and `FINDINGS.md` out of the repo root into
`docs/audits/` (`1d5c423`).

## Stack

- daemon restarted (its `escalate.py` / `escalate_watcher.py` changed), PID
  85905, all 9 streams OK
- API PID 43760 on `:8788`, untouched — no API code changed
- cluster: both TP pairs up 12 days
- gateway NOT restarted

**The daemon was found DOWN before the restart — empty `~/.hscc/daemon.pid`,
nothing surfacing it. Second time in one day.** The skill now carries a
liveness check, but the daemon still has no health signal of its own: the board
and API both looked fine while it was dead.

## Could not verify

**memori_byodb captures nothing yet.** `max(date_created)` in
`memori_conversation_message` is still `2026-06-13`, 6 rows. This is expected
rather than a failure — the orchestrator session has not taken a turn since the
provider was enabled (rowid frozen at 16293 for hours), and it cannot capture a
conversation that has not happened. Re-check after the next real session.

**Compaction has still never fired at the new 200000 cap.** The session sat at
~146K/262144 and idled. The fix is proven in code and config but not in
practice.

## Open

- `hscc` CLI shebang still points at miniconda p313, which lacks the Local
  Network grant (errno 65) and cannot import `hermes_cli`. Repointing it at the
  Hermes venv fixes both. The TCC route was attempted and the pane toggles did
  not take.
- Bootstrap still does not own profile `memory:` or `auxiliary.compression`.
  All 14 orchestrators were corrected by hand tonight; a `bootstrap` run will
  not preserve that. Roadmap milestone `profile-provisioning` records it.
- The daemon needs its own liveness signal.
