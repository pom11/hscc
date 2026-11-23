kind: Changed
task: t_b52e70ba

* `hscc-skills install-skills` now installs/updates the cluster-control skills
  (`hscc`, `hscc-cluster`, `hscc-model-onboard`) into the **main orchestrator's
  profile only** — `<root>/profiles/<main_orch.main_name()>`, resolved through
  the shared resolver so no code path hard-codes `general`. The ruling
  (R4, 2026-11-23) has no "concept"/read-path carve-out for non-main homes.
* Every existing copy of those three skills in the root home or any other
  profile is **moved** into that home's `skills-backups/removed-<stamp>/`, a
  sibling of the scanned `skills/` tree. The move is a same-filesystem rename:
  nothing is deleted, no `shutil.rmtree` is involved, and a colliding backup
  name gets a counter suffix instead of overwriting the earlier snapshot.
  `/tmp`-style unrelated homes are not scanned; only the Hermes root and its
  `profiles/*` children are.
* Shared/work skills are unchanged: the generic set, `sdlc-review` and
  `devops` still install at the caller's `HERMES_HOME/skills`, because the
  review lane and worker authoring depend on them and they grant no cluster
  control. Profile-authored skills are never touched.
* `status` now resolves the main-only skills against the main profile (so it
  cannot report "not installed" for a tree that moved there), and `install`
  (full bootstrap) routes through the same destination function as
  `install-skills` so the two verbs cannot drift. An unreadable/tampered
  main-orchestrator name fails closed before any write.
---
kind: Verified
task: t_b52e70ba

* New `hscc-bootstrap/tests/test_skill_install_main_only.py` (7 cases) drives
  the real CLI as a subprocess with throwaway `HERMES_HOME` / `HSCC_DIR`: the
  three skills land under the resolved (non-default) main name, the root home
  and two fake profiles keep zero copies, every moved tree is byte-recoverable
  under `skills-backups/removed-<stamp>/`, a profile-authored skill and a
  same-named non-skill reference dir survive, a category-depth copy is moved,
  the sweep is idempotent, a non-main `HERMES_HOME` is cleaned, `status` reads
  the main profile, a tampered name fails closed, and the full `install` verb
  follows the same route.
* Revised `hscc-bootstrap/tests/test_skill_install_update.py` (13 cases) to run
  with `HERMES_HOME` as the main profile home plus sandboxed `HSCC_DIR`; the
  backup-first update, collision-guard, retention and fail-closed pins are
  unchanged and green.
