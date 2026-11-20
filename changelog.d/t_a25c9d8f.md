kind: Fixed

- **A tick no longer clobbers a concurrent `hscc merge-queue add`.** Auto-enqueue
  probes the network first, then reloads the ledger under the file lock and
  merges only its candidates into that fresh document. A PR added while the
  probe was running stays queued instead of being erased by the tick's
  start-of-tick snapshot.
- **A red-gated PR is not auto-re-enqueued until its head SHA changes.** The
  red-gate outcome now records the head it gated; auto-enqueue compares the
  PR's current `headRefOid` against it and skips the candidate at the same
  SHA, so the gate, PR comment and card handback do not repeat every minute.
  A worker push (or an explicit operator `add`) makes it eligible again.
- **A non-green head leaves the head slot for a wait list.** Entries waiting on
  pending checks move to `waiting` instead of being reinserted at position 0,
  so later FIFO entries keep running. Wait-listed PRs are retried when the
  active queue is empty (or promoted when auto-enqueue sees them go green) and
  appear in the CLI/API/widget view in their own `waiting` state.
- **`view()` reports the ledger's last write time, not the read time.**
  `generated_at` is stamped on every state write (including a no-work tick), so
  two reads with no tick between them share one timestamp. An `ok:false` route
  answer now maps to an unavailable feed in `MergeQueueReading` and renders as
  offline/failed in `MergeQueueWidget`, never as a fresh empty queue.

---
kind: Verified

- `hscc_daemon/tests/test_merge_queue.py` (40 passed) and
  `hscc_daemon/tests/test_kanban_live.py` (54 passed): 94 passed. New cases
  cover the concurrent CLI add, red-gate suppression until the head moves,
  the wait list letting FIFO entry #8 merge while #7 is pending, and
  `generated_at` stability plus the idle-tick stamp; each fails on the pre-fix
  file.
- `hscc-api/tests/test_routes_merge_queue.py` and
  `tests/test_widget_route_parity.py`: 7 passed.
- `ios-app/scripts/model_decode_check.sh`: 363/363 checks, including the new
  `v1_merge_queue_not_ok.json` fixture (ok:false is offline, not empty).
  `build_check.sh`: 4 targets, 0 errors, 0 warnings;
  `widget_style_check.sh`: passed.
- CLI smoke under a temp `HSCC_HOME`: `merge-queue add/status/list/remove`
  round-trips (status shows `waiting=`), and
  `python3 scripts/address_guard.py --tracked` is clean.
