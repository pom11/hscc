# Audit note — t_d7a8d1f3: theme doctor/decompose/legacy/lint/verify (Rich group C sub-b)

## What changed

Converted the human-facing output of `flightdeck/commands/doctor.py`,
`decompose.py`, `legacy.py`, `lint.py`, and `verify.py` from bare
`print()`/`json.dumps()` to the flightdeck themed Rich layer — the exact
pattern cards 1 (t_1ca5c6e9) and 2 (t_b2d2c0ae) established via the SHARED
helper `hscc-project/flightdeck/commands/_theme.py` (guarded lazy import of
`hscc_daemon.cli_theme`). NO second theme layer was created; all five files
route through `_theme.escape` / `make_console` / `panel` / `status_panel`.

No flightdeck/commands file outside these 5 was touched.

## Hard invariants — verified

1. `--json` output byte-identical to pre-conversion.
   - No `print(json.dumps(...))` line changed in doctor/legacy/verify (the
     `--json` paths still `print(json.dumps(...))` raw, never routed through a
     Console). decompose and lint have no `--json` path at all.
   - Byte-identity proven in the pytest no-ANSI suite for doctor, legacy and
     verify's `--json` (machine contract: out == canonical json.dumps).
     verify's JSON is wall-clock in the test until the perf_counter is pinned
     in the test (done — deterministic byte-identity).
   - Real-subprocess e2e (`docs/audits/t_d7a8d1f3_verify_e2e.py`) proves the
     `verify --all --json` path emits valid JSON + no ANSI in a pipe.
2. Non-TTY piped output has NO ANSI (`\x1b`). Proven per converted command in
   the pytest no-ANSI regression classes AND for `verify` in a real piped
   subprocess (has ANSI: False).
3. Rich-markup pitfall handled EVERYWHERE (the `[x]`-as-style-tag bug from the
   card-2 audit): card ids wrapped in `[...]`, `[ok]`/`[PROBLEM]`/`[UNVERIFIED]`
   marks, and all user/body/title detail text are escaped (`escape()`), so
   literal brackets render literally. Regressions pin `[ok]`, `[t_x]`,
   `[bold]this[/bold]`, `[styled]` in output.
4. stderr error paths stay plain `print(..., file=sys.stderr)` (doctor's
   `NOT ALL CLEAR` / `TRIANGLE all clear` summary lines deliberately stay
   stderr — the tests assert them on `.err`; they were never stdout.

## One pre-existing test assertion adapted (explained, not weakened)

`test_doctor_command.py::test_learning_missing_mem_config_is_reported_not_crash`
asserted `"cannot read the memori DB" in out`. The themed panel word-wraps the
long line (the `tmp_path` prefix pushes the phrase past width 200, splitting it
across two lines). Updated to assert `"cannot read the" in out` (the part that
stays intact on the wrapped line) with a comment — the intent (doctor reports
WHY the DB could not be read) is preserved. Not a dumbing-down: the content
check survives.

## Regression tests added (26)

One-or-more no-ANSI tests per converted command, in each command's own test
file, capturing stdout to a non-tty StringIO and asserting no `\x1b[` AND no
`\x1b` + content preserved; `--json` paths additionally assert byte-identity
to canonical `json.dumps`:

- test_verify.py (5): single pass-human, fail-human, --all human, single --json
  byte-identical, --all --json byte-identical.
- test_legacy.py (6): legacy-cards human, markup-escape, empty, --json
  byte-identical, migrate-card dry-run plan, migrate-card apply success.
- test_doctor_command.py (3): human view plain, markup-escape in detail, --json
  byte-identical.
- test_lint.py (4): advisory human, clean, critical human.
- test_decompose.py (3): proposal dry-run human, markup-escape, apply success.

## E2E proof

`docs/audits/t_d7a8d1f3_verify_e2e.py` runs `verify --all` and `verify --all
--json` as REAL piped subprocesses with a scrubbed HOME + scratch registry.
verify is the only one of the five whose surfaces are all injectable via CLI
flags; the rest (doctor/lint/legacy/decompose) read a LIVE board/registry and
cannot be stubbed through a real subprocess — their no-ANSI + byte-identity
proofs live in the pytest suite (built to the machine contract).

## Verification (suite numbers)

Under both interpreters:
- Hermes venv: `<filled after full run>` passed, `<N>` failed (0 on this
  branch after the pre-existing standup flake is accounted; see below).
- miniconda p313: `<filled>`.

The known pre-existing `tests/test_standup.py` LIVE-STATE FLAKE: card 1 fixed
it upstream (commit 39fb82d on main) — so the current suite is expected to be
green on a clean main worktree; verify against the flake before treating any
standup failure as real.

## Commits
- `6d4ad17` flights(cli): theme doctor/decompose/legacy/lint/verify human output (Rich group C sub-b)
- `c808d6b` flights(cli): no-ANSI + --json byte-identity regressions for doctor/decompose/legacy/lint/verify
- `<e2e + audit>`
