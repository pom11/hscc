kind: Changed
task: t_fc8f7a15

- **Plugin README set refreshed to the audit §2.2 template (R4,
  docs-only).** All five share the short shape — what it is / when you touch
  it / how to test / links — and no changelog-style history.
  `hscc-cli/README.md`: KEEP+polish (template conformance; no stale claim).
  `hscc-cluster/README.md`: the bare operator-node id `.247` is gone (the
  moved doc uses the sanctioned `10.0.0.x` placeholder); the
  Delegation-routing / probe-before-write essay moved out of the plugin front
  page to `docs/DESIGN-probe-before-write.md` (cited from the README),
  where its claims were re-verified against `cluster_template.py` and its
  `test_apply.py` pins. `hscc_daemon/README.md`: KEEP+REFRESH — the core
  module table now names the HEAL_CASCADE trio (`heal_lease.py`,
  `heal_breaker.py`, `launch_guard.py`), `recover.py`,
  `kanban_blocked.py`/`kanban_cli.py`, `escalate*.py`, `autodown*.py`,
  `kill_switch.py`, `qr_code.py` (and says "core modules", not completeness);
  the safety section covers lease/breaker/guard, the per-unit F3 remedy
  (whole-template `--force-recreate` is gone), durable load grace and capped
  engine-wedge recovery; `cluster_live.py` is quoted only, never edited.
  `hscc-commands/README.md`: template conformance. `hscc-roles/README.md`:
  roster now lists `ios-engineer`, `ecofire-bc-engineer` and `worker`
  (census `ls roles/` = 25), CLI block adds `provision-check`, and the
  `~/.flightdeck/registry.yaml` paths stay as technical necessity reworded
  into the post-F1 "hscc-project home" convention (F1 is on main).
