kind: Fixed
task: t_a95a67df

- **The CLI help-surface audit now walks the verbs it used to miss.**
  `scripts/hscc_cli_help_audit.py` enumerated the `kanban` group from two
  literals (`("blocked", "stale")` and `{"blocked","stale"}`), so when the CLI
  grew `monitor`, `pause`, `resume`, `paused` and `enforce` the audit kept
  passing while never probing them — and `orch` / `cards` were
  never reached at all, so the gate's "a `--help` must not execute" guarantee
  was unenforced for exactly the newest, most mutating verbs (audit A GAP-A-2,
  GAP-A-3). Both groups' verb sets are now DERIVED from the registries that own
  them (`kanban_cli.VALID_SUBCOMMANDS` + `kanban_blocked.SUBCOMMAND_HELP`,
  `hscc._ORCH_GROUP_ENTRIES`, and `cards`' own dispatcher refusal line), the
  group routers and every verb are probed at all three widths, and the (C)
  example oracle knows the same sets. A new (COV) class then re-checks, against
  each live dispatcher, that everything it advertises is actually walked: a
  registry that rots fails the gate instead of shrinking the walk.
  `hscc cards` help also lost its 99-column overflow line so the newly-probed
  surface is clean rather than newly-expected.
