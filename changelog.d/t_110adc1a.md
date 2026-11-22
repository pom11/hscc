kind: Added
task: t_110adc1a

- **A past-due `hscc node pause --until` now resumes itself, from disk.** The
  overlay has recorded `until` since 2.17.3 and nothing read it, so the deadline
  was a note to a future operator rather than a deadline: `hscc node paused` would
  print `PAST DUE; resume it …` forever. The daemon now runs a `node_pause_expiry`
  stream (`daemon_ops.PERIODIC_INTERVALS`, 60 s — the `kanban_enforce` class, free
  when idle) whose body is `auto_resume_expired` / `expiry_tick` /
  `check_node_pause_expiry` in `hscc_daemon/node_pause_exec.py`, next to the resume
  it performs. **The schedule is the overlay file, not memory**: each pass re-reads
  `node_pauses.list_pauses()` and compares every `until` against a clock it reads
  then, so a daemon crash or an upgrade in the middle of a pause loses nothing —
  which is exactly what an in-memory `threading.Timer` cannot promise, and why one
  is not used (a pause held in memory is a pause that silently expires on a daemon
  restart, and the watchdog then "heals" the paused unit back to life on top of
  whatever the node is doing).
* The resume is the **same code the verb runs**, not a copy of it. The card asked
  the tick to "call the CLI/executor"; the tick calls `node_pause_exec.resume_host`,
  which is now also what `hscc node resume … --apply` delegates to — one resume
  policy with one dry-run gate (#478), one rebuild-the-launch-command-or-refuse
  rule, and one ordering (restart the units, *then* clear the pause, *then* clear
  heal strikes). What the card's words would have done — capture the verb's stdout,
  as `hscc-cluster/node_pause_tools.py` does with `contextlib.redirect_stdout` — is
  fine for a plugin tool and wrong for a daemon: it swaps a process-global while
  ~14 supervised stream threads may be printing, and it makes the tick's behaviour
  depend on a message that was written for a human. The executor takes `echo` and
  `run` seams instead. `TestResumePolicyIsShared` pins the delegation behaviourally
  and with an AST guard that fails if the verb grows its own resume again.
* **A pause that cannot be safely ended is kept, not cleared** — and the failure is
  reported. `node_pauses.expiry_pass()` is deliberately *not* used: it drops every
  expired entry at once, including one whose restart just failed, which is "the
  record is cleared because the tick ran" — the node would be back in the heal paths
  with units half up. Here the clear is per host and only after a restart that
  succeeded; a failed restart keeps the pause *and* the heal strikes and is retried
  next tick (that retry, needing no memory, is the durability the card asked for).
  A due pause whose `span_nodes` overlap a pause the operator still holds is
  **skipped** — restarting it would re-issue a whole multi-node workload onto a node
  that has not been released — and both outcomes make the stream red: a schedule
  that is not being honoured is the "policy is not being applied" class
  `kanban_enforce` already established, not a healthy idle.
* Expiry and the span decision come from **one** overlay read
  (`_due_entries` / `_nodes_still_held` over a single snapshot), because
  `node_pauses.load()` re-normalises into fresh objects — identity comparisons
  across two reads silently never match, and the overlay is written by the CLI in
  another process.
* `hscc node resume` gained the half a tick needs and a single verb never asks for:
  it **refuses to resume half a span** (`resume_host` rule 0.5) — if another pause
  still holds a node this entry spans, the verb says which host and which pause and
  leaves everything alone, telling you to resume the owner first. A verb user can
  see the fleet and is naming one host; a tick iterating the overlay has neither, so
  the rule sits in the shared policy where both are honest about it.
* One pass resumes at most `MAX_RESUMES_PER_TICK` (4) nodes, longest-waiting first,
  and defers the rest **visibly**. Each resume is one sparkrun control call with a
  120 s budget run sequentially in this stream's own thread; an unbounded pass on a
  large backlog could gate the stream for an hour and make verify's per-stream
  staleness floor cry wolf on a healthy daemon. The shape is the merge queue's
  one-PR-per-project-per-tick.
* Idle is free, measured rather than asserted: no overlay file means `list_pauses`
  short-circuits on `os.path.exists` and the pass returns before `plan`,
  `resume_units` or `serving.load_serving` are reachable — `TestIdleTickIsFree`
  patches all three to raise and calls the production entry point with no
  arguments, then pins exactly one overlay read. Only the usual stream stamp is
  written, on every path including the raising one, so a deliberately idle stream
  never reads as a dead one to `hscc verify`.
