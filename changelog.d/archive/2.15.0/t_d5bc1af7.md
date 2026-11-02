kind: Fixed
task: t_d5bc1af7

- **`flash-next-all` no longer declares per-profile cap 2 — one
  `hscc template apply --confirm` can no longer stomp the operator's live 3
  back to 2 (audit E GAP-E-3).** The template shipped declaring
  `max_in_progress_per_profile: 2` (77be50d1, 2026-10-29) while the operator's
  live `~/.hermes/config.yaml` kanban caps had since moved to 12/3. Because
  `_apply_caps_declaration` (cluster_template.py:4009) writes the DECLARED
  numbers verbatim, applying the template would have silently rewritten the
  live config 3 -> 2 — the global 12 already matched by luck, not by design.
  The template's caps block now states the SOT pair 12/3 (with the reasoning
  comment updated to match), and
  `hscc-cluster/tests/test_template_declarations.py::TestShippedTemplateCapsMatchSOT`
  pins it two ways: the declared caps must equal the pinned SOT pair, and
  applying the shipped template against a config seeded with live 12/3 must
  change nothing (proven red-then-green — both tests fail on the old 12/2).
  The SOT pair in the test is explicit, not `CAPS_MIGRATION_DEFAULTS` (30/10,
  the bootstrap fill-if-absent default — a different thing with its own pin).
