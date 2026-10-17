# t_fc307a51 — [cli-help F5] top-level help surface (LAST)

Base: origin/cli-help-2026-10-15 @ 8906e151 (lane tip at claim time; reported per
standing rule). Branch wt/t_fc307a51, push-only. NOT rebased after push.

## Claim-time rule inventory (scripts/tests/cli_help_expected.json)

27 rules total: F5x20, BROKEN-HEALx3 (parent t_aeb982bc pruned its own on
origin/wt/t_aeb982bc @ b97b4b48 — NOT in the lane at my claim; claim-time state
wins: I leave all 3 intact), F3x2 (deliberately-kept F6 D-rc — not mine),
MONITOR-t_1cc33407 x2 (operator-owned, off-limits). Matches the orchestrator
claim-time note exactly (card body's "22" is stale; 20 is correct).

## F5 rule map -> planned fix

| # | kind | command | needle | n | fix |
|---|------|---------|--------|---|-----|
| 0 | A | hscc --help | flightdeck | 1 | drop "(flightdeck)" blurb (hscc.py:328) |
| 1 | A | hscc help project | flightdeck | 2 | drop "(flightdeck)" + reword "relocated flightdeck CLI" (COMMAND_HELP["project"]) |
| 2,3 | B | hscc --help | width=120 / width=80 | 2/10 | pre-wrap _get_help_text section lines (hanging indent, <=78 authored) |
| 4,5 | B | hscc advanced --help | width=120 / width=80 | 1/1 | unknown-command "Available:" 220-char line disappears once rc=0 path exists |
| 8,9 | B | hscc help advanced | width=120 / width=80 | 2/3 | ed-* rows deleted -> single 73-col line, no wrap |
| 10,11 | B | hscc help kanban | width=120 / width=80 | 1/2 | rewrap COMMAND_HELP["kanban"] to <=76-col lines (F3 cluster pattern) |
| 12,13 | B | hscc help project | width=120 / width=80 | 1/1 | rewrap COMMAND_HELP["project"] value (multi-line, own indents) |
| 15 | D-rc | hscc advanced --help | exited 1 | 1 | `advanced` gets a help path (not in DAEMON_COMMANDS/advanced_cmds today) |
| 20-26 | DEAD | hscc ed-* --help, hscc help ed-*, hscc help advanced | not yet available | 1/1/1/5/1/1/1 | DELETE the ed-* placeholder trio: help rows + COMMAND_HELP entries + dispatch (operator fan-out ruling: DELETE, not hide) |

## Consequences of the DELETE ruling (must be handled, not papered over)

The audit enumerates the ed-* trio by name (scripts/hscc_cli_help_audit.py:142-143
surface + :642-643 example oracle). With dispatch gone those probes become
"Unknown command" rc=1 -> 6 NEW D-rc findings with no rule. So the audit surface
must be amended deliberately in the same card (that is what the gate docstring
prescribes: "a removed command must not silently vanish from the audit" — the
floors MIN_SURFACE_PATHS/MIN_PROBES exist to make the amendment loud, not to
forbid it). Floor re-check required after removing 3 paths / 6 probes.

## Findings doc (docs/audits/CLI_HELP_2026-10-15.md)

Owner F5 rows: A5 (:326->:328 blurb), A6 (:386->:396 reword), the `hscc help
advanced` (B) row, `hscc advanced --help` (B) row, the D-rc `advanced` rc=1
paragraph, the DEAD `ed-*` row (delete). Precedent (F1/B6/F4/BROKEN-HEAL):
append a FIXED note; do not rewrite the historical row.

## Gate plan

1. red-first tests: hscc_daemon/tests/test_toplevel_cli_help.py
2. code fix in hscc_daemon/hscc.py (+ audit surface amendment + a_counts re-cut)
3. sibling tests updated: test_unified_cli.py (ed-* assertions),
   test_cli_rich_handlers.py (advanced render pins)
4. prune exactly my 20 F5 rules (scripted JSON edit, pattern-unique + dict identity)
5. gates: scripts surface gate, canonical suite (both interpreters),
   address_guard 0, changelog.d fragment, no-attribution grep, push-only

## Status log

- [x] base ground-truth audit (base_findings.json) inspected — 54 findings,
  42 top-level (39 F5 + parent's 3 BROKEN-HEAL), matches the rules file 1:1
- [x] red tests committed (dd2e85af notes; test file lands with the fix)
- [x] fix committed (2514773f)
- [x] rules pruned + gate green (d835bc25; 27 -> 7, 0 NEW / 0 STALE / 0 OVER;
  a_counts re-cut; gate 12 passed / 1 sibling-interpreter skip)
- [x] red-first verification of the FINAL test file at base 8906e151
  (scratch worktree; 44 of 60 red, 16 structural passes)
- [x] mutation battery 9/9 CAUGHT unfiltered (M1 no-split, M2 blurb-back,
  M3 A6-back, M4 advanced-gone, M5 ed-COMMAND_HELP, M6 ed-dispatch,
  M7 stale audit surface, M8 footer spill, M9 kanban overflow); final
  restore verified diff-clean (git diff of both files empty)
- [x] canonical suite ALL GREEN on the frozen tip ac25c0fc: py3.11 leg
  (FROZEN_TIP==TIP_AFTER verified) and py3.13 leg, both exit 0
- [x] address_guard --tracked exit 0; own-diff IP grep: only the documented
  10.0.0.1 sandbox placeholder in the new test fixture; attribution grep 0
  hits
- [x] real-CLI smoke (sandbox HOME + canaries, COLUMNS 80/120): 17 surfaces
  x both widths = 34 runs, all ok — rc as designed, zero trips, zero new
  HOME files, no ANSI, no line over budget on help surfaces, no placeholder
  text, no rendered flightdeck
- [ ] re-stamp on the MERGED tip (lane advanced mid-run: BROKEN-HEAL landed
  as b97b4b48 -> merge 18ce7727 + test rebase 7a9dcba7): surface gate green
  (12 pass/1 skip), bipartite 0 NEW / 0 STALE / 0 OVER (12 findings <-> 4
  rules: F3 D-rc x2 + MONITOR B x10), rules file = union of both prunes
  (27 - 20 - 3 = 4); canonical suite ALL GREEN both interpreters on frozen 7a9dcba7 (FROZEN_TIP==TIP_AFTER verified)
- [ ] pushed; sha reported (branch wt/t_fc307a51 pushed; final doc commit TBD)

## Follow-ups found (NOT folded into F5 — recorded per protocol)

- `start-daemon -h` / every `-h` outside top-level `hscc -h`: falls through
  to dispatch (start-daemon would LAUNCH the daemon loop on a help request).
  The audit probes `--help` only, and the codebase convention in main() is
  `args[1] == "--help"`, so fixing the whole CLI's `-h` gap is a separate
  surface card (would touch every dispatcher); deliberately not widened here.
- `hscc help <topic>` prints COMMAND_HELP values via `print` (style (E)
  "print"); convergence card (E) tracks that, not F5.
