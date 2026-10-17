# t_5132deb3 — [cli-help follow-up] cluster up/down --help, escalate/autoscale -h, CLI-wide -h gap

Base: origin/main @ baf98413 (contains the whole cli-help lane). Branch
wt/t_5132deb3, push-only. Scratch workspace.

## Scope (operator-verbatim from PR #33 final pass + F5's recorded gap)

(a) `cluster up --help` / `cluster down --help` print "unknown argument"
    (rc=1) instead of help. inert, just not helpful.
(b) `escalate -h` and `autoscale -h` run the read-only command instead of
    printing help.
(c) CLI-wide `-h` blind spot (F5 NOTES_t_fc307a51): the audit oracle probes
    `--help` only; worst case `start-daemon -h` LAUNCHES the loop.

## Operator ruling recorded on the card

For (a): help now WINS over the F6 refusal for a help token on up/down.
The two owner=F3 D-rc rules in scripts/tests/cli_help_expected.json
(F6-refusal rc=1 pins) are pruned/replaced by this card. The refusal stays
byte-identical for up/down WITHOUT a help token (unknown token like --foo).
MONITOR×2 rules stay untouched (operator-owned, off-limits).

## Red-first measurements (base baf98413)

Full -h/--help matrix probed under sandbox HOME + exit-97 sparkrun/hermes/
gh/git PATH canaries + recording landmines on every verb handler (a TRIP ==
the help token reached dispatch; mutating verbs raise instead of running).
Matrix JSON: scratch base_h_probe2.json. Findings:

(a) `cluster up --help` / `cluster down --help` -> rc=1 F6 refusal text
    ("Error: unknown argument for 'hscc cluster up': --help") — the two
    owner=F3 D-rc rules. `up -h`/`down -h` identical. `up --foo --help`
    refuses on --foo (rc=1) — refusal-without-help-token path to preserve.

(b) DAEMON_COMMANDS `-h` executes the verb (rc as if run, no help):
    escalate -h (scan ran: "no escalations pending"), autoscale -h (panel),
    throughput -h, stats -h, status -h (wrote 1 HOME file), verify -h (ran,
    rc 1, wrote 1 HOME file), check -h (1 canary TRIP — shelled out),
    watch -h (HUNG past 60s — live tail), notify msg -h (SENT a notification;
    `notify msg --help` too — dispatch joined argv), plist -h, log -h.
    MUTATING/launch verbs landmine-TRIPPED on -h: start, stop, install,
    uninstall, and start-daemon -h -> cmd_start_daemon TRIPPED (launches the
    loop — the F5 worst case, confirmed).
    `--help` is only honoured as args[1]: `escalate --json --help`-shaped
    tails fall through (B10 class one level deeper). profiles is broken for
    BOTH flags: `profiles --help` AND `profiles -h` load the engine and run
    cmd_profile_status. `advanced -h` -> "Unknown command: advanced" rc=1.

(c) -h already inert (audit-safe probe set): cluster verbs (status/hosts/
    jobs/info/stop, monitor via _MONITOR_HELP), template group/verbs, api
    group/verbs, autodown group/verbs, kanban group/verbs, project (argparse),
    heal-clear/heal-status (B4), top-level hscc -h.

## Design decisions (measured basis above)

- (a): `up`/`down` join _CLUSTER_SUBCOMMAND_HELP + _CLUSTER_HELP_VERBS, so
  the existing raw-argv intercept (before --theme strip, before engine load)
  prints verb usage rc=0 for ANY help token anywhere in the tail; the F6
  refusal code path (_cluster_refusal_message / _parse_cluster_flags) is
  NOT touched and still refuses rc=1 byte-identically for every non-help
  unknown token (--foo, dangling --theme). Parser-level pins in
  test_cli_arg_safety.py stay (defence in depth: the parser still refuses
  --help if a caller bypasses main()); the dispatch-level pins flip to rc 0.
- (b/c): the DAEMON_COMMANDS + advanced_cmds help branches in main() become
  the chain shape (same as heal/api/autodown/kanban/cluster): ANY "-h"/
  "--help" token in the RAW tail intercepts before dispatch; -h and --help
  render the SAME COMMAND_HELP text (no-reword pin compares bytes of both
  renders). Command lookup lowercases first (cmd_lower) so `Start -h` cannot
  bypass the help branch while dispatch still lowercases (measured hazard
  class, one line, closes it). profiles gets the same raw-tail intercept
  inside _handle_profiles before the engine load.
- (c) audit: probe -h alongside --help for every flag/flag2 surface EXCEPT
  the launch-class set {start, stop, install, uninstall, start-daemon,
  watch} (a regressed intercept would fork a daemon, run launchctl, or hang
  3x60s — those verbs are pinned IN-PROCESS by landmine-raise instead, the
  card's "start-daemon NEVER launched for real" rule). Skips are recorded
  and shape-pinned in the gate (like the api-start skip pin). MIN_PROBES
  raised to the new exact count in the same commit.
- New pin file: hscc_daemon/tests/test_cli_h_shortflag.py — landmine battery
  over main() dispatch for every verb (both flags), start-daemon double
  landmine (cli.cmd_start_daemon AND daemon_ops.run_daemon_loop),
  no-reword byte-parity pins, positive-dispatch controls (without the help
  token every verb still reaches its handler — intercept-off teeth).

## Design decisions

- up/down help route through the _CLUSTER_SUBCOMMAND_HELP shape (lane commit
  cb352d40 pattern): entries "up"/"down" added, _CLUSTER_HELP_VERBS extended.
  The F6 raw-argv refusal keeps covering every NON-help unknown token
  (unchanged wording; pinned by the canonical-whitespace test).
- escalate/autoscale: intercept -h (and --help if measured broken) BEFORE
  dispatch, per-subcommand help from COMMAND_HELP, rc 0. Same shape as the
  heal-verb intercept (BROKEN-HEAL) — but the DAEMON_COMMANDS `--help` branch
  already runs before dispatch, so the measured gap is `-h` only (verify).
- audit oracle: probe `-h` alongside `--help` where argparse/our dispatch
  promotes it; new finding kind D-h for "-h executes instead of printing
  help"; enumerate the -h gap; MIN_PROBES raised honestly in the same commit.
- start-daemon -h NEVER launched for real: landmine-raise run_daemon_loop /
  cmd_start_daemon in unit tests; real-CLI probes sandbox HOME + exit-97
  canaries only.

## Status log

- [ ] red-first measurements
- [ ] (a) fix + rules prune
- [ ] (b) fix
- [ ] (c) audit -h probes + gap enumeration + fixes
- [ ] mutation battery
- [ ] canonical suite green (frozen tree, both interpreters)
- [ ] address_guard 0, changelog fragment, no-attribution, push
