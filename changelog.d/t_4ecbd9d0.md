kind: Added
task: t_4ecbd9d0

* The pipeline watchdog now honours the node-pause overlay:
  `pipeline_watchdog()` asks `node_pauses.covering()` for the node it would
  heal (`serving.PRIMARY_NODE`) before it decides anything is broken, and a
  node the operator parked with `hscc node pause` is skipped — no restart
  attempt, no failure entry in `WATCHDOG_BLOCK_FILE`, no 3-failures-in-10min
  `PIPELINE BLOCKED` latch, no desktop notification. A pause is not a fault, so
  it can no longer page anyone or leave a strike in the breaker's memory that
  outlives the pause. The watchdog stream keeps reporting the truth
  (`ok: false`, both leg verdicts) and says why it did not act, carrying
  `paused` / `paused_node` / `paused_reason` / `paused_since` for the readers
  (`hscc status`, `hscc verify`) to render.
* The skip is span-aware: it asks `covering()`, never `paused()`, so the other
  nodes of a tensor-parallel unit — named only in the pause record's
  `span_nodes` — stay parked instead of being relaunched behind the operator's
  back.
* `restart_vllm()` gained rule 0 for the same reason. It is the single
  mutating relaunch surface and it has a caller that never comes through the
  watchdog (the trigger loop calls it directly), so the guard lives there too,
  ahead of the heal lease: a skipped repair must not even advertise itself to
  the other repair paths as "a repair is in flight". A paused node returns
  `paused: true` and no stop and no launch are ever issued.
* The skip covers exactly one node, deliberately: a pause on an unrelated node
  does not disarm the breaker, so a serving or gateway failure the pause did
  not cause still counts, still latches and still pages. A missing or malformed
  overlay keeps failing open (the heal proceeds, `node_pauses` WARNs) so a
  broken file can never freeze the fleet's ability to repair itself. 14 new
  tests pin both directions — a paused node is not healed and records nothing,
  an unpaused node is still healed exactly as before — on documentation-range
  hosts and a temp-dir overlay, with no cluster touched.
