# t_b071701c — GAP-F-8: plugins row must assert deployed cluster tools

## Finding (verified against live state)
- The "plugins row" named by audit F is `hscc_daemon/verify.py::check_plugins`
  (@main: verify.py:75). It grepped only the installed **hscc-commands**
  `__init__.py` for `workers-up` / `cluster-restart` / `template` and printed
  `plugins ✓ all core commands found`, asserting NOTHING about hscc-cluster
  tool deployment.
- Live proof (this box, 2026-11-01): `~/.hermes/plugins/hscc-cluster/` has no
  `orch_message.py` (ls), its `__init__.py` has 6 `_TOOLS` tables (28/41/50/
  59/71/83) and no `_ORCH_TOOLS`; deployed payload VERSION 2.14.0 — while
  repo main registers 7 tables incl. `_ORCH_TOOLS` -> `orch_message`.
- t_19dbb9de (plugin.yaml provides_tools reconciliation) is still `ready`
  (not landed), so per card the expected set is asserted against the
  **registry** (`__init__.py` `_…_TOOLS` tables + `from . import` handler
  modules) unioned with `plugin.yaml provides_tools` when present — so the
  manifest's current GAP-A-7 under-declaration hole can't hide a tool.

## Fix (hscc_daemon/verify.py, read-only)
- `check_plugins(plugins_dir=None, repo_root=None)` keeps every legacy verdict
  string, then appends a cluster-tools leg (`_cluster_tools_verdict`):
  expected = repo registry tables ∪ repo plugin.yaml provides_tools; files =
  every `from . import <mod>` module on the repo side; actual = deployed
  registry ∪ deployed manifest + deployed `<mod>.py` existence.
- Missing entries named verbatim under the explicit category
  "cluster tools missing vs deployed payload" (+ deployed VERSION) with a
  next_step naming both readings (deploy now vs pending-release GAP-A-5
  closing with the next bootstrap). ok=False only from REAL drift; every
  unassertive case (no deployed dir / no repo tree / in-place same-tree /
  unreadable / no register() / empty expectations) keeps the legacy row shape
  — the leg can only ADD signal, never fabricate pass or fail.
- `repo_root` defaults to verify.py's own tree (check_plugin_payload's
  convention), so `hscc verify` launched from a repo asserts repo->deployed;
  from the deployed tree itself it self-skips (in-place layout = vacuous).

## Acceptance (measured, not asserted)
- BEFORE (this tree with the change stashed, real `hscc verify --json`,
  launched from the repo, live ~/.hermes): `plugins ok=True
  'all core commands found'` — the ✓ GAP-F-8 is about.
- AFTER (same command, same box): row ok=False, detail verbatim:
  `all core commands found; cluster tools missing vs deployed payload:
  orch_message.py, orch_message (deployed payload VERSION 2.14.0) — declared
  by repo .../hscc-cluster (__init__.py registry + plugin.yaml)`
  — names orch_message.py as missing TODAY (pre-2.15.0). That closes itself
  when 2.15.0 bootstrap deploys orch_message.py.
- 13-row table intact; row-by-row before/after diff (both real runs):
  ONLY `plugins` flipped (True->False); api_routes (/v1/logs 400 on live API)
  and plugin_payload (docs dir drift) are byte-identical pre-existing fails.
- Fixtures for present/absent in hscc_daemon/tests/test_verify_cluster_tools.py
  (13 cases; the 3 run_all legs use the existing hermetic override helper).

## Status log
- [x] RED confirmed (13 tests fail pre-fix: TypeError no repo_root seam).
- [x] Implementation GREEN: new file 13 passed; + test_verify.py 104 passed.
- [x] Real-run acceptance (above) + before/after row diff.
- [x] Commit early (2024dcce tests; 96cdc670 fix).
- [x] Full suite scripts/run_tests.sh ALL GREEN (12/12 legs, rc 0);
      address_guard --tracked rc 0; changelog check OK (77 fragments);
      git status --short empty.
- [x] PR #157 opened (base main, MERGEABLE/CLEAN, address-guard CI SUCCESS),
      NOT merged per card.
