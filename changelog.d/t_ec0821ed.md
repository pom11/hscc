kind: Fixed
task: t_ec0821ed

* **The merge queue could not drain: `waiting` was only revisited when every
  FIFO was empty.** `_pr_ready` required `mergeable == "MERGEABLE"`, so the
  `UNKNOWN` GitHub reports for every open PR for a few seconds after the base
  moves was treated as "not ready" and the entry was parked in `waiting`; the
  only path back was auto-enqueue, which runs only on an empty FIFO. The same
  trap caught every just-pushed PR, because its post-push CI is pending by
  definition. Measured on the live host: 10:45–13:15 UTC with 14 PRs queued and
  nothing landing. `process_entry` now re-polls `gh pr view` a bounded
  3 x 5 s while `mergeable` is `UNKNOWN` and, if it still is, gives the entry
  back at the HEAD of the FIFO (never `waiting`).
* `waiting` is re-checked on EVERY tick with one cheap `gh pr view` per entry,
  whether or not the FIFO is empty; green entries return to the queue in their
  wait-list order. A pushed head whose CI is still pending now KEEPS its
  in-flight claim at the new `ci-pending` stage — no re-rebase, no re-gate —
  and is re-read each tick until green (merge that exact SHA), red (comment,
  hand the card back as `red-ci`) or the 1800 s CI budget expires (release to
  the rechecked wait list as `ci-timeout`, not a resolve).
* `check_merge_queue` now passes the daemon log into the tick, so merge-queue
  outcomes actually appear in `~/.hscc/daemon.log` (the previous default `log`
  was a no-op lambda); the `ci-pending` stage renders in the monitor table.
* Tests: `hscc_daemon/tests/test_merge_queue.py` 92 -> 100. New cases cover
  UNKNOWN -> MERGEABLE re-poll, still-UNKNOWN keeping the FIFO head, a green
  waiting PR draining while the FIFO is non-empty, a `ci-pending` claim resumed
  without a second gate run, red CI handing back as resolve, the CI timeout
  releasing to `waiting`, the tri-state checks verdict, and the daemon-log
  wiring. Existing post-push cases were updated to the held-claim contract.
