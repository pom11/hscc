kind: Added
task: t_23fad1e4

- **`hscc_daemon/node_pause_exec.py` — the executor seam of the node pause
  overlay.** `plan(host)` answers "what lives on this node" from sparkrun's
  structured status query and reports a multi-node unit with **every** node it
  spans: the node set is the union of the observed placement
  (`groups[cid].containers[].host`) and the intent-recorded `meta.hosts`, so a
  tensor-parallel pair whose sibling node did not answer the sweep is still
  reported as a span and the unverified sibling is named — the reading that a
  pause must not get wrong, because stopping a span unit stops it on every node
  it covers. `plan()` also flags whether the host serves the orchestrator model
  from the served-model config (`serving.orchestrator_nodes`), corroborated by
  the live served-model alias, and fails closed to flagged when the status read
  or the serving config is unavailable — the CLI's refusal without `--force` is
  the cheap direction to err in. `stop_units` / `resume_units` are dry-run by
  default and mutate only with `apply=True` (the rule from #478); the resume
  argv is `serving._unit_run_cmd`'s — the keepalive relaunch command,
  `--ensure` included — taken from the unit recorded at pause time, so a resume
  re-issues the declared workload instead of deriving a different intent id and
  launching a parallel duplicate (t_e1ff0e8e). A workload with no unique
  serving.json match is refused with a printed reason rather than restarted
  from a guessed command. Transport stays sparkrun-only
  (`health._SPARKRUN_STATUS_SCRIPT` under sparkrun's own venv python, the
  `recover.fetch_identity_entries` precedent), and a failed read yields an
  error plus no units — never an empty list readable as an idle node, the
  v1.15.0 local-`docker ps` failure class. 49 tests, all against 192.0.2.x
  placeholders and planted fakes.
