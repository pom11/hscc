kind: Fixed
task: t_aeb982bc

- **`hscc heal-clear --help` EXECUTED the mutating verb** (audit BROKEN B4,
  docs/audits/CLI_HELP_2026-10-15.md Class D, safety-class). Measured in a
  sandbox HOME: the `--help` token was consumed as the NODE NAME and
  `cmd_heal` ran the clear path — output echo `No strikes recorded for
  --help` — and the dangerous sibling `heal-clear <node> --help` CLEARED the
  node's live strikes (rewrote `~/.hscc/heal-breaker.json`) while the
  operator believed they were reading help. `hscc.py main()` now intercepts
  `--help`/`-h` anywhere in the RAW tail of `heal-clear`/`heal-status` —
  before the dispatch that builds the node kwargs — prints the COMMAND_HELP
  wording and exits 0 executing nothing (the raw-argv shape of the
  cluster/kanban/api intercepts). `cmd_heal` keeps a second guard for direct
  library callers: a node name of `--help`/`-h` prints usage and touches no
  state (the B6 engine-side `cmd_stop` shape).
- **`hscc help heal-clear` / `hscc help heal-status` exited 1** (audit B5):
  both verbs the breaker alert tells operators to run had no COMMAND_HELP
  entries (`Error: no help for …`). Added, so both exit 0. Multi-line values
  print as authored (only the first line takes `_handle_help`'s 2-space
  prefix), so every line stays <= 76 cols for the (B) gate's 78 budget, and
  the wording deliberately never quotes the (D) EXEC_ECHO strings (`Cleared
  heal strikes` / `healing resumes`) — help that echoed the execution output
  would itself trip the sensor.
---
kind: Verified
order: 1

- Tests (red-first on the base tree — 25 of 30 new cases fail before the
  fix, all 33 green after, incl. the 3 controls that pass on both trees):
  dispatch landmines (`cmd_heal` + the breaker's state reader/writer RAISE,
  so a `--help` reaching dispatch fails on the spot), the argv-shape case
  `heal-clear <node> --help`, positive-dispatch pins (kwargs byte-pinned —
  an over-eager always-help fix cannot pass them), the second-line-of-defence
  guard via a tmp-redirected ledger, COMMAND_HELP existence + 76-col line
  budget + EXEC_ECHO-wording absence, and real-CLI probes of the exact audit
  invocations (`heal-clear --help`/`-h`, `heal-clear <node> --help`,
  `heal-status --help`, `help heal-clear`, `help heal-status`) at COLUMNS 80
  AND 120 under a throwaway HOME + recording `sparkrun`/`hermes`/`gh`/`git`
  PATH canaries, against a PRE-SEEDED LIVE `heal-breaker.json` (in-window
  strikes + alert/cleared stamps): file bytes AND mtime must not move, zero
  canary trips, zero new files under HOME, rc 0, no ANSI, no breaker-panel
  state rendered. A positive control (`heal-clear <node>` WITHOUT a help
  token, same sandbox) proves the tripwire has teeth — that same file does
  move when the verb really runs.
- Gate: pruned the 3 now-satisfied owner=BROKEN-HEAL rules from
  `scripts/tests/cli_help_expected.json` (27 -> 24) with a scripted
  JSON-level check (only those 3 rules changed; every other owner's count
  — F5 x20, F3 x2, MONITOR x2 — verified untouched). Surface suite green
  (12 passed / 1 expected sibling-interpreter skip). Real-CLI audit
  invocations re-run in the sandbox: B4/B5 signatures gone, the mutating
  verb unchanged.
