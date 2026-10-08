# run_tests.sh never ran `scripts/tests` — the fix, and the evidence

Task: t_95d864fc (child of t_a98c009f). Workspace branch: `wt/t_95d864fc`.
Base: `main` @ fb15d97b.

## Status

- [x] Reproduced the gap at main tip
- [ ] Fix applied (mechanism: see "Mechanism" — decision recorded before measuring)
- [ ] Baseline per-leg counts (py3.11.16, main tip, pre-change)
- [ ] Post-change full suite (py3.11.16) — 10 legs
- [ ] Post-change full suite (p313 3.13.12) — 10 legs
- [ ] Changelog fragment

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

Append the measured logs here as they land.
