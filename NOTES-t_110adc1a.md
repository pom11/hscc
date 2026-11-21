# t_110adc1a — daemon tick auto-resumes expired pauses

Status file for the reviewer and for whoever picks this up. Design decisions
first, because three of them contradict the card's prose or the overlay's
docstring and are deliberate.

## Dependency guards (checked at start, origin/main `3fd41f74`)

- `hscc_daemon/node_pause_exec.py` — PRESENT (#560/#567 lineage).
- node CLI verb `_handle_node` → `_node_pause` / `_node_resume` / `_node_paused`
  in `hscc_daemon/hscc.py` — PRESENT.
No block needed.

## Decisions

### 1. The tick lives with the feature, not in `daemon_ops.py`
The card says "daemon tick (hscc_daemon/daemon_ops.py periodic pass)". Every
other stream in this loop puts its cadence + CHECKS row in `daemon_ops.py` and
its *body* in the feature module (`kanban_pause.enforce_tick`,
`merge_queue.check_merge_queue`, `orch_waker.waker_tick`). `daemon_ops.py` gets
`PERIODIC_INTERVALS["node_pause_expiry"]` + the CHECKS row; the body
(`auto_resume_expired` / `expiry_tick` / `check_node_pause_expiry`) goes in
`node_pause_exec.py`, which already owns resume policy and already imports
`serving`/`util`. No new stream is invented: `verify._live_stream_intervals()`
reads `PERIODIC_INTERVALS` dynamically, so the per-staleness rule picks the new
stream up with no roster edit.

Cadence 60 s — the `kanban_enforce` class: when nothing is paused the tick
costs zero subprocesses (one `os.path.exists` on the overlay), and a pause
whose `until` passed is resumed inside a minute.

### 2. Reuse is a shared function, not stdout capture
The parent's handoff says "reuse the CLI, don't copy it", and the plugin tools
do that by calling `_node_resume` in-process under `contextlib.redirect_stdout`
and reporting the bytes. That is safe for a single-shot agent turn; it is NOT
safe in the daemon, which runs ~15 supervised stream threads — `redirect_stdout`
swaps a *global*, so for the window of a resume it would steal prints from every
other thread and route them into my capture buffer.

So the shared seam is a function, not a print-capture: `node_pause_exec.resume_host()`
holds the resume policy (rebuild argv from `plan()`, filter to the recorded unit
ids, `resume_units(apply=...)`, then clear the pause + heal strikes) and emits its
lines through an `echo` callable. The CLI passes `echo=print` and keeps its exact
bytes and exit codes; the tick passes a logger. One policy, two callers — the
"smallest executor-side addition" the handoff authorised.

### 3. `expiry_pass()` is deliberately NOT called by the tick
`node_pauses`' own docstring says the tick "resumes the units first, then
`expiry_pass`/`clear`". `expiry_pass` drops *every* expired entry, including one
whose restart just failed — i.e. it clears the record because the tick ran, which
is the one failure mode the parent's handoff (#2) and the executor's whole
refuse-and-keep design exist to prevent. The tick clears per host, only after a
restart that succeeded, exactly as `hscc node resume --apply` does. A failed
restart therefore stays on disk and is retried next tick.

### 4. One overlay read per pass, and a span guard
`list_pauses(directory)` once; expiry and the span decision are both answered
from that snapshot (`load()` normalises into fresh objects, so two reads cannot
be identity-compared). Non-keyed span siblings can never appear as candidates
(they have no entry), which is how the tick gets the CLI's "resume the owner,
not the sibling" rule for free. The guard the CLI cannot express: an expired
entry whose `span_nodes` intersect a *different, still-active* pause is SKIPPED
and kept — restarting it would reissue a multi-node unit across a node the
operator still holds.

### 5. Hermetic posture
RFC 5737 hosts only, fake sparkrun on PATH (the shared fence is autouse),
`directory=` on every overlay read/write, `state.write_state(directory=)` for the
stream stamp, `serving.load_serving` / `node_pause_exec.fetch_cluster_status` /
`util.run_cmd` patched. The daemon leg's conftest seals `node_pauses.HSCC_DIR` +
`heal_breaker.HEAL_BREAKER_FILE`; the new suite re-asserts that seal because a
tick test that forgot it would park a real node and the live daemon would start
skipping it.

## Slice plan (commit each when green)

1. `resume_host()` extraction + CLI delegation — `test_node_cli.py` green
   unmodified (proves byte-for-byte CLI parity).
2. `auto_resume_expired` + `expiry_tick` + `check_node_pause_expiry` +
   stream wiring + `tests/test_node_pause_tick.py`.
3. changelog fragment + `docs/ops/node-pause.md` (it currently states "nothing
   calls them yet / treat --until as a note, not a timer" — this card makes that
   false) + full suite + PR.
