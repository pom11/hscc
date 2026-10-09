kind: Security
task: t_3e6d3db7

- **The address guard was blind to binary blobs, so a committed `.pyc` could
  carry a real address past all three gates.** `scan_blob()` returned `[]` for
  any NUL-bearing content, and the pre-commit hook, the pytest gate and the CI
  backstop all call that one function — so a stray `git add -f
  scripts/__pycache__/*.pyc`, or a vendored dir committed with an old build,
  shipped a blob whose embedded string constants hold whatever address the source
  had at build time, invisible to every control and unaffected by `.gitignore`
  (this repo had exactly such a blob in published history before the 2026-10-09
  rewrite). Policy now: **a build artefact is refused on its name, whatever it
  contains** — `*.pyc` `*.pyo` `*.pyd` `*.o` `*.a` `*.so` `*.dylib` `*.dll`
  `*.class` `*.jar` `*.egg` `*.whl` `*.pyz` and any `__pycache__/` segment are
  rejected before their bytes are read. Extract-and-scan was considered and
  declined, and a probe during review showed why it is the *weaker* option, not
  just the slower one: a real `py_compile` output of source carrying a real-shaped
  address has that address visible to a naive scan over the decoded bytes yet
  **invisible to `FORBIDDEN`** — the detector's `\b` assume a text-delimited
  occurrence, and marshal writes no delimiter after a string constant, so the
  byte that follows is the first letter of the next interned name, a word
  character, and the trailing `\b` fails. A content scan inherits that blind
  spot; a name-based refusal never reads the bytes and cannot be defeated that
  way. It would also have no stopping rule (`.zip`, `.gz`, `.jar`, PDFs are
  containers too).
  `ALLOWED_BINARY_PATHS` is the
  escape hatch for a binary that genuinely belongs here, and round-1 review made
  it honest by making it two-tier: a **compiled-artefact suffix is never
  waivable**, so widening the hatch cannot make a `.pyc` trackable-and-unreadable;
  the hatch waives a `__pycache__`-segment path only, and such a path is then
  decoded and text-scanned even when it carries NULs. It also now overrides
  `SKIP_SUFFIXES`, so no hatch entry can ever mean "tracked and never read".
  `report()` splits the two
  offender classes, because "scrub to `10.0.0.x`" is not the fix for a `.pyc` —
  `git rm --cached` is. Hook and `--tracked` cannot disagree: the verdict is
  formatted in one function. Current exposure was measured at zero (0 tracked
  artefacts; 2 NUL-bearing tracked files, both `.png`), and `--tracked` timing is
  unchanged at 0.12 s. Reasoning + limits:
  `docs/audits/address-guard-binary-policy-t_3e6d3db7.md`.
---
kind: Verified
order: 1

- `scripts/tests/test_address_guard.py`: **10 new functions / 19 collected cases**
  — a `.pyc` carrying a real LAN address is refused; every artefact shape is
  refused *even when clean* (the verdict is about the path, not the content);
  **a genuinely compiled `.pyc` defeats `FORBIDDEN` entirely and is still refused**
  (`py_compile` at runtime, address assembled via the `_addr(*parts)` idiom — a
  hand-spliced NUL blob would have been caught by the existing pattern and proved
  nothing); case/backslash forms do not dodge the list; the tracked `.png`
  population and an unknown-extension binary are **not** broken; the allowlist
  exempts the refusal but not the scan; `scan_paths` refuses a deleted-but-tracked
  or unreadable artefact; `report()` names the artefact and `git rm --cached`; and
  the CLI's `--staged`/`--tracked` exit 1 on it (including after a scrubbed tree).
- `hscc_daemon/tests/test_precommit_address_hook.py`: **4 new cases**, including
  the card's agreement test — a real `git commit` through the real hook and the
  real `--tracked` CLI over the same tree emit **identical** verdict strings — and
  a real `git add -f` of real compiler output blocked by the hook.
- `.github/scripts/tests/test_redact_guard_report.py`: **2 new cases** at the CI
  job level, through the same `_run_job_leg` helper that runs the workflow's real
  step script.
- Full suite, both interpreters, clean worktree at the **frozen final gated tip**:
  `scripts/run_tests.sh` **ALL GREEN, RUN_TESTS_RC=0** on py3.11.16
  (`~/.hermes/hermes-agent/venv`) and py3.13.7 (`p313`), 11/11 suites each
  (`scripts` and `.github/scripts` included). The stamped SHA and both legs'
  `RUN_TESTS_RC` lines are in this card's completion metadata; the worktree was
  frozen for the whole stamp window, so the stamped SHA is the SHA that lands.
- `python3 scripts/address_guard.py --tracked` and `--staged` both exit 0 on this
  card's own diff; every fixture address in the new tests is assembled at runtime,
  so none of them is a contiguous real-shaped literal in a tracked file.
---
