# Audit note — t_e6bff987: cwd-dependent MissingStyle 'error' flake in test_project.py

## Summary

Two `hscc-project/tests/test_project.py` partial-failure tests crashed with
`rich.errors.MissingStyle: Failed to get style 'error'` when pytest was run
from inside `hscc-project/`, but passed under `scripts/run_tests.sh`. Root
cause was a real defect in the fallback path of
`hscc-project/flightdeck/commands/_theme.py`, not a test-logic bug and not a
collection-order artifact.

## Root cause (confirmed)

`_theme.py` is flightdeck's handle on `hscc_daemon.cli_theme` (the single
source of the Rich palette). It imports `hscc_daemon.cli_theme` lazily and,
when that peer package is unavailable, falls back to a **plain
`rich.console.Console` with NO theme**. But every flightdeck card
unconditionally uses semantic style names through that module:

- `project.py:111` and `:260`, `sessions`/`digest` paths: `border_style="error"`
- `project.py:160`: `[error]{escape(health)}[/error]` markup in a list table
- `status_panel()`: embeds `[{color_role}]...[/]` markup (e.g. `[error]`)

On a plain Console (`theme=None`), a `border_style="error"` or `[error]` tag
makes Rich call `Style.parse('error')`, which fails because `'error'` is not a
valid colour name → `MissingStyle`. The two tests exercise exactly the
error-coloured partial-failure render.

**Why cwd matters** (verified empirically with a sys.path dump under pytest):

- Run from the repo root (what `run_tests.sh` does — it runs each dir by
  absolute path with cwd = repo root): `sys.path` contains the repo root →
  `import hscc_daemon` succeeds → `_theme.theme()` returns the real
  `cli_theme` → themed Console registers `'error'` → tests pass.
- Run from inside `hscc-project/` (the natural `cd hscc-project && pytest`):
  `sys.path` contains only `hscc-project`, NOT the repo root →
  `import hscc_daemon` raises ImportError → `_theme.theme()` → None → plain
  Console → `MissingStyle` on every error-coloured render.

So the flake was masked by the canonical harness purely because repo-root cwd
happens to put `hscc_daemon` on sys.path. The same crash would hit any
standalone flightdeck deployment (hscc_daemon absent), which the `_theme.py`
docstring explicitly claims to support ("flightdeck still renders (just
unthemed)") — that claim was false on every error path.

## Fix

`hscc-project/flightdeck/commands/_theme.py`: the fallback console is now
created with `_FALLBACK_THEME`, a `rich.theme.Theme` registering the SAME
semantic names as the real palette (`accent`, `primary`, `title`, `dim`,
`label`, `border`, `text`, `ok`, `warn`, `error`), mapped to **neutral
(colourless)** styles so the fallback stays genuinely unthemed while every
markup/style resolves. Bold is kept on the emphasis names so output still
reads structurally. Both `make_console` branches (non-tty and terminal
recreate) attach the theme.

This is the "make the theme always register 'error'" option from the scope —
the fallback now always registers every semantic name, so rendering is
cwd-independent and no card depends on collection order or on the repo root
being on sys.path.

## Regression tests

New `hscc-project/tests/test_theme_fallback.py` (3 tests) forcing the
fallback via an autouse `monkeypatch.setattr(_theme, "theme", lambda: None)`:
- fallback console renders `border_style="error"`, `status_panel(...,
  "error")`, and `[error]`/`[dim]`/`[ok]` markup without raising
- fallback output stays no-ANSI (plain)
- fallback keeps the non-tty width=200 (content preservation)

Verified the tests FAIL against the pre-fix `_theme.py` with the exact
MissingStyle error and PASS with the fix (genuine regression coverage).

## Verification

- `cd hscc-project && pytest tests/test_project.py -q` — green (both partial-
  failure tests now pass from inside hscc-project).
- Full `hscc-project/tests` from inside hscc-project with
  `HERMES_DELEGATED_CHILD_CONTEXT` unset (the run_tests.sh harness
  condition): **1339 passed** — ALL GREEN. (With the delegated-child fence
  set, 2 unrelated sqlite `OperationalError` tests fail — the known
  harness-only live-state fence, explicitly stripped by run_tests.sh; they
  pass on pristine main with the fence unset and are out of scope here.)
- `scripts/run_tests.sh` full 8-dir run: **ALL GREEN under BOTH interpreters**
  (hermes venv and p313) — all 8 dirs ✓ each.
- Both interpreters, from `cd hscc-project`, the 2 fixed tests +
  `tests/test_theme_fallback.py`: **all pass**.
- `cd hscc-project && pytest tests/test_project.py -q` under the hermes venv:
  **46 passed** (fully green).

### Known pre-existing p313 limitation (out of scope, unrelated to this fix)

`TestNoAnsiProject::test_chat_banner` fails under the **p313 (miniconda)
interpreter only**, from any cwd, with `HermesRuntimeUnavailable: cannot
import the Hermes runtime ... hermes_cli/hermes_state live in the Hermes
venv`. This is a deliberate environment split (see
`project_lifecycle.py` `_session_db_for_profile`), not a test-logic or
theme bug. Under the canonical full-suite run, an earlier test's kanban
loader (`flightdeck/core/kanban.py:_load_kanban_db` inserts the hermes-agent
path onto sys.path) makes `hermes_cli` importable and the test passes — so
p313 `run_tests.sh` is ALL GREEN. Run standalone under p313, it fails. This
predates and is independent of the MissingStyle fix (confirmed: fails on
pristine main too). Flagged as a candidate follow-up (make `test_chat_banner`
not depend on collection order / trigger the hermes-agent path insert), but
it is NOT the MissingStyle flake this card was filed for.

## Commits

- `287d444` fix(theme): register semantic styles in _theme fallback console

## Non-negotiables honoured

- Based on `main`, merged + pushed, `python3 hscc-bootstrap/install_payload.py`
  run after merge, no LAN addresses / secrets / AI attribution, gateway not
  restarted, state.db untouched, notes in `docs/audits/`.
