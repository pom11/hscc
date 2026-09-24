# Audit note — t_b2d2c0ae: theme message/ask/report/qa CLI output (flights Rich epic, card 2/21)

## What changed

Converted the human-facing output of `flightdeck/commands/message.py`
(`send`, `read`, `dispatch`, `broadcast`), `ask.py` (`ask render`, `template
list`, `template show`), `report.py` (single-project, `--all`, `--backfill`),
and `qa.py` (one-shot queue render) from bare `print()`/`json.dumps()` to the
flightdeck themed Rich layer — the exact pattern card 1 (t_1ca5c6e9) established
via the shared helper `hscc-project/flightdeck/commands/_theme.py` (guarded lazy
import of `hscc_daemon.cli_theme`). NO second theme layer was created; all four
files route through `_theme.make_console` / `panel` / `status_panel` / `escape`.

Project.py / map_sessions.py (card 1) and every other flightdeck/commands file
were NOT touched — this card stayed inside group B.

## Hard invariants — verified

1. `--json` output byte-identical to pre-conversion.
   - No `print(json.dumps(...))` line changed in any of the four files
     (grep of the diff's `json.dumps` lines is empty).
   - Terminal proof (`docs/audits/t_b2d2c0ae_verify_e2e.py`, real piped
     subprocess): `ask template list --json` output byte-equals the canonical
     `json.dumps(names)`, is valid JSON, and `rc: 0`. (`qa --json`
     byte-identity is proven in the pytest no-ANSI suite — a real subprocess has
     no injectable board, so it can't be a clean byte-identity demo there.)
2. Non-TTY piped output has NO ANSI (`\x1b`). Rich degrades to plain on a
   non-tty console. Proven for every converted command in BOTH the pytest
   no-ANSI regression classes AND the real-subprocess e2e script.
3. `--watch` live surface in qa.py was deliberately LEFT as-is: it is an
   interactive TTY re-render loop that already emits raw ANSI (`_clear`) and is
   not a "human-facing rich render" — theming it would break the frame model.
   Only the one-shot `cmd_qa` human view was themed.
4. Stderr error paths stay plain `print(..., file=sys.stderr)` (matching card 1:
   errors deliberately left out of the themed layer).
5. Rich-markup pitfall fixed: literal `[ask]` / `[dry-run]` / `[report]` tags in
   body text are escaped for Rich so they render literally — a bare `[dry-run]`
   was being swallowed as a style tag (caught by the pre-existing MCP test
   `test_report_defaults_apply_false_and_mutates_nothing`, whose `"dry-run" in
   out` assertion failed until the `\[dry-run]` escape was added).

## Regression tests added (15)

One no-ANSI test per converted command, in the per-command test files, each
capturing stdout to a non-tty StringIO and asserting no `\x1b[` escape bytes +
content preserved; `--json` paths additionally assert byte-identity to the
canonical `json.dumps(...)`:

- test_message.py (5): send, read, dispatch dry-run, dispatch apply, broadcast.
- test_ask.py (4): render, template list human, template list `--json`
  byte-identical, template show.
- test_report.py (3): dry-run, apply, nothing-to-report (all via cmd_report_all).
- test_qa.py (3): human view, empty view, `--json` byte-identical.

## Width nuance

`_theme.make_console` (card 1) already recreates a non-tty console at width=200
so wide rows neither wrap mid-phrase nor collapse a table's leading column; the
four converted commands inherit it unchanged. The real-subprocess e2e proof
confirms the panel/table bodies stay on whole lines in a pipe.

## Verification

- Suite under the Hermes venv: `1241 passed, 30 failed` on main →
  `1256 passed, 30 failed` on this branch (+15 new, SAME 30 failures).
- Suite under `/Users/desac/miniconda3/envs/p313/bin/python`: `1241 passed,
  30 failed` on main → `1256 passed, 30 failed` on this branch (+15 new).
- The 30 failures are the PRE-EXISTING `tests/test_standup.py` set (Hermes
  `kanban_db_connect` sqlite3.OperationalError — a Hermes-version/env issue),
  shown identical on main under BOTH interpreters via git worktree checkouts
  (`/tmp/hscc_main_check`). Branch failure set == main failure set byte-for-byte:
  `diff main_failures.txt branch_failures.txt` is empty.
- No existing assertion was weakened; all `--json` payload lines untouched.

## Commands that produced each number

- `python -m pytest -q` (hermes venv) → `30 failed, 1256 passed` on branch.
- `/Users/desac/miniconda3/envs/p313/bin/python -m pytest -q` → `30 failed, 1256 passed`.
- main checkouts: `git worktree add -f /tmp/hscc_main_check main` then same
  pytest → `30 failed, 1241 passed` under both interpreters.
- `diff /tmp/main_failures.txt /tmp/branch_failures*.txt` → empty (same 30).

## Commits

- (list final commit shas here after merge flow)
