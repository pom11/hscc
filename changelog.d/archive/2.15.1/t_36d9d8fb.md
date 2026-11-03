kind: Fixed
task: t_36d9d8fb

- **`scripts` leg red on this host — "sibling python3.12 failed the (A)-only
  pass: E402" — was a mis-reported DEPENDENCY failure, and the cross-interpreter
  gate was non-hermetic.** No linter ever ran and nothing ever reported `E402`.
  The captured leg log (`run_tests_49013b1b.log`, `run_tests_t8438.log`) shows
  the sibling dying on `ModuleNotFoundError: No module named 'yaml'` /
  `'rich'`; the gate's `proc.stderr[-400:]` **character** cut landed inside the
  `# noqa: E402` comment on a source line Python echoes into a traceback, so
  the last 4 characters the assertion could show were `E402`. Three cards
  (t_49013b1b, t_84381a8b, t_9e45955c) each read that as a lint/order mystery
  and correctly failed to find one. Two real defects, both fixed:

  *Non-hermetic sibling selection.* The (A)-only pass imports
  `hscc_daemon.hscc` and `flightdeck.cli` at module level, which need `rich`,
  `yaml`, `pygments`, `markdown_it` from the **running** interpreter's own
  site-packages. `_sibling_interpreters()` checked only `os.path.exists`, so a
  bare interpreter became a "sibling" and then failed the leg — for reasons
  outside the checkout. Measured on this host: `/opt/homebrew/bin/python3.12`
  was a red leg until its site-packages gained `rich`/`pyyaml`, and green from
  that moment on with **zero** code change on our side. Selection now probes
  each candidate by loading the audit module itself (same file, same
  module-level imports; ~0.2 s per candidate measured) and drops a
  non-importable one with a recorded reason in `SIBLING_DROPPED`, surfaced in
  the skip message — so a host that has lost its siblings says so instead of
  failing a stranger's diff or silently looking green.

  *Truncation invented the error code.* Failure excerpts now come from
  `_root_cause()`, which reads whole lines and returns the exception line, so a
  `# noqa:` comment can never be blamed for a failure again.

---
kind: Verified
order: 1

- `scripts/tests/test_cli_help_surface.py`: 19 -> **22** cases. New pins —
  `test_sibling_cause_extraction_never_blames_a_lint_code` (asserts BOTH that
  the old character cut contains the literal `E402` and that the new extractor
  returns `ModuleNotFoundError` for the same bytes — the phantom code is
  reproducible, the fix is not vacuous);
  `test_sibling_gate_uses_the_interpreter_that_can_import_the_audit` (builds a
  real **copy** venv with no third-party deps and asserts the candidate is
  dropped AND the drop carries a reason; a symlinked venv resolves back to the
  leg's own interpreter and is deduped before the probe, which the pin states
  explicitly so it cannot regress into a no-op);
  `test_sibling_gate_still_runs_a_real_sibling` (anti-vacuity: every selected
  sibling must actually execute the audit's `--a-table` contract end-to-end and
  answer with parseable JSON — restricted to a phantom surface so it costs
  seconds, not a full walk — and a capable interpreter on PATH must yield at
  least one selected sibling).
- `bash scripts/run_tests.sh` `scripts` leg: **314 passed, 2 skipped** (was 311
  at base on this host; base was red at `1 failed, 310 passed` whenever the
  sibling lacked its deps). Standalone file: 22 passed, 1 skipped (the
  pre-existing `HSCC_CLI_HELP_WRITE_ARTIFACT` skip).
- Mutation-proven in a scratch clone, restored after each probe: neuter the
  runnable probe -> drop pin fails; revert `_root_cause` to the raw `[-400:]`
  cut -> cause-extraction pin fails; make the selector return `[]` ->
  anti-vacuity pin fails.
