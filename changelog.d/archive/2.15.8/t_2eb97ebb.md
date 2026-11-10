kind: Fixed
task: t_2eb97ebb

- **The sqlite ignore rules are on `main` and now have a changelog entry; the
  card that asked for them had gone stale, not wrong.** `*.sqlite` and
  `*.sqlite3` are in `.gitignore` and live, measured in a clean checkout of
  `main` at `fc7c1caf`: `git check-ignore -v` names the **rules**, not
  positions — `…:*.sqlite  db.sqlite`, `…:*.sqlite3  probe.sqlite3` and
  `…:*.sqlite  hscc_daemon/nested.sqlite` — and a planted repo-root `db.sqlite`
  is invisible to `git add -A`. No line number is quoted, deliberately:
  `grep -n sqlite .gitignore` answered 75-76 at `8df6a352` and 84-85 at
  `fc7c1caf`, moved only by blocks other cards appended above them, so a pinned
  number goes false while the rule `check-ignore` names has not moved. No
  repo-root `db.sqlite` is tracked (`git ls-tree -r --name-only HEAD |
  grep -i sqlite` → only `hscc-api/tests_gateway/diag_sqlite.py`, a source
  file), so the deletion half of `071fdb4e` is moot.
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
  is `false` at `fc7c1caf` too. The question "is this change on `main`" is a
  **tree** question, and the tree answer is scoped to the rules and pinned to a
  sha: `git diff --stat 071fdb4e 8df6a352 -- .gitignore` is **empty** — the file
  was byte-identical there, and that is what answered the question. It is *not*
  empty at `0a4db383` or later (`git diff --stat 071fdb4e 0a4db383 -- .gitignore`
  is `.gitignore | 9 +++++++++`, #279's unrelated `.fastembed_cache/` block), and
  the divergence grew again when #283 added the sidecar rules. What survives all
  of that is the **deletion column**: `git diff --numstat 071fdb4e origin/main
  -- .gitignore` reads `18 0` at `fc7c1caf` — eighteen lines added by other
  cards, **zero** removed — and `git show origin/main:.gitignore | grep -cx
  '*.sqlite'` is `1`, so neither rule has been edited since `071fdb4e`. The
  durable claim is therefore "the tree matched at `8df6a352`, and everything
  since is additive", not "the file is byte-identical to `main`": an append by
  someone else falsifies the latter without moving this change one inch. (Worth
  recording because this entry's own first correction repeated the mistake: it
  asserted the diff contained no sqlite line, and #283 merged a block full of
  them minutes later. The deletion column and `check-ignore` naming the rule were
  both still true.) Any card that opens on an ancestry check plus a
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

- **What `071fdb4e` did not close: the WAL sidecar variants — left to their own
  card on purpose, and their record is not this one's.** `*.sqlite` matches no
  sidecar: measured at `8df6a352` in a clean clone, `git check-ignore -v
  db.sqlite-wal db.sqlite-shm db.sqlite-journal` exited 1 with no output and
  `git add -A --dry-run .` answered `add 'db.sqlite-journal' / add
  'db.sqlite-shm' / add 'db.sqlite-wal'`. t_4e553569 landed those three rules
  (`*.sqlite-wal` / `-shm` / `-journal`, PR #283), so this entry claims nothing
  about whether the class is open now — it is that card's record, and the
  `*.sqlite3-*` variants are a further card's subject. The ask here named two
  specific rules and `.gitignore` was a live collision hotspot: PR #279 was
  appending its block at the same end-of-file hunk (`9 0 .gitignore`), so a
  second EOF append would have conflicted for no benefit.
- **No `check-ignore` false-negative from the trailing-slash rule** (skill
  `cwd-model-caches`): both patterns are file patterns with no trailing `/`, so
  they match paths git has never seen — the probe planted files that were brand
  new to the clone and every one still got a rule-naming match. No directory
  pattern was added by this card, so the caveat never engages.
- **This card's gate run** of `python3 scripts/changelog_fragments.py check`
  printed `OK — [Unreleased] is pointer, 8 fragment entries consistent`, exit 0,
  with this file present (that count is the roster as of that run, not a
  constant — it reads 13 once #279-#283's fragments landed).
  `changelog_fragments.py list` shows this card contributing exactly two entries
  (`Fixed` + `Verified`). `CHANGELOG.md` untouched — the diff was this one new
  file.
