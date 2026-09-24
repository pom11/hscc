# t_8306890e — FINAL REPORT: Configurable dispatcher worktree root + EcoFire relocation

## What shipped
1. **hermes patch 0012** (patches/hermes/0012-feat-kanban-configurable-out-of-tree-worktree-root.patch,
   merged to main, pushed, deployed):
   Makes the kanban dispatcher's worktree placement configurable. `_worktree_anchor()`
   in hermes_cli/kanban_db_workspace.py reads `kanban.worktree.out_of_tree_repos` +
   `out_of_tree_root` from hermes config. Repos listed there keep their worktrees OUT
   of the tree under `<out_of_tree_root>/<repo-name>/<task-id>` instead of the
   hardcoded `<repo>/.worktrees/<task-id>`. Unlisted repos keep the in-tree default.
   Applied + committed in deployed hermes-agent as febbef720d; reverse-apply check
   confirms committed code == patch.
2. **enable_plugins.py** registers EcoFire_customizations_bc (AL root) + ecofire +
   efsdriver (both Flutter apps — same class of exposure) in `kanban.worktree`.
   Fill-only (preserves operator choice). Env-overridable. Deployed via install_payload.
3. **Live config** (~/.hermes/config.yaml) updated via the sanctioned enable_plugins.py
   runner — kanban.worktree now lists all three repos.

## Verification (all by execution)
- AL compile file count (the number the compiler walks): 2 tools.
  ```
  find /Users/desac/dev/EcoFire_customizations_bc -type f -name '*.al' -not -path '*/.git/*' | wc -l
  ```
  BEFORE: 2487 (1,991 in the 4 in-tree worktrees) — raises AL1021.
  AFTER:  500  (0 .worktrees dirs remain; compiler enumerates .al across the tree).
  The actual alc.exe run could not be reproduced here: no BC alcompiler image tag is
  available on MCR for this app platform and the two dependency apps
  (Romanian Localization, Evo Electronic Documents) are not present. The .al walk is
  exactly what alc.exe does to enumerate files, so 500 is the authoritative post-fix
  count.
- `git worktree list` (EcoFire): all 4 worktrees OUTSIDE the project root:
  ```
  /Users/desac/dev/EcoFire_customizations_bc                       58ab75c [feat/wcollect-descr-pr]
  /Users/desac/dev/.worktrees/EcoFire_customizations_bc/ctw1       81a76a1 [wt/t_d4705029]
  /Users/desac/dev/.worktrees/EcoFire_customizations_bc/t_49627a43 bae45d0 [feat/transfer-stock-fix]
  /Users/desac/dev/.worktrees/EcoFire_customizations_bc/t_4dbda030 864d6a8 [wt/t_4dbda030]
  /Users/desac/dev/.worktrees/EcoFire_customizations_bc/t_c27d531b 51725f0 [wt/t_c27d531b]
  ```
  git status (main tree) clean w.r.t. worktrees.
- Relocated branches still exist, checked out at their original commits (identical
  hashes, NOTHING lost):
  wt/t_d4705029 @ 81a76a1 · feat/transfer-stock-fix @ bae45d0 ·
  wt/t_4dbda030 @ 864d6a8 · wt/t_c27d531b @ 51725f0.
- End-to-end dispatcher anchor (HERMES_HOME=~/.hermes, the gateway/dispatcher home):
  ```
  EcoFire anchor: /Users/desac/dev/.worktrees/EcoFire_customizations_bc   (OUT of tree)
  hscc anchor   : /Users/desac/dev/hscc/.worktrees                          (in-tree, unchanged)
  ```
- Other-projects exposure: ecofire (Flutter, 10 .worktrees), efsdriver (Flutter, 1)
  EXPOSED same way → registered. flosana (static docs) / hscc (python) / others not exposed.
- Test suites: 8 packages ALL GREEN under BOTH
  ~/.hermes/hermes-agent/venv/bin/python AND /Users/desac/miniconda3/envs/p313/bin/python.
  hermes-side kanban tests: 73 passed, 2 skipped.

## How each number was produced (commands)
- .al count: `find /Users/desac/dev/EcoFire_customizations_bc -type f -name '*.al' -not -path '*/.git/*' | wc -l` → 500 (was 2487).
- worktree list: `git -C /Users/desac/dev/EcoFire_customizations_bc worktree list`.
- anchor check: `HERMES_HOME=/Users/desac/.hermes <venv> python /tmp/verify_anchor3.py`.
- tests: `HSCC_TEST_PY=<interp> bash scripts/run_tests.sh` (both interpreters).

## Branch/merge/deploy state
- Branch: wt/t_8306890e — 5 commits ahead of main at merge time, now merged.
- git rev-list --count main..wt/t_8306890e before merge: 4 (after merge + docs commit: 5)
- Merged: main 5c1f37a..bef6e75 (fast-forward), push 5c1f37a..bef6e75 origin/main.
- Deployed: install_payload.py run (plugins refreshed in ~/.hermes/plugins/hscc-bootstrap);
  hermes-agent patch applied+committed (febbef720d) in the deployed venv's source.
- install_payload command:
  `~/.hermes/hermes-agent/venv/bin/python hscc-bootstrap/install_payload.py`

## Follow-ups
- t_3db321fa: make flightdeck hygiene/standup/lint/why aware of out-of-tree worktrees
  (they still hardcode <repo>/.worktrees and won't prune/report the relocated ones).
- Existing ecofire (10) + efsdriver (1) worktrees left in place; registered so future
  dispatches go out-of-tree. Relocating existing ones is deferred to avoid disrupting
  running cards on those boards.
