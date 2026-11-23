kind: Fixed
task: t_8e5ab6cb

* The down-repair paths in `health.py` no longer heal a parked node.
  `check_workers()`'s `auto-heal` (whole-template force-recreate) and
  `worker-relaunch` (per-unit relaunch) gates, and `check_proxy()`'s
  `proxy-relaunch` (the fleet-wide `sparkrun proxy start`), consulted nothing
  that says "the operator parked this node on purpose", so a node under
  `hscc node pause` kept having its units force-recreated and relaunched on top
  of whatever is now using that GPU. Each now asks the node-pause overlay at the
  mutating surface — `node_pauses.covering(node)`, never `paused()`, which is
  host-keyed and misses the `span_nodes` of a tensor-parallel unit — BEFORE the
  heal lease is acquired and before the strike is counted, so a parked node
  neither leases, nor strikes, nor heals, and a skip never advertises "a repair
  is in flight" to the other repair paths. Same shape as the watchdog rule 0
  (#565), the autodown wake (#566) and the heal-breaker seam (#568).
* The backstop #568 removed is restored where it belongs. Before #568 a parked
  node accrued heal strikes and the breaker refused further heals after
  `HEAL_BREAKER_MAX`; #568 correctly stopped accrual for a pause, which left
  nothing at all between a parked node and an unbounded relaunch loop. The
  refusing gate is now the pause seam itself, at the mutating surface, rather
  than an incidental side effect of the counter.
* Exactly the parked node is skipped, and the failure direction stays open. A
  pause on node A does not disarm repair for node B (over-suppression is the
  same class of bug as the false page the seam removes), and
  `node_pauses.load()`'s fail-open direction is inherited — a missing or
  malformed overlay means the heal proceeds and `node_pauses` WARNs, so a broken
  file can never freeze the fleet's ability to repair itself.
* The streams stay truthful about a skipped repair. The proxy stream gains
  `paused` plus the reason in `message` and is recorded as neither a failed
  relaunch nor a cooldown stamp (nothing was attempted, so the durable cooldown
  window is not pushed out by a pause); the workers stream gains a `paused` list
  of the units left down because their node is parked, still counted `down`.
  Both log the skip and its reason instead of pretending the tick was ok.
* `check_workers(pause_directory=None)` and `check_proxy(pause_directory=None)`
  thread `node_pauses`' own explicit overlay-root seam through to the seam and
  on into `heal_breaker.note_heal()`, so the daemon (reader) and the CLI
  (writer) agree on one root instead of each reading an env var per process.
  `None` is the default `~/.hscc`, which is what the daemon's dispatch table
  passes.
---
kind: Verified
task: t_8e5ab6cb

* 23 new tests in `hscc_daemon/tests/test_health_repair_node_pause_t_8e5ab6cb.py`
  pin both directions on all three paths: a parked node takes no lease, counts no
  strike and gets no force-recreate, no `sparkrun stop` + re-issue and no
  `proxy start`; the same node with no pause — and a node beside the parked one —
  is repaired exactly as before. The lease is asserted separately from the heal,
  so a seam that skipped only *after* acquiring the lease (still deferring real
  repairs elsewhere in the fleet) cannot pass. Three span-sibling cases assert
  `paused()` and `covering()` disagree on that node and that the behaviour
  follows `covering`, so swapping the two reddens this file — the same pin #565
  and #568 make. The overlay is asserted read-only, the workers and proxy streams
  are asserted truthful (`paused`, reason, not-a-failed-relaunch, cooldown stamp
  carried forward rather than re-stamped), and the fail-open half is pinned from
  both sides: a malformed overlay leaves the repair armed and its WARN reaches
  the log. Default-root cases (the daemon's own configuration, no
  `pause_directory`) guard-assert the root is conftest's tmp dir before writing.
  All hosts TEST-NET-1 (192.0.2.0/24); no node, sparkrun, ssh or real `~/.hscc`
  touched; the file is green run alone as well as in-leg.
* Mutation-probed on a copy of the worktree, one mutant at a time, pristine
  restored and verified byte-for-byte afterwards: 10/10 load-bearing mutants
  caught. Deleting the auto-heal seam (7 red), the relaunch seam (3), or the
  proxy seam (1); `covering()` → `paused()` reddening exactly the three
  span-sibling pins; "any pause disarms every node" over-suppression reddening
  the pause-elsewhere pins (5); the seam moved below the lease (7); the workers
  stream no longer naming the skip (1); the proxy `paused` key hardcoded (1); a
  pause tick re-stamping the durable cooldown (1); fail-open inverted so a
  corrupt overlay freezes the repair (6). One probe is reported as
  expected-survived rather than hidden: dropping the threaded root from
  `heal_breaker.note_heal()` inside `_note_heal_action` stays green, because
  #568's accrual guard behind it is already suppressed for a parked node — that
  threading is defence-in-depth for a future caller, not the load-bearing gate.
