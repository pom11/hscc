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

- [ ] base ground-truth audit (base_findings.json) inspected
- [ ] red tests committed
- [ ] fix committed
- [ ] rules pruned + gates green
- [ ] pushed; sha reported
