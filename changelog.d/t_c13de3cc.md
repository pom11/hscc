kind: Added
task: t_c13de3cc

* `hscc_daemon/tests/test_surface_coverage_gates_t_c13de3cc.py` (new, 6 tests
  green) — the two LEFT-4 coverage gates from the agent-surface audit
  (`docs/audits/2026-11-23-agent-surface.md` §11). Gate 1 walks dispatch in
  both shapes the CLI has: top-level verbs parsed from `main()`'s own
  `cmd`/`cmd_lower` branches and the `DAEMON_COMMANDS`/`advanced_cmds`
  membership tables it dispatches through, then every group leaf from that
  group's own registry (`CLUSTER_SUBCOMMANDS`, `_NODE_SUBCOMMAND_HELP`,
  `VALID_SUBCOMMANDS`, `SUBCOMMAND_HELP` keys) plus the branch-parsed `cards`
  and `orch` routers and the four `main wakeups` leaves. Every dispatched verb
  must be named in `COMMAND_HELP`; exemptions are explicit and named
  (`unit-emit` with its reason; F3's kanban verbs are now advertised, so the
  exemption table is empty). Gate 2 parses `BUNDLED_SKILLS`
  and checks `install/hscc-skills/` in both directions: every listed name is a
  real directory with a `SKILL.md` beneath it, and every directory is listed
  (duplicates and path-shaped names fail too). No live state, fleet, launchd
  or network access.

---
kind: Fixed
task: t_c13de3cc

* The gates found three real `COMMAND_HELP` gaps on main and the list fixes are
  in this change: `hscc help cluster` now names the dispatched `history` leaf,
  and `hscc help api` now names `install-service` and `uninstall-service`
  (re-flowed onto its own line inside the authored 76-column budget). Both
  groups dispatched the verbs while `hscc help <group>` hid them — the audit's
  F3 class, one group over.
* `BUNDLED_SKILLS` now lists `hscc-lessons`, so the vendored tree and the
  installer's list are the same 13 names and the F12 orphan is installed and
  updated instead of being left on disk unowned. `install/README.md` and
  `hscc-skills/README.md` now state vendored = installed and point at the gate.
  Sandbox smoke (`HERMES_HOME` in a temp dir): `install-skills` wrote 13 skill
  dirs (43 files) including `hscc-lessons`, and `status` reported all 13 OK.
