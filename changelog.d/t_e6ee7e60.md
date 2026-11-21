kind: Fixed
task: t_e6ee7e60

* The heal breaker no longer treats a parked node as an outage. `hscc node
  pause` parks one node on purpose and edits nothing the cluster is built from,
  so the node's units are down with nothing anywhere the repair paths look
  saying "deliberate" — and `heal_breaker.note_heal()` counted every such tick
  as a repair action: after `HEAL_BREAKER_MAX` of them inside the window it
  latched, emitted `heal_breaker.tripped` at `critical` and pushed a desktop
  notification about the operator's own command, while the parked node picked up
  a slate of strikes that outlived the pause and kept it unhealable after the
  resume. `note_heal()` — the strike/trip decision seam — now asks the
  node-pause overlay first: while the overlay holds the node no strike is
  appended, the ledger is not opened for writing at all, and `tripped` is False,
  so there is no latch, no event and no notification. The result carries
  `paused` / `paused_reason` so the caller logs why the tick was free.
* The skip is span-aware: it asks `covering()`, never `paused()`, so the other
  nodes of a tensor-parallel unit — named only in the pause record's
  `span_nodes` — stop accruing strikes too, instead of being struck for a fault
  the pause already explains. Same pin the watchdog seam (t_4ecbd9d0) makes.
* Exactly one node is suppressed, and only its *accrual*: `suspended()` still
  reports the breaker's own timeout, so a node already in timeout when it was
  parked stays in timeout and a repair path that never consults the overlay
  never gets a green light on a parked node. A pause cannot trip the breaker and
  cannot lengthen a suspension (suppressed ticks write nothing, so the oldest
  strike the expiry is computed from never moves), strikes earned before the
  pause still count after the resume, and a pause on an unrelated node leaves
  the breaker armed exactly as before. A missing or malformed overlay keeps
  failing open (strikes accrue, the breaker can still trip, `node_pauses`
  WARNs), so a broken file can never freeze the fleet's ability to repair
  itself.
---
kind: Verified
task: t_e6ee7e60

* 19 new tests in `hscc_daemon/tests/test_heal_breaker_node_pause_t_e6ee7e60.py`
  pin both directions — a parked node accrues nothing, never trips and never
  pages; an unparked node still accrues, trips and pages exactly as before —
  including the span-sibling case where `paused()` and `covering()` disagree and
  the behaviour must follow `covering()`, the real-caller path through
  `check_workers()`, and the fail-open half on a malformed overlay. All
  documentation-range hosts (192.0.2.0/24) and a per-test temp overlay root
  passed through `node_pauses`' own `directory=` seam; the two tests that use
  the default root guard-assert that it is the conftest-sealed tmp root before
  writing to it. No node, sparkrun, ssh or real `~/.hscc` touched.
* Mutation-probed on a throwaway worktree: deleting the pause check reddens the
  accrual tests, and swapping `covering()` for `paused()` reddens the
  span-sibling pin — so the seam is not decoration.
