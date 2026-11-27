kind: Fixed
task: t_cf6bf476

- **A failed launch step silently kept the OLD template as `applied`, so
  `template status` lied (t_cf6bf476).** `apply_template` wrote
  `~/.hscc/applied_template.json` only when EVERY step came back OK — so an
  apply where serving/models/config/routing/caps/reasoning all wrote cleanly
  but ONE launch step reported failed recorded NOTHING, and the status read
  path kept reporting the PREVIOUS template after a successful switch (the
  operator had to write the file by hand). One failed sub-step vetoed the
  whole record: the file's meaning — "what is applied to the nodes" — was
  coupled to launch CONFIRMATION, not to what was actually written.
  Now the record tracks the writes: a non-OK **launch-side** step
  (`provision`, `gateway-restart`) still fails the apply, but the new
  template is recorded with a `launch_unconfirmed: true` flag naming the
  unconfirmed steps, and the apply report carries the same flag. A non-OK
  **config-side** step keeps the full veto — it means one of hscc's own write
  claims is false, and a rolled-back apply records nothing either way. A
  clean apply's record gains NO new keys — full-green records stay
  byte-identical to before (§7 no-phantom-key rule). Rejected alternative —
  re-checking unit health before deciding to record: it re-couples the record
  to live cluster state, can time out or race a slow vLLM start, and a
  health-check false-negative would reproduce the exact stale-record bug this
  fixes, while the honest answer to "what did apply write?" is already in
  hand at that point in the flow.
