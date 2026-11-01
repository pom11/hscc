# t_b071701c — GAP-F-8: plugins row must assert deployed tool files

## Finding (verified against live state)
- The "plugins row" named by audit F is `hscc_daemon/verify.py::check_plugins`
  (verify.py:75 @ main). It greps only the installed **hscc-commands**
  `__init__.py` for the three strings `workers-up` / `cluster-restart` /
  `template`, prints `plugins ✓ all core commands found`, and asserts
  NOTHING about hscc-cluster tool deployment.
- Live proof (this box, 2026-11-01): `~/.hermes/plugins/hscc-cluster/` has NO
  `orch_message.py` (ls recorded below) and its `__init__.py` has 6 `_TOOLS`
  tables (28/41/50/59/71/83) — no `_ORCH_TOOLS` at all (2.14.0 payload) —
  while repo main registers 7 tables incl. `_ORCH_TOOLS` -> `orch_message`.
- t_19dbb9de (plugin.yaml provides_tools reconciliation) is still `ready`
  (not landed), so per card the expected set is asserted against the
  **registry** (`hscc-cluster/__init__.py` tool tables) directly, unioned
  with `plugin.yaml provides_tools` when the manifest has one.

## Fix design
- `check_plugins(plugins_dir=None, repo_root=None)` keeps its core-commands
  legs verbatim (skip semantics preserved so existing tests/rows don't
  shift), then adds a **cluster-tools assertion**:
  - expected = tool names registered in the REPO-side
    `hscc-cluster/__init__.py` tables ∪ names in repo `plugin.yaml`
    provides_tools (so when t_19dbb9de lands, the manifest is covered too);
  - actual = tool names registered in the DEPLOYED
    `~/.hermes/plugins/hscc-cluster/__init__.py`;
  - every handler module referenced on either side must exist as `<mod>.py`
    in the deployed dir;
  - missing tools/files are named verbatim in the detail, prefixed with the
    explicit category **"cluster tools missing vs deployed payload"** so a
    KNOWN-pending-release gap (GAP-A-5, closes with 2.15.0) is never
    conflated with a repo-side or payload-copy bug;
  - unassertive cases (no deployed dir, no repo tree, same-file in-place
    layout, unreadable) report `skipped`/`unassertive` and keep the row
    exactly as today — the check can only ADD signal, never fake one.
- Acceptance (real run, pre-2.15.0): `hscc verify` launched from the repo
  tree names **orch_message.py** as missing vs the deployed payload. That is
  the proof the check works; it turns green by itself when 2.15.0 deploys.

## Status log
- [x] Oriented; live drift re-verified (ls of deployed dir, table counts).
- [ ] Implement + fixtures (present/absent).
- [ ] Real-run acceptance output.
- [ ] Suite + guard; PR.
