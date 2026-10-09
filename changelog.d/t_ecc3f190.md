kind: Security
task: t_ecc3f190

- **The guard's artefact refusal was defeated by a trailing byte in the file
  name, so `evil.pyc ` was tracked and invisible to both gates.** `is_build_artefact()`
  matched the suffix of the **raw** path string, and git tracks names with
  trailing bytes verbatim: `git add -A --force` accepted `evil.pyc `, `evil.pyc.`
  and `dir/evil.pyc\t` (measured: APFS creates them and `git ls-files -z` reports
  the trailing byte back), while the hook and `--tracked` both returned `[]` —
  the name looked like it ended in nothing. Found by reviewer round 3 of
  t_3e6d3db7. Policy now compares a single normalised form, `_name_key()`:
  lower-cased, backslashes folded, and every path **segment** trimmed of leading/
  trailing whitespace, `.` and NUL (per-segment, because the artefact in
  `pkg/__pycache__ /m.dat` is the directory and a whole-path trim never sees its
  byte). Normalisation is applied **only** where it can widen a refusal: the skip
  list and `ALLOWED_BINARY_PATHS` stay on the raw path on purpose, because
  normalising a skip or a waiver would catch *less* — a trailing byte would turn
  `x.png ` into a skipped asset, or let a name match a hatch entry narrower than
  what someone reviewed. A trailing-byte name therefore never matches the hatch
  and falls through to the refusal: it fails closed. The verdict keeps the **raw**
  path, so `git rm --cached <path>` names a file that exists and the two gates
  still describe one tree with one string. Blast radius measured before choosing:
  1103 tracked files, 0 trailing-whitespace names, 0 trailing-dot names, 0
  NUL-bearing names, and **0 tracked paths whose verdict the trim changes** — so
  nothing tracked today (including both `.png` assets and any leaf that
  legitimately ends in `.`, e.g. `README.`) newly breaks. `--tracked` 0.152 s
  best-of-7; the trim itself is 0.82 µs/call. Severity stated honestly: this was
  not the accident class the list's job is accidents — no compiler emits a
  trailing-space name, a human must script it — and it was strictly weaker than
  the rename-to-`.txt` limit, which is unchanged. Reasoning + limits:
  `docs/audits/address-guard-binary-policy-t_3e6d3db7.md` §9.
---
kind: Verified
order: 1

- `scripts/tests/test_address_guard.py`: **6 new functions / 27 collected cases**
  (48 → 75) — 22 parametrised refusal cases (the reachable spellings, the NUL
  case, mid-name controls like `docs/ev il.pyc` which must **still** be refused,
  and the `README.`/`Makefile.` negatives that must **not** become newly
  blocked); the asymmetry test that pins normalisation on the refusal only; the
  raw-path verdict test; a live-population guard that re-measures the tracked
  tree on every run so a future `docs/notes.` trips CI rather than a commit; a
  crash-safety case for the new per-character trim over `surrogateescape`-decoded
  paths (a raise there is a traceback on the commit path, which is how
  `--no-verify` gets used); and a two-gate test over a real index entry.
- `hscc_daemon/tests/test_precommit_address_hook.py`: **1 new case** (19 → 20) —
  the card's ASK 2 at the git level: runtime `py_compile` output written to
  `evil.pyc `, a premise assertion on the **raw** `git ls-files -z` bytes (the
  shared helper decodes, which is exactly what would hide the byte under test),
  the shipped hook refusing the commit, then the hook, `--staged` and `--tracked`
  all naming the same path with the same verdict string.
- `.github/scripts/tests/test_redact_guard_report.py`: **1 new case** (93 → 94)
  at the CI job level through the same `_run_job_leg` helper that runs the
  workflow's real step script — the backstop fails on `evil.pyc ` **and** the
  public job log still names it with the trailing byte. The verdict's shape
  (`<path>:0: …`) puts that byte mid-line rather than at line end, so no CI log
  post-processor that strips trailing whitespace can silently eat it.
- 6 mutants, each in a copy outside the workspace, each caught by a real test —
  no survivors: raw-string suffix (pre-fix behaviour, 15 cases), whole-path trim
  (3), normalised verdict (5), normalised skip list, normalised hatch match, and
  trailing-end-only trim (each caught by the case that names it).
- Full suite, both interpreters, worktree frozen at the stamped tip: see this
  card's completion metadata for the SHA and both `RUN_TESTS_RC` lines.
- `python3 scripts/address_guard.py --tracked` and `--staged` both exit 0 on this
  card's own diff; every fixture address in the new tests is assembled at runtime.
