kind: Changed
task: t_5b560adf

- **`hscc-bootstrap`'s real-Hermes-tree locator is a seam, not a `~`-derived
  gate.** Thirty-five cases (24 installer cases in `test_bootstrap_dry_run.py`,
  `test_bootstrap_isolated.py`, `test_bootstrap_main_orch_e2e.py`,
  `test_bootstrap_node_recovery_stage.py`; 11 resolver cross-checks in
  `test_skill_resolver_xval.py`) gated on
  `Path.home()/".hermes"/"hermes-agent"/venv/bin/python` existing — one candidate,
  frozen at import, no way for a machine to declare the checkout it has. Under the
  roster-wide HOME seal that retired all 35 as *skips* while the leg printed green,
  and that subset alone took 230.35 s unsealed against 2.74 s sealed: an ~80x
  faster leg bought by deleting coverage. `tests/hermes_tree.py` is the seam in the
  shape #604 proved for the sparkrun checkout: `HERMES_AGENT_PATH` is the documented
  pin and, when set, the answer (nothing else consulted, so a pinned skip reason
  cannot name a home); unpinned the candidates are a repo-relative sibling checkout
  then the login-home default, in that order; the gate is the interpreter, not the
  directory name; the paths the cases spawn resolve at CALL time. Same variable
  production already honours (`hscc_daemon/hermes_import.py`, `autodown.py`,
  `kanban_live.py`, `hscc-roles/hscc.py`) and the same one `scripts/run_tests.sh`
  declares for the machine's tree before it seals, so no new runner surface is
  needed — and no `SUITE_ENV_UNSET` change, so `alone_sweep.sh`'s `-u` mirror needs
  no move. `HERMES_HOME` is never consulted: on a dispatched worker it names a
  profile dir, and that join is the bug t_8579fba4 / t_417610e6 removed from
  `apply_patches.py` / `ensure_review_feature.py`.
---
kind: Fixed
task: t_5b560adf

- **`doctor`'s preflight crashed the installer on a host with no default cluster.**
  `sparkrun cluster default --json` prints the literal `null` when it resolves no
  `~/.config/sparkrun` (the empty-HOME posture, i.e. every CI runner and every fresh
  host), `json.loads` accepts that, and
  `_sparkrun_default_cluster_matches_serving` then did `data.get("hosts")` on `None` —
  `AttributeError` inside `Prerequisites (doctor)`, so `bootstrap.sh` died before any
  stage ran. The check already refused an *empty* answer and an *unparseable* one; this
  is the third shape a real host produces, and it is now report-only
  ("not a mapping — skipped") like every other branch there. Found because this card's
  seam stopped the four `test_bootstrap_isolated.py` installer cases from skipping under
  the roster-wide HOME seal — with the old `~`-derived gate they were invisible
  everywhere the crash could bite.
---
