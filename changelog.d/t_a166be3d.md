kind: Fixed
task: t_a166be3d

- **`hscc api --help` and `hscc autodown --help` overflowed the help-surface
  gate at the mandated widths** (audit Class B, owner F4,
  docs/audits/CLI_HELP_2026-10-15.md). Both group HELP_TEXTs packed usage and
  description onto ONE hard line separated by a padding gutter, so the block
  was 83-116 columns wide: `hscc api --help` measured 5 overflow lines at
  width 80 plus 2 at width 120 (the `install-service` / `uninstall-service`
  entries are 116 cols, so they failed at BOTH widths), and `hscc autodown
  --help` measured 16. Both groups now use the two-line layout the template
  and per-verb surfaces already use — usage alone on its line, description on
  indented continuation lines — with every line authored within the 78-column
  budget. `autodown_cli` emits its help with plain `print` (the authored width
  IS the rendered width) and `api_cli` prints through Rich, whose non-tty
  console is fixed at width 200, so re-flowing the authored text is the only
  fix that holds at every width; neither COLUMNS nor Rich can rescue a hard
  line here. Wording is the base's verbatim — re-flowed, never reworded.
- **`hscc api uninstall-service` embedded an absolute path in a hard
  sentence** (audit Class B, owner F4). The removal notice printed
  `no unit file at <absolute plist path> (nothing to remove)`, so with a long
  HOME root — the audit probes under a sandbox HOME — the line ran to 87+
  columns and overflowed at both mandated widths; Rich cannot wrap it either,
  because a filesystem path has no break opportunity. The path now gets its
  own indented line, rendered `~`-relative when it is under HOME (it always
  is, by construction of `api_launchd.API_PLIST_PATH`), and verbatim if
  anything ever rebinds that constant — so the notice is width-independent and
  still names exactly the file it acted on. This also shrinks the (D) surface:
  the path no longer leaks the sandbox/absolute HOME root into help-shaped
  output.
- **`hscc kanban blocked --help` / `hscc kanban stale --help` ran the verb**
  (audit BROKEN B10, Class D — folded into this card with operator approval,
  recorded on t_7b0f65c8 comment 1157). `cmd_kanban` honoured `--help` only as
  the GROUP's first token, so a per-subcommand help request fell through into
  the listing branch and enumerated every board: the autodown C3 interlock read
  logged WARN lines and wrote `~/.hscc/daemon.log` under the caller's HOME
  while "printing help" — and the mutating sibling of that same branch
  (`blocked --recover <id>`, `stale --archive <id>`) was one token away. Both
  kanban dispatchers now carry a per-subcommand help table and intercept
  `--help`/`-h` ANYWHERE in the raw argv — before `--json`/`--theme` stripping,
  before dispatch, exactly as `cmd_api` and `cmd_autodown` already do — so
  `blocked --recover --help` prints help instead of reaching the recovery
  seam. `stale` help is delegated to `kanban_cli` (its owner) rather than
  duplicated here, so the wording has one source; if that module is absent the
  delegation says so instead of advertising a verb the build lacks. Non-help
  behaviour unchanged, `--recover`/`--archive` still dispatch normally.
---
kind: Verified
order: 1

- Tests (red-first on the base tree): `hscc_daemon/tests/test_cli_help_f4_surfaces.py`,
  47 cases — two-line layout + 78-column budget on both re-flowed group surfaces,
  canonical-whitespace no-reword guards pinning every base description, the
  uninstall path line driven through an absurdly long HOME (plus the
  path-outside-HOME case, so ~-relativising is pinned as display, never as a
  lie about which file was removed), kanban landmine fixtures making ANY board
  read or recovery seam FATAL during a help path, argv-shape cases
  (`blocked --recover --help`, `blocked --theme dark -h`, `stale --json -h`,
  `stale --archive --help`) that fail if the intercept ever reads stripped argv,
  the positive pins that `--recover` still dispatches and that both kanban entry
  points render byte-identical `stale` help. Real-CLI cases re-run the exact
  audit invocations at COLUMNS 80 and 120 under a throwaway HOME + recording
  `sparkrun`/`hermes`/`gh`/`git` canaries: rc 0, zero new HOME files (above all
  no `.hscc/daemon.log`), zero canary trips, no daemon-log-shaped line, no ANSI,
  no line over budget. `blocked --recover` is never executed — it is mutating;
  the recovery path is pinned through a monkeypatched seam only.
- Gate: the 3 `owner=F4` rules and the 3 `owner=BROKEN-KANBAN` rules this card
  clears are pruned by hand from `scripts/tests/cli_help_expected.json`
  (39 → 33 rules; the rules are the contract, every hunk reviewed, never
  regenerated — no other owner's rule touched). Post-fix audit: 89 → 63
  findings, zero UNEXPECTED violations, zero rule over-counts, and
  `scripts/tests/test_cli_help_surface.py` green.
- Safety: no real sparkrun/fleet touched — sandbox HOME + exit-97 recording
  canaries, and the kanban help paths run against landmines rather than a
  board. Operator-owned monitor surfaces and every other card's rules are
  byte-untouched.
