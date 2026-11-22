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
  `None` is the default `~/.hscc`.
