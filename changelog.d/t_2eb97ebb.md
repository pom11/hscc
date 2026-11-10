kind: Fixed
task: t_2eb97ebb

- **The sqlite ignore rules landed with PR #275 and now have a changelog entry;
  the card that asked for them had gone stale, not wrong.** `*.sqlite` and
  `*.sqlite3` are live: measured in a clean clone **at `8df6a352`**, `git
  check-ignore -v` names `.gitignore:75:*.sqlite  db.sqlite`, `…:76:*.sqlite3
  probe.sqlite3` and `…:75:*.sqlite  hscc_daemon/nested.sqlite`, and a planted
  repo-root `db.sqlite` is invisible to `git add -A`. (Those rule numbers are as
  of `8df6a352`: `0a4db383` (PR #279) inserted its block at `.gitignore:16` and
  shifted these two patterns to `:84-85` without altering them.) No repo-root
  `db.sqlite` is tracked (`git ls-tree -r --name-only HEAD | grep -i sqlite` **at
  `8df6a352`** → only `hscc-api/tests_gateway/diag_sqlite.py`, a source file), so
  the deletion half of `071fdb4e` is moot.
  **Nothing was landed by this card except this file.** The card's ask was
  written against `main` at `06a12aaf`; PR #275 squash-merged at
  `2026-11-10T02:54:34Z` into `8df6a352` and its head already carried
  `071fdb4e` (authored 01:56 UTC, before that merge), so the squash took the
  `.gitignore` hunk. Re-landing it would have been an empty diff, and no other
  PR was merged or touched as a side effect.
- **Probe lesson, recorded because the wrong probe is what produced the stale
  card: in a squash-merged repo, ancestry cannot answer "did this change land".**
  `git merge-base --is-ancestor 071fdb4e main` is `false` *at* `8df6a352` — the
  squash rewrote the commit — while the content is present and effective. The
  question "is this change on `main`" is a **tree** question, and a tree question
  needs two *named* trees: `git diff 071fdb4e 8df6a352 -- .gitignore` is empty
  (byte-identical), and that is the answer. Never pin the claim to `origin/main`
  instead of a sha — the same diff is 20 lines wide at `8271b81d`, because PR #279
  landed its own `.gitignore` hunk afterwards, which is exactly how this entry
  went stale on a true sentence.
  Any card that opens on an ancestry check plus a `git branch -a --contains`
  roster is asserting where the *commit* is, not whether the *change* is, and
  will go stale the moment the PR it waits on merges.
- **The changelog half was the only real gap.** `071fdb4e`'s own stat is
  `.gitignore | 2 ++` + `db.sqlite | 0` — no fragment — and PR #275's squash
  fragment `changelog.d/t_2cd692f0.md` describes the orch-waker tests, never the
  ignore rules (`grep -n -i gitignore` on it → no match). So the rules reached a
  release candidate with nothing in `[Unreleased]` saying the repo now refuses
  sqlite artifacts, against `changelog.d/README.md` ("one file per card") and
  against the precedent the sibling `.gitignore` chore PR #279 set
  (`.gitignore` + `changelog.d/t_ae3e5f9b.md`).

---

kind: Verified
task: t_2eb97ebb

- **The class `071fdb4e` claimed to close is still open at the sidecar
  variants, and this card deliberately did not close it.** `*.sqlite` does not
  match a WAL-mode connection's companions: in the same clean clone **at
  `8df6a352`** — re-measured at `8271b81d`, unchanged — `git check-ignore -v
  db.sqlite-wal db.sqlite-shm db.sqlite-journal` exits 1 with no output and
  `git add -A --dry-run .` answers
  `add 'db.sqlite-journal' / add 'db.sqlite-shm' / add 'db.sqlite-wal'`. Three
  lines would close it, left to their own card instead: the ask here named two
  specific rules, and `.gitignore` is a live collision hotspot — PR #279 (since
  merged as `0a4db383`) touched the same file with `9 0 .gitignore`, so a second
  append there would conflict for no benefit.
- **No `check-ignore` false-negative from the trailing-slash rule** (skill
  `cwd-model-caches`): both patterns are file patterns with no trailing `/`, so
  they match paths git has never seen — the probe planted files that were brand
  new to the clone and every one still got a line-numbered match. No directory
  pattern was added by this card, so the caveat never engages.
- **`python3 scripts/changelog_fragments.py check` → `OK — [Unreleased] is
  pointer, 8 fragment entries consistent`**, exit 0, with this file present, and
  `changelog_fragments.py list` shows this card contributing exactly two entries
  (`Fixed` + `Verified`). `CHANGELOG.md` untouched — the diff is this one new
  file (`git status --short` → `?? changelog.d/t_2eb97ebb.md`).
