# Audit note — t_d7a8d1f3: theme doctor/decompose/legacy/lint/verify (Rich group C sub-b)

## Status
IN PROGRESS — converting the 5 files to the themed Rich layer via _theme.py.

## Scope
Convert human-facing output of:
  doctor.py, decompose.py, legacy.py, lint.py, verify.py

## Context (already done)
- _theme.py exists (card 1, t_1ca5c6e9).
- project.py, map_sessions.py (card A); message/ask/report/qa (card B) converted.
- Pattern (see message.py): accumulate lines, make_console().print(panel(title, "\n".join(lines))).
  status_panel for success; table for tabular; escape() all user/body text.
  Errors stay plain print(..., file=sys.stderr).
- --json stays BYTE-IDENTICAL (raw print(json.dumps(...)) never routed through Console).
- Non-TTY degrades to plain automatically (make_console at width=200).

## Per-file plan
### lint.py (139 lines, no --json)
Human loop sites in cmd_lint (lines 62-102): build list of lines, single panel.
No --json path. Add no-ANSI regression test.

### verify.py (183 lines, has --json)
Human sites: _cmd_single (lines 107-116), _cmd_all (lines 132, 148-168). --json paths (lines 95-105, 144-146) stay raw. Add no-ANSI + --json byte-identity tests.

### legacy.py (468 lines, has --json)
cmd_legacy_cards line-loop (191-192) -> panel. _print_plan + cmd_migrate_card human. --json path (188-189) stays raw. Add no-ANSI + --json byte-identity tests.

### decompose.py (718 lines, no --json)
cmd_decompose proposal output (570-630) -> panels. No --json path. Add no-ANSI test.

### doctor.py (857 lines, has --json)
cmd_doctor human loop (820-827) -> panels. --json path (808-815) stays raw. stderr summary lines (838,840) deliberately stay stderr. Add no-ANSI + --json byte-identity test.

## TODO
1. Convert each file.
2. Add no-ANSI regression tests (per command) in test files. Test files for these commands: test_verify.py, test_lint.py, test_doctor_command.py, test_legacy.py, test_decompose.py.
   NOTE: card says "Do NOT touch tests" = do not touch sibling cards' files, but each card ADDS no-ANSI regressions for its own commands (per card 2 audit +15 tests). Add for these 5.
3. E2E verify: --json byte-identical + no-ANSI.
4. Suite green both interpreters.
5. Merge to main, push, install_payload.
