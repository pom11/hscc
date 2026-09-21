# Telegram removal from HSCC repo — t_07fd5b86

Task: remove Telegram from the HSCC repo entirely, landing AFTER "make Telegram fully optional" (t_1a0a47e8 / wt/tg-optional).

## Invariants that must survive
1. registry `Project.topic` field + data (historical metadata for archive attribution).
2. `flightdeck/core/archive.py` and `session_discovery.py` keep READING telegram-sourced rows (source='telegram' stays a valid read).
3. `hscc project chat --resume` still opens an old session by id.
4. `hscc project sessions hscc` still lists the 3 archived Telegram sessions.

## Guard against over-deletion
`message`, `ask`, `report`, `qa`, `decompose` are general commands — keep the command, drop the Telegram delivery path. Note if removing Telegram leaves a command with no delivery mechanism at all.

## Verify by execution
- HSCC_TEST_PY=... bash scripts/run_tests.sh ALL GREEN on 8 packages.
- grep -ri telegram over repo (excl _archive/, .git, .worktrees) — report remaining hits with one-line justification each.
- hscc project sessions hscc still lists 3 archived Telegram sessions.
- hscc --help and hscc project --help must not reference Telegram.

## Status
- Started: orienting on scope, cataloguing telegram references.

(findings will be appended as they surface)
