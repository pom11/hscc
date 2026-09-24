# Themed Rich audit: sync.py, release.py, ingest.py, metrics.py (monitor.py)

**Task:** t_c2cae6e9 — convert the human-facing stdout of the 5 flightdeck
commands (ingest, sync, release, monitor, metrics) through the themed Rich
layer (`_theme.make_console().print(panel(...))` / `status_panel(...)`),
preserving byte-identity of every `--json` / machine path.
**Worker:** worker
**Branch:** `wt/t_c2cae6e9`
**Base:** `main` @ `74bf4ab`
**Result:** 4 commands converted + no-ANSI regression tests; 1 command
(monitor) intentionally left as-is (interactive full-screen loop, precedent of
sub-b's `qa --watch`). All `--json` paths byte-identical. Suite green under
both pinned interpreters.

## How ran

```
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  <venv>/bin/python -m pytest -q hscc-project/tests
```
| Interpreter | Path | Result |
|---|---|---|
| A (hermes venv) | `~/.hermes/hermes-agent/venv/bin/python` | **1326 passed** |
| B (conda p313) | `~/.miniconda3/envs/p313/bin/python` | **1326 passed** |

Baseline on `main` was 1318 passed for hscc-project; +8 = the 8 new no-ANSI
regression tests added in this card (metrics +2, release +2, sync +3,
ingest +1).

## What changed (source)

Theming happens at the presentation call site (`cmd_*` / `_print_*`), NOT
inside `render()` — `render()` stays a pure function, so callers/tests that
call `cmd.render(r)` directly (e.g. metrics `test_window_is_stated_in_header`)
are unaffected. Every user/body-derived string goes through `escape()` so
literal `[x]` / `[ ]` brackets render literally instead of being swallowed as
Rich markup. `escape()` is identity for normal text, so no human output
changes.

- **metrics.py** — human branch: `for line in render(...): print(line)` →
  `make_console().print(panel("metrics", "\n".join(escape(line) for line in render(...))))`.
  `--json` (`print(json.dumps(render_json(...)))`) untouched.
- **release.py** — `_print_plan` → `panel("release — plan", ...)` preserving
  `release plan for {name} {version} (dry run — nothing executed):`;
  `_print_apply` → `status_panel(..., status="ok", title="release --apply")`
  preserving `released step: {step}` and `bumped {name} {version_file} to
  {version}`; `_print_verify` VERIFIED branch →
  `status_panel(..., status="ok", title="release — verify")`. UNVERIFIED /
  FAILED stay on stderr (loud, unthemed). No `--json` path exists.
- **sync.py** — apply/conflict notes accumulated into `applied_lines` for a
  single `status_panel(..., status="ok", title="project sync --apply")`.
  Human render → `panel("project sync", escape(render(...)))`. **`--json`
  path keeps raw `print()` of the applied/conflict notes then raw
  `print(_render_json(...))` — byte-identical to before** (a theme panel
  border would pollute a JSON stream, so it is deliberately not themed).
- **ingest.py** — ACCEPTED PROPOSED ROADMAP block →
  `panel("PROPOSED ROADMAP", f"for {escape(proj.name)} ...\n{escape(extracted)}")`.
  The `- [x]` / `- [ ]` checklist marks in `extracted` are escaped so they
  render literally (this was the specific bug Rich theming would introduce).
  `_print_parse_evidence` + `[ingest]` progress stay on stderr, raw. No
  `--json` path.
- **monitor.py** — **UNCHANGED** (see below).

## Why monitor.py was left as-is

`monitor` is an interactive full-screen re-render loop: `cmd_monitor` clears
the terminal (`ANSI(_clear)`) and re-draws the live board view on every tick
via `sys.stdout.write(_clear() + body)`. That is not a one-shot human deliverable
a panel boxes — it is a live full-screen TUI. This is exactly the precedent
sub-b set for `qa --watch` (left as a live full-screen loop while `qa`'s
one-shot human view was themed). Theming monitor's frame inside an already-ANSI
`_clear` loop would add nothing and contradict the no-ANSI invariant the card
polices. Documented here instead of a misleading "converted".

## Escape-correctness (the point of this card)

Rich treats `[text]` as a style tag and swallows it. Checklist marks appear in
ingest's extracted roadmap (`- [x] done`, `- [ ] todo`) and any user text can
carry brackets. Every converted command escapes content before it reaches a
panel:
- metrics: every `render()` line is escaped.
- sync: `render(...)` block + `proj.name/repo/board`, `slug`, `c.name/c.slug`
  are escaped.
- release: `project.name`, `version`, `step`, `files_written`, `installed/
  released_version` escaped.
- ingest: `proj.name`, `args.project`, and the whole `extracted` roadmap body
  escaped.

`escape()` is a no-op for plain text, so no content the existing tests assert
on (e.g. `"applied: wrote 'hscc'"`, `"PROPOSED ROADMAP"`, `"MATCHED"`,
`"release plan for acme 1.9.0"`, `"100%"`) is altered on screen.

## No-ANSI regression tests (new)

Per converted command, a `Test<Command>NoAnsi` class (sub-b pattern in
test_verify.py): `import io as _io / contextlib as _contextlib`, a `_no_ansi`
helper that `redirect_stdout`s into a StringIO and asserts neither `\x1b[` nor
`\x1b` appears, plus content substrings (so the theming didn't just blank the
output). `--json` byte-identity asserted for the commands that have it
(metrics, sync) by rebuilding the same payload from the same inputs and
comparing exact stdout.

| Test file | New tests | Cover |
|---|---|---|
| test_metrics.py | +2 | human view plain, no `\x1b`; `--json` byte-identical |
| test_release.py | +2 | dry-run plan plain; apply+verify plain, no `\x1b` |
| test_sync.py | +3 | dry-run human plain; apply human plain; `--json` byte-identical |
| test_ingest.py | +1 | accepted roadmap plain no `\x1b`, `- [x]` literal |

monitor has no regression test (not converted).

## Byte-identity proof (metrics, sync)

`--json` stdout is asserted equal to re-computing the same report from the same
inputs and JSON-encoding it — not just "no ANSI". This catches a themed panel
accidentally replacing a machine stream. No box borders / no color reach a
`--json` stdout: the `--json` branch `return`s before any `make_console()`
call, and sync's apply/conflict notes stay raw `print()` in the json branch.

## Files touched (worktree only)

- `hscc-project/flightdeck/commands/{metrics,release,sync,ingest}.py` (modified)
- `hscc-project/tests/test_{metrics,release,sync,ingest}.py` (no-ANSI tests added)
- `docs/audits/t_c2cae6e9_verify_e2e.py` (added — E2E proof script, both interpreters)

## Anti-hotspot note

The 4 source files each have exactly one card's worth of diff; no file is
shared with the sibling sub-c work (t_4a548958 touches decompose/roadmap/why,
a disjoint set). The follow-up step (test additions) lived entirely inside this worktree once
the accidental primary-checkout edits were reverted and re-applied here; the
primary checkout was left clean of this card's changes.

## Caveats / follow-ups

- **monitor.py**: intentionally not themed. If a future card wants the live
  board TUI restyled, that is a distinct design task (Rich `Live`), not a
  panel swap — do not bolt a panel onto the `_clear` loop.
- sync `--apply --json`: the applied/conflict notes precede the JSON payload
  on stdout exactly as before; they are raw bytes, not themed. A machine
  consumer of `--apply --json` should parse only the trailing JSON object.
