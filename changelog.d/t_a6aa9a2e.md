kind: Fixed
task: t_a6aa9a2e

- **`leg hscc-commands` no longer asserts the operator's HOME spelling.**
  `test_restart_one_stops_only_target_recipe` asked
  `"~/r/27b.yaml" in stop_call or "/Users" in stop_call[2]`. `restart_one` expands the
  unit's recipe with `os.path.expanduser` before it hands argv down, so the second
  clause was a macOS escape hatch: the case could only ever be satisfied on a box
  whose `HOME` begins with `/Users`, while the runner's `HOME` is `/home/runner`. It
  had been red on `main` — `1 failed, 77 passed` — across at least four consecutive
  full rosters, hidden by advisory mode (the leg job still reported `success`). The
  case now seals `HOME` to `tmp_path` and asserts the recipe slot equals the expanded
  path the fixture owns, that the raw tilde never reaches argv, and — with a second
  unit declaring a different recipe — that the target is read from the unit rather
  than baked in. Same claim as written, now satisfied by the fixture in any posture
  instead of by the machine.
---
kind: Verified
task: t_a6aa9a2e

- **The CI-shaped posture is HOME's *spelling*, not "an empty dir".** Pointing `HOME`
  at an empty directory under the scratch root reads GREEN on this host and is a false
  control: `/Users/…/home_empty/r/27b.yaml` still contains `/Users`, so the escape
  hatch fires. The posture that reproduces CI verbatim is a `HOME` whose path carries
  no `/Users` at all. At `df7d517e` that arm gave `1 failed, 77 passed` with the same
  assertion text as CI job 137154115489 (run 45004870253); the real-`HOME` arm gave
  `1 passed`.
- **Leg counts as a pair, with the sha they were measured at.** `leg hscc-commands`
  (`HSCC_TEST_LEGS=hscc-commands`, hermes venv py3.14) at `5f4abc29`: `78 passed` with
  the real `HOME` and `78 passed` with `HOME` pointed at a directory whose spelling has
  no `/Users`. Before the fix, at `df7d517e`, the same two arms were `78 passed` and
  `1 failed, 77 passed`. Zero skips, zero new cases — the item count is unchanged at 78.
- **Mutation battery, 4/4 caught, each in BOTH postures** (copy at an absolute path,
  green control first in both postures, pristine reset byte-verified before every probe,
  anchors asserted to occur exactly once, `PYTHONDONTWRITEBYTECODE=1` plus
  `__pycache__` dropped before each run, provenance gated by a `-p` plugin asserting the
  `cmdlib` it imported lives under the copy): expansion removed entirely, stop call fed
  the raw `~/…` instead of the expanded path, target replaced by `--all`, target
  hardcoded to one recipe string — every one reddened the named case by name, never a
  sibling. The `before` arm: the first slice (fixture `HOME`, no second-unit assertion)
  SURVIVED the hardcoded-target mutant in both postures, which is why the second unit is
  in the case.
- **No test was silenced.** No `skip`, no `xfail`, nothing deleted, and the `/Users`
  escape was removed rather than widened to also accept `/home/runner`. `ruff` finding
  count on the touched file is unchanged (11 at `df7d517e`, 11 at `5f4abc29` — zero
  introduced).
