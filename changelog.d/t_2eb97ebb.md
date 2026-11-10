kind: Fixed
task: t_2eb97ebb

- **The sqlite ignore rules are on `main` and now have a changelog entry; the
  card that asked for them had gone stale, not wrong.** `*.sqlite` and
  `*.sqlite3` are in `.gitignore` and live, measured in a clean checkout of
  `main` at `789130dc`: `git check-ignore -v` names the **rules**, not
  positions — `…:*.sqlite  db.sqlite`, `…:*.sqlite3  probe.sqlite3` and
  `…:*.sqlite  hscc_daemon/nested.sqlite` — and a planted repo-root `db.sqlite`
  is invisible to `git add -A`. No line number is quoted, on purpose:
  `grep -n sqlite .gitignore` answered 75-76 at `8df6a352` and 84-85 at
  `789130dc` only because an unrelated block landed above them, so a pinned
  number goes false while the rule `check-ignore` names has not moved.
  No repo-root
  `db.sqlite` is tracked (`git ls-tree -r --name-only HEAD | grep -i sqlite` →
  only `hscc-api/tests_gateway/diag_sqlite.py`, a source file), so the deletion
  half of `071fdb4e` is moot.
  **Nothing was landed by this card except this file.** The card's ask was
  written against `main` at `06a12aaf`; PR #275 squash-merged at
  `2026-11-10T02:54:34Z` into `8df6a352` and its head already carried
  `071fdb4e` (authored 01:56 UTC, before that merge), so the squash took the
  `.gitignore` hunk. Re-landing it would have been an empty diff, and no other
  PR was merged or touched as a side effect.
- **Probe lesson, recorded because the wrong probe is what produced the stale
  card: in a squash-merged repo, ancestry cannot answer "did this change land".**
  `git merge-base --is-ancestor 071fdb4e main` is `false` *at* `8df6a352` — the
  squash rewrote the commit — while the content is present and effective, and it
  is `false` at `789130dc` too. The question "is this change on `main`" is a
  **tree** question, and the tree answer is scoped to the rules and pinned to a
  sha: `git diff --stat 071fdb4e 8df6a352 -- .gitignore` is **empty** (the file
  was byte-identical there, and that is what answered the question). It is *not*
  empty at `0a4db383` or later — `git diff --stat 071fdb4e 0a4db383 -- .gitignore`
  is `.gitignore | 9 +++++++++` — because #279 appended its unrelated
  `.fastembed_cache/` block to the same file, and every added line in that diff
  is that block: `git diff 071fdb4e origin/main -- .gitignore | grep -ci sqlite`
  is `0`, so the two sqlite rules are still unchanged between the two trees.
  The durable form of the claim is therefore "the tree matched at `8df6a352`, and
  the only divergence since is later, unrelated content" — not "the file is
  byte-identical to `main`", which a stranger's append can falsify without moving
  the change one inch. Any card that opens on an ancestry check plus a
  `git branch -a --contains` roster is asserting where the *commit* is, not
  whether the *change* is, and will go stale the moment the PR it waits on merges.
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
  match a WAL-mode connection's companions: in the same clean clone of
  `origin/main`, `git check-ignore -v db.sqlite-wal db.sqlite-shm
  db.sqlite-journal` exits 1 with no output and `git add -A --dry-run .` answers
  `add 'db.sqlite-journal' / add 'db.sqlite-shm' / add 'db.sqlite-wal'`. Three
  lines would close it, left to their own card instead: the ask here named two
  specific rules, and `.gitignore` is a live collision hotspot — open PR #279
  appends its block at the same end-of-file hunk (`9 0 .gitignore`), so a second
  EOF append would conflict for no benefit.
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
