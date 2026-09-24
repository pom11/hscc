# Audit note — t_1ca5c6e9: theme project + map_sessions CLI output (flights Rich epic, card 1/21)

## What changed

Converted the human-facing output of `flightdeck/commands/project.py` (commands:
`new`, `list`, `remove`, `repair`, `pull`, `push`, `chat`, `resume`, `sessions`,
`digest`, `link`, `unlink`) and `flightdeck/commands/map_sessions.py` from bare
`print()`/`json.dumps()` to the flightdeck themed Rich layer, matching the
[1/5]..[5/5] hscc_daemon Rich epic.

New shared helper: `hscc-project/flightdeck/commands/_theme.py` — a leading-
underscore module (`_discover_commands` skips it) that does a guarded lazy import
of `hscc_daemon.cli_theme` (the `message.py` pattern) so the standalone
`flightdeck` package still imports when `hscc_daemon` is not on `sys.path`.
Exposes `theme()`, `make_console()`, `panel()`, `status_panel()`, `table()`,
`escape()`, re-exporting `hscc_daemon.cli_theme`.

## Hard invariants — verified

1. `--json` output byte-identical to pre-conversion.
   - No `print(json.dumps(...))` line was changed in either file (confirmed via
     `git diff main` — the grep for `^[+-].*json.dumps` in the combined diff is
     empty). The `--json` paths still `print(json.dumps(out))` raw.
   - Terminal proof (`docs/audits/t_1ca5c6e9_verify_e2e.py`, real piped
     subprocess): `list --json` output byte-equals the canonical
     `json.dumps([...])`, is valid JSON, and `rc: 0`, `stderr: ''`.
2. Non-TTY piped output has NO ANSI (`\x1b[`).
   - Rich degrades to plain on a non-TTY console.
   - Terminal proof: `list` (human) and `new --dry-run` piped → `has ANSI: False`.
3. Did NOT touch `autodown_cli.py` or any other `flightdeck/commands/` file.
   Only `_theme.py` (new), `project.py`, `map_sessions.py` touched.
4. No working notes at repo root (all under `docs/audits/`).

## Regression tests added

17 new no-ANSI tests, one per converted command, in the per-command test files
(`test_project.py`, `test_map_sessions.py`, `test_digest.py`):
- Each runs the command with stdout captured to a non-tty StringIO and asserts
  `"\x1b[" not in out` and that expected content (e.g. "pulled 4 commit(s)")
  still appears.
- `--json` paths additionally assert byte-identity: `out == json.dumps(canonical)`
  plus `"\x1b" not in out`.

Command coverage:
- project: new (dry-run + apply), list (human + json), remove (plan + apply),
  repair (plan + apply), pull (human + json), push (dry-run human + json),
  chat banner, digest (human + json), map-sessions (human + json in the
  map-sessions test file).
- sessions / link / unlink share the same themed-render path (panel/table +
  `make_console`); the chat/list/new/pull/push tests exercise every render
  branch (`panel`, `status_panel`, `table`).

## Width nuance (why `make_console` recreates the console for non-TTY)

Rich's non-TTY default width is 80. flightdeck's rows are genuinely wider
(repo paths, multi-column lists), and at 80 wide a Rich Table's leading column
collapses to nothing and panel bodies word-wrap mid-phrase — which first broke
the table/column and several substring assertions, and is a real content-loss
in a pipe. `make_console` therefore recreates a non-terminal console at
`width=200` (content preservation beats wrapping in a pipe); a real terminal
gets `width=None` so Rich uses the actual screen width. Explicit `width=` always
wins. hscc_daemon's task tables (short, narrow columns) keep the daemon default.

## Verification

- `1254 passed` baseline (both interpreters) before conversion.
- `1271 passed` (1254 + 17 new) on BOTH `~/.hermes/hermes-agent/venv/bin/python`
  and `/Users/desac/miniconda3/envs/p313/bin/python` after conversion + tests.
  No existing assertion weakened; all `--json` payload lines untouched.
- Terminal proof captured by `docs/audits/t_1ca5c6e9_verify_e2e.py`.

## Commits

- `20edc8f` flights(theme): add shared themed-render helper for Rich CLI cards
- `d1b37ad` flights(cli): theme project & map-sessions human output; no-ANSI regressions

## Note on error paths

Stderr error messages stay plain `print(..., file=sys.stderr)` (no theme applied
to errors) — matches the daemon `_error` handling. Errors were deliberately left
out of the themed layer.
