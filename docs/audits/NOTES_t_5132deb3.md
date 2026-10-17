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

(measurements appended below as they land)

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
