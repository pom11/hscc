kind: Security
task: t_0a2d4add

- **the operator's home directory was spelled in absolute form 383 times across
  66 prose files of this public repo.** `docs/` (incl. `HANDOFF.md` and ~60
  `docs/audits/*` reports, plus 9 archived audit `.py` scripts and one
  `overnight-*.log` transcript), `ios-app/docs/`, one archived fragment under
  `changelog.d/archive/2.5.7/` and `CHANGELOG.md`'s historical entries all
  carried `/Users/<operator>/…`. Scrubbed **in place** — no report deleted, no
  sentence dropped, transcript shapes preserved — by one byte-level
  substitution, `/Users/<operator>` → `~` (and `/home/<operator>` → `~`, of
  which scope had zero), matching the `~`-relative convention the rest of the
  repo's prose already uses (`~/.hermes/hermes-agent/venv/bin/python`,
  `~/dev/hscc`, `~/.local/bin/sparkrun`). Every *sanctioned* fixture spelling
  is untouched by construction: the substitution keys on the operator home
  token only, so the six fixture names under `/Users/` — `operator`,
  `example`, `x`, `dev`, `you`, `runner` — 33 files, several of them load-bearing
  detector inputs (e.g. the `hermetic_subprocess_spawn` tests and the
  `model_decode_check` fixtures' `PATH_PLACEHOLDER`) — survive verbatim. The
  10 non-`.md` files in scope were checked against the content-pinning trap
  first: `git grep` finds them only in `docs/README.md`'s filename index and
  two changelog mentions — no test asserts on their bytes, no CI leg executes
  them — so the substitution is mechanical for every file touched.

---

kind: Verified
task: t_0a2d4add

- Scope closure measured on this tree: `git grep -I -E '/Users/<operator>|/home/<operator>'
  -- docs ios-app/docs changelog.d CHANGELOG.md` → **rc 1, no output** (was 66
  files / 383 occurrences). Fixture invariance: the per-file-count of the
  sanctioned `/Users/(operator|example|x|dev|you|runner)` spellings is
  **33 files on `origin/main` and 33 on this branch**. Nothing deleted:
  `git diff --stat origin/main...HEAD --diff-filter=D` → no output.
- Docs gates green on the Hermes venv python: `scripts/tests/test_readme_lint.py`
  and `scripts/tests/test_changelog_fragments.py` pass, and
  `scripts/readme_lint.py --repo . --strict` keeps `findings: 0` (no new file
  was added under `docs/` — the evidence lives here and in the PR body, per
  readme_lint rule (4)).
