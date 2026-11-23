kind: Changed
task: t_e98c6ad4

* **The cluster surface is now decided by one fact: is this Hermes home the main
  orchestrator's home?** `hscc-roles/main_orch.py` is the single owner of that
  answer. It resolves the main profile name through its own store, so no plugin,
  test or doc names a profile and no second resolver exists (ruling D-A).
* Two surfaces, two answers, one resolver. The **tool catalog** — `hscc-cluster`'s
  28 tools and `sparkrun-hermes`' `sparkrun_exec` — registers only where the home
  resolves to `<main>` (`cluster_tools_allowed`). The **operator slash verbs** in
  `hscc-commands` register at `<main>` *and* the operator/gateway root home
  (`operator_commands_allowed`), because they are the wedge path and a human types
  them into the gateway rather than a model calling them (ruling R3/D-B). The
  audit's looser D-B wording would have handed a model chatting at the root the
  whole catalog; the card's tightened wording is what the code follows.
* `hscc-cluster`'s six hooks still register in **every** home — worker and
  dispatcher lanes (`[budget]`, the per-profile tool ledger, kanban
  completion/blocked observers) stay alive (ruling D-C). Hermes loads hooks and
  tools as one all-or-nothing unit, so the split is made inside `register()`
  (audit §7 G-1); a source-shape test pins the tool loop inside the gate and the
  hook lanes outside it, and two more pin which wrapper each plugin may call.
  `sparkrun_exec`'s reach is finally enforced — the SOUL has claimed
  "orchestrator-only" since it shipped and the audit left the claim unenforced
  (`LEFT-2`, closed here).
* **Bootstrap now writes an explicit `plugins.enabled` into every home, tiered,
  which is what defuses the `_migrate_to_21` landmine** (ruling R5). Hermes'
  migration fills a key-less config with every manifest-bearing dir under
  `$HERMES_HOME/plugins/`, and the audit measured the key present in 0 of 52
  profile configs, so "no `plugins` key" was one migration away from handing
  every worker the whole cluster surface. `enable()` sweeps the tree the way
  `ensure_goal_judge_everywhere` does (root from the argument, never from
  ambient `HERMES_HOME` — the t_267f9d88 class), never creates a config Hermes
  owns, and prunes **only HSCC's own cluster names**, so a third-party entry an
  operator enabled survives. `--home` keeps meaning "wire this one home
  deliberately": it writes that profile's tier and nothing else.
  `HSCC_CLUSTER_PLUGINS_MAIN_ONLY=0` restores the old flat shape for an operator
  who disagrees, and a missing resolver falls back to it rather than to a
  half-applied tier.
* `hscc-roles/generator.py::generate_profile()` seeds the tier for a home it
  creates, so a profile made after the last bootstrap is not left key-less (R5's
  "seed NEW profiles the same way"). It is best-effort by design: profile
  generation cannot fail because a sibling package would not load.
* The root home's `plugins.enabled` is left exactly as §6 writes it (all three
  names). Config and registration are now separate facts: the gate is what the
  ruling controls, and pruning the root's config would delete the operator's
  verbs from the one home R3 keeps them in.

---
kind: Verified
task: t_e98c6ad4

* 53 new tests across six files, every one sandboxed to a fake `~/.hermes` tree
  in tmp plus an `HSCC_DIR` holding a NON-default stored main name — nothing
  live is read or written: `hscc-roles/tests/test_main_orch_cluster_surface.py`
  (20), `hscc-bootstrap/tests/test_enable_plugins_cluster_tier.py` (16),
  `hscc-commands/tests/test_cluster_surface_gate.py` (6),
  `hscc-cluster/tests/test_cluster_surface_gate.py` (5),
  `sparkrun-hermes/tests/test_cluster_surface_gate.py` (5), and 1 in
  `hscc-roles/tests/test_generator.py`.
* The root home is pinned on both sides of the split: verbs register there, the
  tool catalog and the fleet passthrough do not — and the split is asserted at
  the wrapper each plugin actually calls, not just at the resolver.
* D-A is pinned by tests that store a main name that is NOT the default and
  assert the resolver still answers correctly, plus source gates that no plugin
  or new bootstrap writer contains a profile literal or a `-orch` sniff.
* The writer/gate asymmetry is pinned in both directions: a named worker home can
  never get the full tier from the config writer, while a stub context with no
  usable `profile_name` is allowed even under a profile home (existing suites must
  not start depending on ambient `HERMES_HOME`).
* A sweep of an already-tiered tree writes nothing: file contents, mtimes and the
  backup set are all asserted unchanged.
