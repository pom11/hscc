kind: Fixed
task: t_bcc4e414

- **A waiting PR that was red for its own reasons stayed waiting in silence.**
  The merge queue's wait list only ever pulled a PR back into the FIFO once its
  checks went green. Two kinds of red had no exit at all: the branch would not
  move onto the base (`gh pr update-branch` refused with a conflict, which the
  queue never retries), and CI came back red *again* on the base the queue had
  just moved it onto. Neither is something the queue can fix, and neither was
  ever said out loud — the author only found out by opening the PR, which is how
  a card sat untouched for a day. The per-tick wait-list pass now hands such a PR
  back through the same path a failed gate uses: a PR comment naming the failing
  check(s) and the base they were red against, plus a board handback to the
  entry's card and assignee. An entry with no card (a manually added PR) resolves
  one from its branch name — `wt/<card>` is the card — and reads the assignee off
  the project's board. The entry stays in the wait list: a hand-back is a
  message, not a drop, and the queue still returns the PR to the FIFO when its
  checks go green. `handed_back_main_sha` bounds it to one hand-back per base
  commit per PR, re-armed when main moves — so a PR that stays red cannot
  re-comment every tick, and no author is blamed for a base that has since been
  fixed.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_merge_queue.py`: 147 -> **153** cases. The new ones
  pin the counts rather than the prose — a conflict produces EXACTLY one PR
  comment and one board handback across five ticks, red-after-refresh produces
  one and is re-armed only by a newer main sha (which first earns the PR its one
  free refresh, so a stale base is never the author's fault), a green or closed
  PR produces zero of both, a card-less entry lands on the card its branch names
  with the board's assignee, and the comment body carries the failing check names
  and the base sha it was tried against. One case injects neither a git runner
  nor a handback, so the drain runs the daemon's own `_default_runner` and
  `default_handback`. Because the drain can now reach `default_handback` — a
  write to the live board — `hscc_daemon/tests/conftest.py` fences it to the
  no-hermes answer, the same pattern as the process-table fence.
