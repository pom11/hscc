kind: Added
task: t_28caea71

* Node pause overlay core: `hscc_daemon/node_pauses.py` keeps a durable
  per-node pause record in `~/.hscc/node_pauses.json`, written atomically
  (tmp file in the same directory, fsync, `os.replace`) so a pause survives a
  daemon restart and is visible to whichever process asks — the CLI that set
  it or the daemon loop that has to honour it. Before this there was nowhere
  to park one node: every other knob (templates, `serving.json`, the
  applied-template record, kanban caps, autodown, proxy config) is cluster
  source of truth and has an apply or heal path that would fight the edit or
  restore the node from it. The new module writes exactly one file — its own
  overlay — and nothing else.
* API: `load()` / `save()`, `pause(host, reason=, until=, units=,
  span_nodes=, stopped=)`, `clear(host)`, `paused(host)`, `list_pauses()`,
  `covering(host)` / `paused_nodes()` (span-aware: a tensor-parallel unit stops
  as a whole, so a repair path that asks only for the keyed host would relaunch
  the other nodes of the span), `pending_resume(now)` and `expiry_pass(now)`.
  A pause past its `until` is still a pause until something resumes it: the
  clock ends a pause only in `expiry_pass`, never in `paused()`, so no heal
  path can decide a parked node is free the moment a deadline passes. A pause
  whose `until` cannot be parsed is read as open-ended, never as expired.
* Absence fails open, corruption fails loud: a missing, unreadable or malformed
  overlay reads as "no pauses" — it never raises into a repair decision — and
  every malformed shape is WARNed through the daemon's log convention, because
  a pause the operator believes is in force and cannot be read is exactly the
  thing they must find out about. `save()` propagates write failures rather than
  swallowing them: a pause that never reached the disk did not happen.
* Test seam: every read and every write takes `directory=`, the explicit-root
  seam `cron_watchers.py` uses, with no environment override for the default
  root (an env-var root is per-process, so it is not a root another process can
  have written). The 69 new tests use documentation-range hosts and a temp-dir
  overlay, and the no-live-`~/.hscc` guard now plants a realistic pause and runs
  these tests inside a sandboxed HOME.
