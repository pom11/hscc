# t_23bf61f3 — the pytest roster now runs in CI

Final state of the card that closed the "no server-side pytest gate" finding.
Supersedes the wip handoff written before this branch reached GitHub.

## The finding

`scripts/run_tests.sh` — the repo's 12-leg test roster — appeared in no
workflow (`grep -rn run_tests .github/workflows/` matched nothing). `gh pr
checks 443` / `446` returned only `compile` and `guard`, so a red leg could not
block a merge. Measured by t_bcebb042: `hscc-api/tests/test_contract_swift_routes.py`
was red on `main` for 1h40m (`704f1cb8` #443 → `ce8e87f3` #452) with three PRs
merged on top, and went green only when the feature was deleted. Whether anyone
ran the suite locally is not observable from the repo and is not claimed.

## What shipped

`scripts/run_tests.sh` gained `HSCC_TEST_LEGS`: unset = the full roster
unchanged; an unknown name exits 64 before any leg starts; the selection is
always printed. Selection was needed because the runner had none — extra argv
goes to pytest, so `run_tests.sh hscc-api` ran twelve legs and handed `hscc-api`
to pytest as a path.

`.github/workflows/pytest-legs.yml`:

| trigger | legs |
|---|---|
| `pull_request` | the legs the touched paths can break (merge-base diff) |
| `push` to `main` | all 12 (backstop for a wrong map) |
| `workflow_dispatch` | the `legs` input, else all 12 |

`.github/scripts/decide_legs.sh` owns the trigger decision; `leg_select.py` owns
the touched-path map and reads `DIRS` out of the runner (no second roster). The
map fails closed: an unattributable path widens to the full suite and names
itself, and every leg in `DIRS` selects its own directory (pinned).

Each selected leg is a **parallel matrix job** with `fail-fast: false`. Since
`hscc_daemon/event_driven.py:240` annotates `select.kqueue` at class scope, that
leg runs on `macos-latest`; every other leg uses `ubuntu-latest`. The venv is
cached on `requirements.txt` + `requirements-ci.txt` (exact test-only pins:
websockets, mcp, sqlalchemy, aiohttp, numpy, Pillow, setuptools, pytest).

`.github/scripts/leg_verdict.sh` owns one leg's verdict: advisory by default,
`HSCC_TESTS_ENFORCE=true` (or `workflow_dispatch.enforce`) to block. A leg that
could not run — exit 64, `pytest exit N` with N ≠ 1 (including a 143 kill), or a
truncated log — is red in advisory mode too.

## Measured CI

- **Full suite, run `44550168746`** (PR #467, diff touches `.github/` → all 12):
  **success**, 15:01:46Z → 15:07:40Z = **5m54s wall**, cold venv cache.
  Per leg: hscc-api 5m31, hscc_daemon 2m38 (macOS, queued separately), scripts
  2m12, hscc-bootstrap 1m15, hscc-cluster 1m16, comfyui-sparkrun 1m04,
  .github/scripts 0m50, hscc-project 0m45, memori_byodb 0m41, hscc-roles 0m38,
  hscc-commands 0m31, sparkrun-hermes 0m28. The aggregator `legs` check took 3s.
- The two runs that shaped the design, kept as the measurement rather than a
  guess: `44539504053` (ubuntu, original `requirements.txt`) failed four legs at
  collection on undeclared test imports and `hscc_daemon` on `select.kqueue`;
  `44543850104` (all-macOS) was still queueing two legs after ~30 min because
  the macOS pool serialised twelve jobs.
- Probe: the deliberately-failing enforced run is reported in PR #467 and on the
  card. A gate that has never gone red is not proven to gate.

## Local proof

- `.github/scripts` leg: **208 passed**.
- `.github/scripts/tests` + `scripts/tests/test_run_tests_leg_selection.py`:
  **93 passed** in the last combined run.
- Real runner: `HSCC_TEST_LEGS=.github/scripts bash scripts/run_tests.sh` →
  208 passed, `ALL GREEN`; `HSCC_TEST_LEGS=nope` → exit **64** before any leg.
- `python3 scripts/address_guard.py --tracked` → rc 0.
  `python3 scripts/changelog_fragments.py check` → OK.
- `test_decide_legs.py` builds a throwaway repo where `main` moved after the
  branch point and proves the PR diff is three-dot (merge-base); a two-dot diff
  would run a leg the PR never touched.
- `test_leg_verdict.py` executes every advisory/blocking branch, including the
  SIGKILLed leg (143) and a truncated log.

## LEFT (operator)

1. Make the gate blocking: set `HSCC_TESTS_ENFORCE=true` and require
   `pytest-legs / legs` in branch protection. The check has now run green, so it
   is selectable. Advisory remains the shipped default per the card's decision 2.
2. The one prerequisite red this card was handed
   (`test_fresh_home_says_not_wired_instead_of_claiming_success`, owner
   t_ea59a6ba) is green on the runner in all runs above.
3. Unowned, not inherited by a runner: `scripts/tests/test_site_landing.py::test_live_cli_and_fixture_agree_on_the_command_list`
   fails on the Spark host when the installed `hscc` is on PATH, and skips when
   it is not (measured by the previous worker: `21 passed, 4 skipped` under a
   stripped PATH). Worth its own card; it does not affect CI.
