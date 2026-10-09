# run_tests.sh never ran `scripts/tests` — the fix, and the evidence

Task: t_95d864fc (child of t_a98c009f). Workspace branch: `wt/t_95d864fc`.
Base: `main` @ fb15d97b.

## Status

- [x] Reproduced the gap at main tip
- [x] Fix applied — `scripts` joins `DIRS` (mechanism decision recorded below)
- [x] Baseline per-leg counts on BOTH interpreters (clean detached worktrees)
- [x] Post-change full suite (py3.11.16) — 10 legs, ALL GREEN
- [x] Post-change full suite (p313 3.13.12) — 10 legs, ALL GREEN
- [x] Changelog fragment (via `scripts/changelog_fragments.py add`, never CHANGELOG.md)

## The gap, reproduced at main tip (fb15d97b)

`scripts/run_tests.sh:23` holds the only DIRS list:

    DIRS=(hscc-bootstrap hscc-commands hscc-roles hscc-cluster hscc-project
          hscc_daemon sparkrun-hermes hscc-api memori_byodb)

The loop resolves `"$ROOT/$d/tests"`, so `scripts/tests/` is unreachable. Measured
on this checkout, standalone:

| interpreter | command | result |
|---|---|---|
| 3.11.16 (`~/.hermes/hermes-agent/venv/bin/python`) | `pytest -q scripts/tests` | 56 passed in 1.25 s |
| 3.13.12 (`~/miniconda3/envs/p313/bin/python`) | `pytest -q scripts/tests` | 56 passed in 1.34 s |

So 56 cases — including the address detector suite `test_address_guard.py` — are
green but excluded from every `ALL GREEN` stamp. (At the t_a98c009f tip
`24a3acca` the same dir holds 97; that branch is not on main yet, which is why
this checkout measures 56.)

Tracked test-file census at main tip (`git ls-files '*test_*.py'` grouped by dir):
the 9 registered plugin dirs, plus exactly two unregistered ones that are not
archived dead code — `scripts/tests` (3 files) and `.github/scripts/tests`
(1 file, `test_check_runtime_deps.py`). `_archive/**` is excluded from collection
by `pytest.ini: norecursedirs`.

## Mechanism — decision and why

Chosen: **`scripts` joins `DIRS`** (the card's smallest change). Rejected
alternative: a separate explicit leg for `scripts/tests`.

Rationale: the isolation property lives in the loop (one pytest process per
entry), not in what kind of dir the entry is, so a DIRS entry and a bespoke leg
give the same isolation — and a bespoke leg would fork the runner's semantics
into two mechanisms for no gain. `scripts` is not a deployed plugin (it is not
in `install_payload.DEFAULT_PAYLOAD`), so the header comment gets one line
saying so; nothing else about the model changes.

Precondition the card asked me to verify — no new bare-import collision. Each
module `scripts/tests` imports under test exists in exactly one place in the
repo, and the leg runs in its own process, so it cannot perturb the other legs:

| module imported bare | sole holder |
|---|---|
| `address_guard` | `scripts/address_guard.py` (importlib from absolute path) |
| `changelog_fragments` | `scripts/changelog_fragments.py` |
| `migrate_changelog_fragments` | `scripts/migrate_changelog_fragments.py` |
| `dep_pr_watcher` | `scripts/dep_pr_watcher.py` |

## Scope discipline (deliberate non-changes)

- `.github/scripts` is **not** added here. That is t_9a4b7687's unmerged change
  to the same `DIRS` line (branch tip `eda8efa9`, still `running`). Touching it
  here would collide on the one shared line for a change a live card already
  owns. Flagged to the orchestrator instead.
- The drift guard in
  `hscc-bootstrap/tests/test_install_payload.py::test_run_tests_sh_covers_every_tested_payload_package`
  stays as-is: it enumerates `install_payload.DEFAULT_PAYLOAD`, and `scripts` is
  not in it, so extending that guard cannot see this dir at all. Extending the
  guard's *scope* to "every `*/tests/` dir" is t_69a1c9f2's explicit judgement
  call, and it depends on t_9a4b7687 (an extended guard fails today because
  `.github/scripts/tests` is unregistered on main). Not duplicated here.

## Evidence

### Gate #1 — base fb15d97b / tip b32954c (superseded when main moved mid-gate)

4 sequential legs, each log stamped commit + `dirty_files: 0` + interpreter
(logs: `profiles/backend-engineer/cache/scratch/t_95d864fc-logs/`). Baseline
worktree clean-detached at fb15d97b. ALL GREEN rc=0 on all four legs; summary
blocks show 9 legs (baseline) vs 10 legs (post-change) with `✓ scripts`. The 9
pre-existing legs were count-for-count identical between baseline and
post-change on each interpreter; the new leg was `scripts` = 56 passed (1.32 s,
both interpreters). This gate is superseded — not discarded — by gate #2 below,
because t_a98c009f + t_5abdb13d landed on main (`4216df50`) while it ran and
repo policy stamps the gate at the SHA that gets merged.

### Gate #2 — base 4216df50 / tip f5e0358f (the stamp for this card)

Baseline: clean detached worktree at `4216df50`, `dirty_files: 0`. Post: this
branch at `f5e0358f` (rebased 1:1 onto 4216df50), `dirty_files: 0`, frozen
while legs ran. Sequential legs, each stamped (logs: `.../t_95d864fc-logs2/`).

| leg | commit | dirty | interpreter | legs | result |
|---|---|---|---|---|---|
| base2-311 | 4216df50 | 0 | 3.11.16 | 9 | ALL GREEN rc=0 |
| base2-313 | 4216df50 | 0 | 3.13.12 | 9 | ALL GREEN rc=0 |
| post2-311 | f5e0358f | 0 | 3.11.16 | 10 | ALL GREEN rc=0 |
| post2-313 | f5e0358f | 0 | 3.13.12 | 10 | ALL GREEN rc=0 |

Per-leg pass/skip counts — the 9 pre-existing legs are identical baseline→post
on each interpreter; only the new leg appears:

- py3.11.16: bootstrap 416 · commands 69 · roles 135 · cluster 479 · project
  1351 · daemon 1259 · sparkrun-hermes 12 · api 876+1skip · memori_byodb 11 ·
  **scripts 97 passed in 3.23 s** (post only)
- py3.13.12: bootstrap 416 · commands 69 · roles 135 · cluster 460+15skip ·
  project 1351 · daemon 1256+3skip · sparkrun-hermes 12 · api 861+16skip ·
  memori_byodb 11 · **scripts 97 passed in 3.00 s** (post only)

The `scripts` leg went 56 → 97 between the gates because t_a98c009f's
`test_ledger_scrub.py` (41 cases) landed on main — i.e. the scrubber suite is
now under the canonical runner too, which was half the point of this card.

Standalone check (no new coupling): `pytest -q scripts/tests` → 97 passed in
2.89 s (3.11) / 2.81 s (3.13). `changelog_fragments.py check` rc=0;
`address_guard.py --tracked` exit 0; `CHANGELOG.md` untouched; every commit on
this branch passed the armed pre-commit hook (`core.hooksPath=.githooks`).

### Final re-stamp at the frozen post-docs tip

The evidence commits change no collected test path (changelog fragment + this
file), so the tree under test is otherwise identical — but policy is policy:
both post legs are re-run on a clean detached worktree at the final tip and
stamped before completion. The results live in the card's completion metadata
and comment (appending them to this file would move the tip again — chicken
and egg), citing this commit as `stamp_for`.

## Reviewer observation carried from t_a98c009f (measured, not acted on)

`ledger_scrub.py` FILE mode replaces the named path via `mkstemp`+`os.replace`,
so on a **symlink** it swaps the link for a regular file and leaves the target
untouched (`sed -i` semantics). Re-measured on this branch: `git ls-files -s`
shows **0 tracked symlinks repo-wide** and `find docs/audits -type l` returns
**0**. The commit-time guard still backstops anything reaching git. No exposure
today; no action, per the card.

## Sequencing note (recorded for the landing step)

Orchestrator ruling (comment on this card, 03:55): this card owns the `scripts`
token on the `DIRS=(...)` line; t_9a4b7687 owns `.github/scripts`;
t_69a1c9f2's remaining scope after this lands is only the drift-guard
widening. Landing order t_9a4b7687 → this card → t_69a1c9f2. Note: pending
main commits since my base (4216df50) are docs-only (GOAL_LEDGER), so this
branch merges cleanly onto current main; if t_9a4b7687 lands first, its DIRS
rewrite touches the same single line — trivial 3-way merge, but worth
expecting.
