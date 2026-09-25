# README review: docs + hscc-skills + hscc-project (+ its docs) (t_0ae232a4)

Card: PART 2 README REVIEW (3/5) — docs/README (9 lines) + hscc-skills (18 lines)
+ hscc-project/README + hscc-project/docs/README. Verify TRUE after telegram
removal, main-only cutover, Rich CLI theming (hscc-project fully converted in
the Rich CLI epic). Fix or DELETE. VERIFY EVERY COMMAND BY RUNNING IT.
PUBLIC repo — scrub LAN/tailnet to 100.64.0.1 in any commit.

SCOPE per card title:
- docs/README.md (9 lines)
- hscc-skills/README.md (18 lines)
- hscc-project/README.md (379 lines)
- hscc-project/docs/README.md (39 lines)

## Findings so far (IN PROGRESS — not final)

- Telegram was REMOVED from the fleet (commit 5d9c505 / f91924c, release
  2.0.0; hscc-project specifically: 8c2af35 "drop Telegram delivery from
  hscc-project (flightdeck)"). Escalation is now DESKTOP, not Telegram.
- hscc-project/README.md (379 lines) was created during the flightdeck→hscc
  relocate (e34d150) and NOT updated since telegram removal. It is full of
  Telegram references (lines ~24, 36, 60, 86, 89, 240, 295-300, 336, 376) and
  documents `flightdeck <cmd>` standalone plus `flightdeck-mcp`.
- hscc-project/docs/README.md (39 lines) heavily references Telegram config
  (line 15) and *contradicts itself*: line 18-19 says "There is no separate
  COMMANDS or CONFIGURATION doc beyond the example files above", yet
  COMMANDS.md (50K) and CONFIGURATION.md (9.7K) ARE present in the dir.
- docs/README.md (9 lines): references docs/superpowers/ gitignored — need to
  verify that path still holds.

## Pending

- Run `hscc project --help` (full surface) + verify each documented command.
- Decide fix vs DELETE for hscc-project/README.md + docs/README.md.
- Verify hscc-skills/README.md commands by running hscc.py.
- Verify docs/README.md claims (superpowers path, CHANGELOG, GitHub releases).
- Record merge/push/deploy facts here at the end.
