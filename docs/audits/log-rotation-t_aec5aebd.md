# REPORT t_aec5aebd — daemon.log is 299MB with no rotation: rotated both daemon log paths

Date: 2026-09-09 (EEST, UTC+03:00)
Assignee: devops-engineer
Branch: wt/log-rotation

## Summary

`~/.hscc/daemon.log` had NO rotation and grew unbounded. It is now size-capped
via the stdlib `logging.handlers.RotatingFileHandler` (chosen deliberately:
the boring option that already implements size-based rollover, generation
retention, and per-handler locking — no hand-rolled loop). Each rolled
generation is gzipped. The fix covers every writer that goes through
`daemon_ops.log()` (daemon.log **and** api.log) plus `event_driven._event_log`,
so no writer can leave the other half-growing.

## What was fixed (evidence)

- `hscc_daemon/daemon_ops.py`
  - `LOG_MAX_BYTES = 25 * 1024 * 1024` (rotate at ~25 MiB)
  - `LOG_BACKUP_COUNT = 3` (retain 3 rolled generations)
  - `_GzipRotatingFileHandler(RotatingFileHandler)` — `rotation_filename`
    suffixes `.gz`; `rotate()` gzips the source into the destination and
    removes the source.
  - `_get_logger(log_file)` — lazily builds a Logger+handler pair per resolved
    absolute path (thread-safe, cached).
  - `append_rotating(log_file, line)` — shared write funnel; never raises.
  - `log()` now routes through `append_rotating()` instead of a raw
    `open(..., "a")` append.
- `hscc_daemon/event_driven.py` — `_event_log()` now routes through
  `append_rotating()` too (same convention; keeps the whole daemon log writing
  through one capped funnel even though event_driven is currently not wired to
  the live path).
- `hscc_daemon/tests/test_daemon_ops.py` — new `TestLogRotation` class
  (4 tests): size cap, gzip generations valid + content preserved, retention
  limited to backupCount, and existing-large-file-never-truncated.

Verified: `/Users/desac/miniconda3/envs/p313/bin/python -m pytest -q
hscc_daemon/tests/test_daemon_ops.py` → `35 passed`. Functional smoke:
writing well past the cap on a temp log yields `daemon.log` ≤ cap plus
`daemon.log.1.gz` / `daemon.log.2.gz` valid gzip archives.

## Cap choice from measured growth

Evidence from the live `~/.hscc/daemon.log` (read-only, never modified):

- Current size: 303,450,140 bytes ≈ 289.4 MiB
- First timestamp: 2026-05-27 17:15:04; last: 2026-09-09 07:43:30
  (104.6 days span)
- Average growth: 2.77 MiB/day over the whole file
- Recent per-day (from timestamp/byte histogram at end of file):
  2026-08-29 3.41 MiB, 08-30 3.55, 08-31 3.50, 09-01 3.54, 09-02 3.51,
  09-03 3.84, 09-04 3.53, 09-05 3.46, 09-06 3.46, 09-07 3.45, 09-08 5.15
  (outage day), 09-09 2.41 (partial)
- Recent sustained rate ≈ 3.5 MiB/day.

Cap rationale: 25 MiB / 3.5 MiB/day ≈ **7 days of active log per generation**.
With 3 retained gzipped generations at ~10% compression (rotated files gzip to
~1/10 their size → each ~0.35 MiB at full), several more weeks of history
survive. Total on-disk footprint for daemon.log: ~25 MiB live + ~1 MiB gz,
vs 289 MiB unbounded today. This is comfortably above the 3.8 MiB stable day
rate and even the 5.15 MiB outage spike, so a few days always survive even
during an incident.

## Both daemon log paths checked

The daemon is launchd-supervised and the plist sets
`StandardOutPath`/`StandardErrorPath` → `~/Library/Logs/hscc_daemon.log` in
addition to its own log.

- **~/.hscc/daemon.log** (the 289 MiB one): now rotated. This is the real
  growth vector.
- **~/Library/Logs/hscc_daemon.log**: verified **0 bytes** and never written.
  In the supervised `start-daemon` path the daemon saves a pid file
  (`cli.py` `cmd_start_daemon` → `save_pid()`), and `daemon_ops.log()` only
  prints to stdout when `not os.path.exists(pid_file)` (daemon_ops.py, the
  `if not os.path.exists(pid_file): print(line)` branch). So while supervised,
  the daemon writes everything to its own (now-rotated) log and nothing reaches
  the launchd path. The prior t_a6697001 audit reached the same conclusion
  ("which launchd never creates because the daemon writes to its own log file
  while a PID file exists").

Conclusion: the only genuinely growing path was `~/.hscc/daemon.log`, and it is
now fully rotated. The launchd path is dormant by design; no "half-fixed" bug
remains. (If it ever did receive traffic — e.g. an uncaught traceback storm
during a crash-loop — a `/etc/newsyslog.d/` entry would be the correct external
fix; that is reported, not done, as it needs root and is not a live problem.)

## Existing 289 MiB file left untouched

Per the task rule, rotation applies going forward only. The existing file was
NOT truncated or deleted. `append_rotating()` opens the target in append mode
(a stdlib RotatingFileHandler behaviour) and only rotates once the size cap is
crossed by subsequent writes; verified by the
`test_existing_large_file_untouched_until_cap` test. If the operator wants the
289 MiB file archived (e.g. gzipped into `~/.hscc/archive/` to reclaim disk),
that is a separate one-off action for them to run — not part of this change.

## Other potential unbounded growth (reported, NOT fixed here)

Survey of `~/.hscc` (304 MiB total) and `~/Library/Logs` (184 KiB):

- **~/.hscc/daemon.log — 289 MiB** — THE fix in this card. ✓ rotated.
- **~/.hscc/api.log — 12 KiB** — now also rotated for free (it is written via
  `daemon_ops.log(..., log_file=API_LOG_FILE)`, which now routes through
  `append_rotating`). Small today but no longer able to grow unbounded.
- **~/.hscc/relaunch-*.log — one at 620 KiB / 18,997 lines** (others 4-72 KiB).
  These are per-orchestrator relaunch logs and the largest is growing. They do
  NOT go through `daemon_ops.log()` (they are written by a relaunch/exec path),
  so they are NOT covered by this fix. Candidate for a future card: route them
  through the same rotation or cap them. Out of scope here.
- **~/.hscc/*.jsonl (tool_events 484 KiB, events 112 KiB, task_completions
  28 KiB, blocked_tasks 40 KiB)** — JSON-lines, append-only, actively appended
  (tool_events last modified 2026-09-09 08:56). No size cap observed. Likely
  bounded in practice per rollover elsewhere (events.jsonl last touched
  2026-06-04), but worth a scoped look.
- **~/.hscc/notifications.json — 4.0 MiB** — no cap observed. Candidates for a
  separate card: cap/reap old notifications.
- **~/.hscc/serving.json.bak.* / trigger.json.bak / watchdog-block.json.bak** —
  these ACCUMULATE (multiple 4 KiB files seen, timestamped).
  `prune_dead_files()` already caps `.bak.*` groups (tested) — confirm it is
  run on the schedule. Minor.
- **~/Library/Logs** — everything small (DiagnosticReports 160 KiB, etc.);
  `hscc_daemon.log` 0 B. No unbounded growth.

Recommended follow-up cards (NOT done here, per scope): cap the
`relaunch-*.log` files; add rotation/cap to the append-only `*.jsonl` and
`notifications.json`; (optionally) run `prune_dead_files` on a schedule to reap
`.bak.*` accumulation.

## What I did NOT do (and why)

- Did NOT truncate/delete the operator's existing 289 MiB daemon.log — per task
  rule; if archived it must be by the operator (I recommend gzipping it to
  reclaim ~289 MiB; that is a separate one-off, not part of the rotation).
- Did NOT modify the launchd plist / `~/Library/Logs/hscc_daemon.log` — it is
  dormant (0 bytes) by design and needs no rotation; changing the plist would
  not help since the daemon suppresses stdout while a pid file exists.
- Did NOT add a `/etc/newsyslog.d/` entry for the launchd path — needs root and
  the file does not grow; reported as the correct external fix if it ever does.
- Did NOT run `hscc doctor` / bootstrap.sh against the live runtime (they MUTATE
  ~/.hermes/config.yaml). Growth measurement used read-only file scans.
- Did NOT use Telegram or send any mutating POSTs at live state.
- No host/IP/token hardcoded anywhere committed; the relaunch-*.log filenames
  contain real LAN addresses so I referenced them generically in this report
  (test_no_real_addresses_committed.py passes).
