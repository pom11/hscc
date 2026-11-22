kind: Fixed
task: t_d24a05ea

- **The `hscc-roles` leg ran with the operator's `HOME`, so a default-path read of
  an operator file could pass here and be red only on CI.** That is the mechanism
  that hid `t_4b70e8d4` / PR #593 (resolver read `~/.flightdeck/registry.yaml`, the
  workstation has it, the runner does not, and the case asserted a field the live
  file agreed with — advisory-red, so the job stayed green). The resolver was
  fixed there; this closes the posture. `hscc-roles/tests/conftest.py` now seals
  `HOME`/`USERPROFILE` to a fresh empty dir for the whole leg process and drops the
  two pins that outrank a `~` default (`HSCC_REGISTRY`, `HERMES_HOME`), before any
  plugin module is imported. The placement is the point: an autouse fixture — the
  shape this card proposed — runs after collection and so cannot seal the
  import-time class, which is the class that hid the card: `autonomy.py:11`
  (`HSCC_DIR`, which reads no env var at all), `rolelib.py:73` (`HERMES_HOME` →
  `PROFILES_DIR`, a write target) and `generator.py:96,134` (which read
  `~/.hscc/serving.json` at import with no env seam). Measured on this workstation
  unsealed: `main_orch._registry_projects()` → 18 rows, `autonomy.is_on()` → `True`,
  `generator.STRONG_URL` → the operator's serving endpoint, `rolelib.PROFILES_DIR` →
  the operator's live `~/.hermes/profiles`. All four answer the CI way under the
  seal, and `tests/test_home_seal.py` pins each of them. The seal refuses a temp
  root inside the login home: hermes resolves a home under `~/.hermes` back to the
  live root (`t_d39c720e`), and a dispatched worker's `TMPDIR` sits exactly there,
  so a seal taken from `TMPDIR` alone would have sealed nothing.
- **`HSCC_REGISTRY` joined `scripts/run_tests.sh`'s suite-wide scrub list, and the
  alone-sweep harness mirrors it.** The leg conftest cannot reach the leg's own
  child processes and no leg re-pins the registry (unlike the five board pins,
  which every leg conftest re-pins), so the runner is the only place a suite-wide
  scrub of it exists. It is read at call time by `orchestrators._registry_path` and
  `main_orch._registry_projects`, which is why an export outranks the documented
  default even in a process that imported first — #593's mechanism, reachable again
  by any provisioning export. `scripts/tests/test_run_tests_harness_env.py` gained
  claim 5 for it (list + child, with a control arm showing the child would have seen
  it), and `test_alone_sweep_gate.py` is the reason `alone_sweep.sh`'s `-u` list had
  to move in the same commit: it asserts both sides scrub the identical set, and it
  caught that drift on the first run.
---
kind: Verified
task: t_d24a05ea

- **The leg cannot see the difference today, which is why the seal is free.** Six
  arms at `db9a878d` (roster env, one pytest process per leg as `run_tests.sh` runs
  it): real `HOME`, `HOME` at an empty dir, that plus `HERMES_HOME`/`HSCC_REGISTRY`
  popped, real `HOME` with `HERMES_HOME` popped, real `HOME` with `HSCC_REGISTRY`
  exported to the operator's live registry, and the full candidate seal plus the
  `HSCC_*` URL pins popped — `358 passed`, 0 skips in every arm. So no case was
  lying, nothing was weakened, and the seal is prophylaxis with a pinned guard, not
  a coin flip. With the seal and its 9 cases: `367 passed` at this head (358 + 9,
  closing exactly).
- **What the exposure actually is** (same-process probe of the leg's own modules,
  per posture): 17 of 23 resolved values differ between the operator's `HOME` and a
  sealed one — registry default path, 18 registry rows, autonomy flag file,
  serving-derived strong-tier endpoint, reasoning-policy path, `hscc_root()`. Six
  do not: `rolelib.HERMES_HOME`/`PROFILES_DIR` and `native_profiles_dir()`, because
  `HERMES_HOME` is exported to every dispatched worker and outranks the `~/.hermes`
  default — that pair is why `HERMES_HOME` is in the seal and not merely in the
  runner.
- **Mutation battery, 6/6 caught** (copy at an absolute path, green control first
  at `9 passed`, pristine reset + byte-compare before every probe, anchors asserted
  `count == 1`, every mutant `compile()`d before it is written, `__pycache__` dropped
  per run, provenance gated by a `-p` plugin asserting `rolelib`/`generator`/
  `autonomy`/`orchestrators` were imported from the copy and writing a sentinel the
  driver checks). Seal deleted → 8 of the 9 cases red; `USERPROFILE` left unsealed →
  exactly the seal-state case red; the seal moved BELOW the import-time snapshot →
  the 5 import-time cases red while the call-time ones stayed green (this is the
  probe that pays for the placement decision — a fixture-shaped seal would have been
  green here); `TMPDIR` honoured and the containment guard removed → the
  containment case red; `HSCC_REGISTRY` not popped → the seal-state case red with
  the operator's registry exported; `HERMES_HOME` not popped → 3 red. Each seeded
  arm has a positive control under the SAME seed and the pristine conftest: all
  three green, so the reds are the mutants, not the seeding. Two arms are
  CAUGHT-BY-SIBLING and are reported as such, not as named catches: with the seal
  deleted, the pin-absence case stays green because the driver's own environment
  carries no pins (that claim is what P5/P6 seed); and the two registry cases do not
  go red for an ambient `HSCC_REGISTRY` because each `delenv`s it first — their own
  #593 contract, not a gap. Copy restored pristine, probe copies deleted.
- **Nothing in the suite needs `HSCC_REGISTRY` armed, so the runner scrub has no
  blast radius.** Six arms (the three suite files that reference it — both
  bootstrap `main_orch` files and the whole `hscc-roles` leg — each run with a fake
  registry armed and with it scrubbed, one process per leg): all green, `8 passed`,
  `4 passed`, `367 passed` on both sides. A leg that reddens under the scrub would
  have been reading the operator's registry, which is the finding the runner's own
  consequence rule names. `2 failed` on my first attempt at the pin test was me
  inventing helper names, and the alone-sweep gate is the second existing guard that
  caught a real omission in this change.
- **The roster-wide seal is a separate decision, and it is not free — measured at
  `47e7f0d0` (current main) in the faithful CI posture.** Six legs, both arms, one
  sha: `hscc-commands` 78/78, `memori_byodb` 31/31, `.github/scripts` 228/228,
  `comfyui-sparkrun` 132/132 — no delta. `hscc-project` `1475 passed` → `1 failed,
  1473 passed` (a registry default read, `UnknownProjectError: unknown project
  'hscc'` — the #593 mechanism in another leg, whose case fix is not on main yet).
  `hscc-cluster` `1166 passed, 3 skipped` → `1164 passed, 5 skipped`: two cases turn
  into skips because the leg locates a sparkrun checkout under `~`
  (`hscc-cluster/tests/test_media_recipes.py:269`), so they run here
  and silently do not run on CI. That is the "quietly stopped running" class, and it
  is the cost side of sealing a roster: a seal without those two lanes fixed trades
  one honest red and two honest skips for silence. `hscc-bootstrap`, `scripts`,
  `hscc-api` and `hscc_daemon` were NOT measured by me at this head (bootstrap alone
  exceeded 13 minutes at 10% on this loaded host) and that gap is carried forward,
  not hidden.
- **A sealed `HOME` must not live under `/Users`, or the seal is a lie for one class
  of case.** My first roster run created the empty homes inside this scratch dir,
  which contains the token `/Users`, and `hscc-commands` stayed green under the seal
  — a case whose assertion is `'~/r/27b.yaml' in argv or '/Users' in argv[2]` (the
  macOS escape hatch `t_a6aa9a2e` removed on main) can only be exposed by a home
  whose path cannot contain that string. Both arms were re-run with the home under
  `/tmp`; the leg-seal helper itself has always preferred `/tmp` and refuses a home
  inside the login home, so the shipped guard was never exposed to this.
