# CHANGELOG decomposition — why `changelog.d/`, and the proof it works

Card: t_95f1d6e1 · branch `wt/t_95f1d6e1` · docs-only (zero runtime code
changed; `scripts/changelog_fragments.py` + the one-shot migrator + tests are
new files, no existing module touched)

## The defect

`CHANGELOG.md`'s `[Unreleased]` section had become a serial merge-conflict
point. Every card prepends its bullet into the *same* `### Fixed` (or
`### Verified`) hunk, so two cards landing in the same window collide on the
same lines — regardless of how unrelated their code is.

Confirmed by two landed merges this session:

- `t_163fa09f` landed @ `7864796` (its entry commit `b420d24`, +25 lines into
  Unreleased/Fixed).
- `t_9462260b` collided with it and resolved by merging main into its branch
  (`29d5e97`: *"CHANGELOG.md was the only overlap and it was a keep-both in the
  Unreleased/Fixed list … no code conflict"*), then landed @ `f7f15d3`.

Resolution was always mechanical keep-both, but it costs a manual merge commit
per collision, on the landing branch, forever, for every sibling pair.

## Why option (a) — and not (b) or (c)

The card offered three schemes. Chosen: **(a) per-card entry files,
aggregated at release** (`changelog.d/<task-id>.md`). Justification, also
stated in the landing commit:

- **(b) per-card section headers inside Unreleased** still edits one file. Two
  cards both inserting a new `#### t_xxx` section at the *top* of the same
  hunk still collide — git cannot merge two concurrent insertions at the same
  position. A deterministic rule (sort by task id) fixes the *ordering* but not
  the *insertion point*: the conflict is positional, not semantic. It would
  also still require every worker to edit `CHANGELOG.md`, keeping the file in
  every worker diff.
- **(c) supervisor appends at merge time** removes worker edits entirely, but
  moves entry authorship to someone who did not do the work — entries get
  thin, and the supervisor becomes a serialization point holding prose it
  cannot verify. It also needs the card body to carry the entry text anyway,
  which is option (a) with extra steps.
- **(a)** removes the collision *by construction*: two cards cannot name the
  same file (`changelog.d/t_<own-id>.md` is unique by kanban id), so their
  diffs are provably disjoint. The worker — who has the evidence — writes the
  entry. `CHANGELOG.md` leaves worker diffs completely.

A concrete bonus over (b): `changelog.d/` keeps `CHANGELOG.md` **wholly** out
of worker diffs (version history *and* Unreleased), so even a rebase onto a
moved main cannot conflict on it. The committed `[Unreleased]` block is a
1-line pointer; the release step materialises the real text.

## The scheme

```
changelog.d/t_95f1d6e1.md   <- the ONLY thing a worker writes
CHANGELOG.md                <- never touched by workers; [Unreleased] is a pointer
scripts/changelog_fragments.py  <- render / sync / check / add / list / release / rescue
```

Entry ordering is deterministic — kinds in Keep-a-Changelog order with HSCC's
`Verified` last, entries by `(task id, order, index-in-file)` — so
materialising is byte-identical no matter what merge order the fragments took.
`check` accepts exactly two committed shapes of the block (the pointer, or
what `sync` would write) and fails on anything else, which is what keeps the
rule enforceable instead of aspirational.

## Verification (by execution, as the card requires)

All commands run in this card's worktree; both interpreters
(hermes venv py3.11 + p313).

1. **Suite** `scripts/tests/test_changelog_fragments.py` — **26 cases**, plus
   the pre-existing `scripts/tests/test_dep_pr_watcher.py` (5): **31 passed,
   exit 0 on both interpreters** (`env -u HERMES_DELEGATED_CHILD_CONTEXT`).

2. **Two-branch merge proof on the real repo.** From one base commit, two
   throwaway branches each added a Fixed entry per the new scheme (+ a
   disjoint code file); merged sequentially into a scratch branch:

   ```
   merge A rc: 0 | merge B rc: 0
   unmerged paths: ''            # git diff --diff-filter=U -> empty
   CONFLICT in output: False
   check: OK — [Unreleased] is pointer, 15 fragment entries consistent
   ```

   Both fragments present, both entries assemble (`list` shows both), zero
   conflicts — the card's acceptance criterion. All proof branches/refs then
   deleted and `git worktree prune`d; `git status --porcelain` clean.

3. **Negative control (this is what makes #2 a proof).** The same experiment
   with the OLD rule (both cards prepend into Unreleased/`### Fixed` of the
   single file) *does* conflict: `git merge` returns non-zero with
   `UU CHANGELOG.md`, `--diff-filter=U` → `CHANGELOG.md`. Without this control,
   #2 would only prove that git merges disjoint files. It is also a pytest
   (`test_old_single_file_scheme_conflicts`) so it cannot rot.

4. **Migration losslessness (143 lines / 13 entries).** The migrator refuses to
   write unless every non-blank `[Unreleased]` entry line lands in exactly one
   fragment; its dry-run report: `13 entries -> 3 fragment files (143 entry
   lines, all accounted for)`; attribution `t_267f9d88` ×6 (the doctor/Home
   fixes it authored), `t_9462260b` ×6, `t_163fa09f` ×1 — derived from the
   first-parent landing merges (`merge: … (t_xxxxxxxx)`), not guessed. The
   independent round-trip check re-assembles the fragments and diffs them
   against the pre-migration blob (`32c35ec`): **original 143 lines =
   assembled 143 lines, `identical=True`**. A pytest pins this against the last
   pre-migration `CHANGELOG.md` in history so the check keeps its teeth.

5. **The safety net earned its keep.** The migrator's first cut silently
   dropped the last entry of every section (`split_entries` flushed only at
   EOF). The losslessness check caught it before writing — `LOST: - **The WS
   stop-no-op test…` — fixed in `24c0d27`. A "keep-both was fine" migration
   that quietly lost an entry would have been invisible otherwise.

## In-flight branches (the transition cost, stated honestly)

The repo has ~355 `wt/*` branches; a large set still edits `CHANGELOG.md` the
old way. When such a branch lands on the new base it will conflict **once**, in
the `[Unreleased]` hunk only. `scripts/changelog_fragments.py rescue --task
t_xxxxxxxx --branch <ref>` extracts that branch's added `[Unreleased]` entries
into `changelog.d/<task>.md` (reusing the *tested* entry splitter — no second
diff parser), after which the landing step is mechanical: drop the CHANGELOG.md
hunk, commit the fragment. It raises rather than writing anything when the
branch adds no entry lines, so an entry cannot be silently swallowed on the way
through the transition.

## Docs updated (the rule must outlive this card)

- `changelog.d/README.md` — authoring format, ordering, commands, release step.
- `install/hscc-skills/devops/kanban-worker/SKILL.md` — the auto-loaded worker
  skill now states the rule directly.
- `install/hscc-skills/sdlc-review/SKILL.md` — a `CHANGELOG.md` hunk in a
  worker diff, or a fragment not named after the task id, is a REJECT.
- `install/hscc-skills/devops/kanban-orchestrator/SKILL.md` — card-body
  boilerplate line, so the rule is present at authoring time.
- `docs/README.md` — where the changelog lives, and how to add to it.

**Incident during this card, recorded because it is the same class of failure:**
the fuzzy patcher reported `success` (with unified diffs!) for the two
`install/hscc-skills/**` edits, and the files were silently unchanged on disk.
The card's own diffstat caught it — three doc files were missing from the
committed diff. The edits were re-applied through
`scripts/audits/apply_changelog_skill_docs_t_95f1d6e1.py`, which asserts every
edit on disk before and after writing, and kept as the receipt. A doc rule that
silently does not exist is exactly the failure mode this card exists to
prevent.

## Release drill (throwaway clone of the landed branch)

`release --version 9.9.9-drill` on a fresh clone: produced
`## [9.9.9-drill] - 2026-10-06` with `### Fixed` before `### Verified`, all
18 bullet-lines, the four fragments moved to `changelog.d/archive/9.9.9-drill/`,
the `[Unreleased]` pointer restored, `check` green afterward, and the clone's
`scripts/tests` **31 passed / exit 0**. The follow-up `sync` from the now-empty
working set was a clean no-op. Confirms add → check → release → archive
end-to-end, not just the authoring path.

## Follow-up (not done here, deliberately)

The repo has no tracked release-procedure doc (release steps currently live in
audit-doc prose). Someone should write `docs/RELEASING.md`: bump `VERSION` →
`changelog_fragments.py release --version X` → tag → `gh release create`. Out
of scope for a hygiene card about merge conflicts; noting it so it is not lost.
