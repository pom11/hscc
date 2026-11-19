# t_23bf61f3 — CI has no pytest leg: what shipped, what is proven, what is not

**Branch:** `wt/t_23bf61f3` (off `origin/main` `540f8a8b`)
**Commits:** `52efb8a4` (the gate), `b9128d31` (making the coverage pin bite)
**PR:** **not opened** — the orchestrator's mid-run directive ("BLOCKED: migrated to
OpenCode") supersedes this card's "open a PR, never merge" instruction. Nothing was
pushed. Everything is committed on the branch, and the branch is 3 commits behind
`origin/main` (`1d156336`), which is fine for a PR (GitHub diffs from the merge base).

## The finding, stated as the card states it

`scripts/run_tests.sh` — the 12-leg roster — appeared in no workflow, so no PR check
could see a red leg. `gh pr checks 443` and `#446` each returned only `compile`
(app-build-checks) and `guard` (address-guard). The observable consequence measured by
t_bcebb042: `hscc-api/tests/test_contract_swift_routes.py` was red on `main` across
`704f1cb8` (#443) → `9fc53cef` (#446) → `1b431c36` → `ec8bd9c2` → `ce8e87f3` — 1h40m,
three PRs merged on top — and stopped being red at `540f8a8b` because the feature under
test was deleted, not because the gates were fixed.

**Not claimed, deliberately:** whether anyone ran `scripts/run_tests.sh` locally before
those merges. Not observable from the repo. What is observable is that a red leg cannot
block a merge, so "the gate ran and passed" and "the gate was never run" are
indistinguishable from outside.

## Decision 1 — leg selection from touched paths, with a full-suite backstop

Full matrix on every PR is not free: the roster is ~25 min on a loaded Spark host
(measured at `540f8a8b`, throwaway detached worktree: hscc-bootstrap alone 285 s,
hscc_daemon 122 s). A gate that turns every unrelated flake into a merge blocker gets
clicked through, which is worse than no gate.

So:

| trigger | legs | why |
|---|---|---|
| `pull_request` | the legs the touched paths can break (`.github/scripts/leg_select.py`) | seconds-to-minutes, not 25 min |
| `push` to `main` | **all 12** | the backstop for a wrong map: a selection bug becomes a visible red on `main` within minutes of merge, not the next "clear the red" card |
| `workflow_dispatch` | operator's choice | "run just hscc-api on this branch" |

Selection needed a real change in the runner, not a flag: **`run_tests.sh` had no leg
selection at all** — extra argv goes to pytest, so `run_tests.sh hscc-api` ran twelve
legs and handed `hscc-api` to pytest as a path argument. `HSCC_TEST_LEGS` is that change
(`scripts/run_tests.sh`, selection block). Unset = whole roster, unchanged; unknown name
= **exit 64 before any leg starts**; the selection is always printed so a redirected log
distinguishes "3 legs, selected" from "12 legs, full suite".

The map **fails closed**, which is the load-bearing property:

- a path no rule claims widens to the FULL suite and names the offender in the log;
- every leg in `DIRS` is covered by a rule for its own directory — read from the runner
  at runtime, not duplicated, so the two cannot drift;
- the harness (`.github/`, `scripts/run_tests.sh`, `scripts/leg_heartbeat.sh`,
  `requirements*`, `install/`, `conftest.py`, `.githooks/`) widens to everything, because
  a broken harness makes every leg green for the wrong reason and a per-leg map is
  structurally blind to that;
- an empty/un-diffable base (new branch, force-push with the old sha gone, bare dispatch)
  means "we do not know what changed" → full suite.

Cross-leg edges are grepped, not imagined (`file:line` in
`.github/scripts/leg_select.py`'s docstring): `hscc_daemon` widens to
`hscc-cluster`/`hscc-project`/`hscc-api`; `scripts/` widens to `hscc_daemon`;
`macos-app/`+`ios-app/` narrow to the `scripts` leg because
`scripts/tests/test_site_landing.py:46,49-50,305-347` is the only Python reader of those
trees (a Swift-only PR pays ~15 s, not the roster).

## Decision 2 — advisory first, one repository variable

`vars.HSCC_TESTS_ENFORCE`, same shape as `address-guard.yml`'s `ADDRESS_GUARD_ENFORCE`:
unset/empty/`false` → a red leg is a `::warning::` and the job stays green;
`true`/`True`/`1`/`yes` → red. Normalised so a capitalised value can never fail OPEN.
`workflow_dispatch.inputs.enforce` forces the blocking posture for one run without
touching repo settings.

**A leg that could not run is red in advisory mode too** — a bad selection (exit 64) and
any non-test-failure exit code (grep of `pytest exit N`, N≠1) are decided *before* the
advisory branch. "Green" from a job that executed zero tests is the exact failure mode
this gate exists to close.

`runs-on: ubuntu-latest`. The suite is designed to run **thin** — every
hermes-integration case skips through a named probe module when hermes is not importable,
and `-rs` prints each skip reason — so a runner with `requirements.txt` and no hermes is
an existing, documented posture. **Known gap, stated:** it does not cover
macOS-specific behaviour (app-build-checks covers the Apple clients on macos-latest); if
a leg ever goes green here and red on a Spark host because of the OS, `runs-on` is a
one-line change.

## Prerequisite status — measured, and the card's list is now one item

Card body said three reds stand on `main`. Re-measured at `540f8a8b` (full 12-leg roster
in a throwaway detached worktree, then standalone re-runs):

| red named in the body | status |
|---|---|
| `hscc-bootstrap/.../test_bootstrap_isolated.py::test_fresh_home_says_not_wired_instead_of_claiming_success` | **still red** (`1 failed` standalone; owner t_ea59a6ba) |
| `scripts/tests/test_site_landing.py::test_page_command_region_matches_the_generated_one` | **passes** — #447 regenerated the region |
| `scripts/tests/test_formula_dep_drift.py::test_formula_resources_match_requirements_sot` | **passes** — the tap's h2/hpack/hyperframe closure landed with #451 |

One extra data point the body does not list: the `scripts` leg was red in my full-suite
run on
`test_site_landing.py::test_live_cli_and_fixture_agree_on_the_command_list`, which shells
out to the *installed* `hscc`. It **skips when `hscc` is not on PATH** — measured under a
stripped PATH: `21 passed, 4 skipped` — so a GitHub runner cannot inherit it. It is a
host-local problem (that test's `hscc --help` exits 1 inside pytest while the same binary
works standalone) and nobody owns it; worth its own card.

Consequence for the born-red risk the card warns about: leg selection *defuses* it. A PR
that does not touch `hscc-bootstrap/` does not run that leg, so the gate is usable on day
one even with the one remaining red unfixed. Advisory mode covers the rest.

## What is proven

- **201 tests** across the two affected legs (`.github/scripts` + `scripts`), run under
  both interpreters the stamp uses: hermes venv 3.14.10 and the thin sibling `p313`
  (3.13.12) — `201 passed` each, no new skips. The thin run is the CI posture.
- **9/9 mutations caught** (`scratch/mutation_check_t_23bf61f3.py`): loop reverted to the
  roster, `CURRENT_SUITE` dropped, unknown-leg `exit 64` removed, the `hscc_daemon`
  cross-edge narrowed, widening disabled, the per-dir rule broken, `pull_request` trigger
  deleted, the could-not-run check moved after advisory, and the enforce normalisation
  dropped. One of those (the per-dir rule) **initially passed** and forced a stronger pin
  — see `b9128d31`.
- The workflow's own steps were rehearsed off-CI: every `run:` block `bash -n`'d, and the
  "decide which legs" body executed against real git for dispatch / new-branch / push /
  PR / vanished-base shapes.
- `python scripts/changelog_fragments.py check` → OK (67 fragment entries consistent).

## What is NOT proven — the card's gate is unmet

**No CI run exists.** The workflow has never executed on GitHub, so
"a deliberately-failing probe commit goes red" is not demonstrated, and the check has
never run successfully — which also means it is not yet *selectable* as a required check
(GitHub only offers checks that ran in the last 7 days). This is the one thing the next
worker must do; a gate that has never gone red is not proven to gate.

## LEFT — exact next steps

1. `git -C <worktree> push -u origin wt/t_23bf61f3`, open a PR against `pom11/hscc`
   `main`, **do not merge**. Branch is 3 commits behind `origin/main`; a plain
   `git merge origin/main` commit is enough (force-push is blocked in these sessions).
2. Prove detection: on the PR head, add a commit that fails one test in a *selected* leg
   (e.g. an assert in `hscc-api/tests/test_contract_swift_routes.py`). The `legs` job must
   run in advisory mode and emit `::warning::a test leg is red` naming the leg in the
   summary — that is the detection proof.
3. Prove gating: `gh workflow run pytest-legs.yml --ref <branch> -f enforce=true` (or set
   the repository variable `HSCC_TESTS_ENFORCE=true` briefly, which needs admin and
   affects every open PR while set — prefer the dispatch input). Report **both run ids and
   conclusions** on the card.
4. Prove the runner posture is green: the first push run on `main` will show which legs
   are red on `ubuntu-latest` for environmental reasons (skips are fine and printed by
   `-rs`; `gh run view <id> --log` on this repo must go through `scripts/ci_log.sh`, which
   redacts — the repo is public). If the bootstrap red or a new environment red shows up,
   that is the moment to fix, not to flip the variable.
5. Only then require the check: `gh api -X PUT repos/pom11/hscc/branches/main/protection/required_status_checks -f strict=true -f contexts[]=legs`.
6. Unowned finding worth a card: `test_live_cli_and_fixture_agree_on_the_command_list`
   fails on this host because the console script `hscc` exits 1 under the pytest
   environment (`from hscc_cli import main` raises) while working standalone.

## Hotspots / notes for the reviewer

- `scripts/run_tests.sh` is a multi-card collision file; the selection block is appended
  after `DIRS` and the only other edit is `for d in "${DIRS[@]}"` → `"${LEGS[@]}"`.
- The mutation harness restoring with `git checkout -- .` silently reverted an uncommitted
  strengthening and reported the pin as decorative. It now restores only the mutated path,
  and asserts the mutation is on disk before trusting a result.
