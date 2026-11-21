kind: Added

* **The alone-vs-in-leg sweep is now a gate, not a write-up.** A leg runs as ONE
  pytest process, so a leg can print `ALL GREEN` while a file inside it FAILS — or
  skips for a different reason — when run by itself; when that happens the leg is
  answering a question about test *order* and the dual-interpreter stamp's promise
  ("a FAILED in a sibling leg is a regression") is false. It happened twice in
  `hscc_daemon` and the leg was green both times
  (`docs/audits/daemon-alone-run-order-dependence_t_572b4856.md` §2, §7), which is
  why t_572b4856 shipped the measurement and said plainly that a manual
  131-process loop is the durable protection's absence, not its presence.
  `scripts/alone_sweep_gate.sh` routes that measurement: both stamp interpreters,
  sequentially, one verdict per interpreter under
  `$HSCC_ALONE_SWEEP_OUT` (default `~/.hscc/alone-sweep/`), and exit `0` agreement
  / `1` a file answers alone differently (or its leg is red) / `2` **not measured**
  (no interpreter, broken wiring, another pass holding the lock) — the three-way
  split `.github/workflows/pytest-legs.yml` insists on, so a pass that measured
  nothing is never reported green. It contains **no pytest invocation of its own**:
  the argv, the `SUITE_ENV_UNSET` scrub and the enumeration stay single-sourced in
  the audit's `alone_sweep.sh`, because a second implementation is how a gate
  quietly starts measuring something adjacent.
  `scripts/install_alone_sweep_gate.sh` registers it as a daily Hermes cron job
  (`hscc-alone-sweep-gate`, `30 4 * * *`, `--no-agent`); the installed job is a
  generated one-line wrapper that bakes the repo path and `exec`s the gate from
  git, so the nightly measures committed code and cannot go stale the way a copied
  script does.
* **The routing decision, with the wall time measured rather than assumed.** Not
  per-PR: the pair takes **10m23s** on this host (leg 163 s + 131 alone runs
  166 s on the canonical venv; leg 135 s + sweep on the thin sibling — i.e. ~4x
  the leg itself), `hscc_daemon` is a macOS-only leg (CI's smallest runner pool at
  ~10x the minute cost), and 131 processes are 131 chances for an environment
  hiccup to become a merge blocker over a property that only moves when the daemon
  or the harness moves. A subset guard inside the `scripts` leg was rejected for a
  sharper reason: that leg runs on ubuntu, where the daemon leg cannot even be
  collected — so the guard would `skip()` and report green while measuring nothing,
  which is the exact hole this closes. The property has also never been swept on
  the CI runner's thin venv, and a blocking gate is worth only what its baseline is.

---
kind: Verified

* Baseline, on the shipped gate, both interpreters, tree `3b6437c7`:
  `GATE GREEN` / rc 0 — canonical venv (3.14.10) leg `4049 passed, 1 skipped` +
  131/131 alone sweeps rc 0, **0 files differ**; thin sibling (3.13.12) leg
  `4026 passed, 24 skipped` + 131/131 rc 0, **0 files differ**.
* **Non-vacuity measured, not asserted.** One-file mutation
  (`hscc_daemon/tests/test_watchdog_gateway_only_breaker_b0811ce0.py` gains a
  module-level `import hscc` — the §2 shape, and it resolves in-leg only because
  `test_api_launchd.py:11` leaves `<repo>/hscc_daemon` on `sys.path`): in one
  process after the planter `52 passed` rc 0, alone `ModuleNotFoundError: No
  module named 'hscc'` collection error rc 2 — **on both interpreters** — and the
  gate went `GATE RED` (exit 1) naming exactly that file on both, while both legs
  stayed green (`4049 passed, 1 skipped` / `4026 passed, 24 skipped`). Reverted; the
  gate is green again on the reverted tree. The 20-second probe pair
  (`pytest <planter> <mutant>` → `52 passed`; `pytest <mutant>` alone → rc 2) is the
  cheap re-check that needs no sweep.
* `scripts/tests/test_alone_sweep_gate.py` — 20 cases, **1.4 s**, no pytest over
  the checkout: the diff tool's teeth re-measured on fixtures (a status change, a
  collection error, a file the leg never ran, and the skip-reason change whose
  *tally matches* — the §2 shape, invisible to junit) plus a green fixture, because
  a tool that fails for everything also "catches" everything; the alone side pinned
  to the runner's argv and its `SUITE_ENV_UNSET` list (drift there turns every
  later comparison into a comparison of two different environments); the
  enumeration pinned to the dir-wide glob AND to the real file count on disk, so a
  nightly cannot quietly shrink; the gate pinned to fail closed on all three
  not-measured paths and to leave no lock behind; the installer pinned to the
  `$HERMES_HOME/scripts` binding rule (t_3aadd2c0's 17-consecutive-failures
  incident) and refused before it registers anything on a tree it cannot measure.
* `scripts/readme_lint.py --strict` clean (23 files, 597 commands, docs-index
  219/219, 0 findings).

---
kind: Docs

* The audit gains **§9 — the sweep is now a gate**: the routing decision with the
  measured cost, the two rejected shapes and why, what the CI lane keeps, and the
  decision on the two `sys.path.insert` sites — `test_api_launchd.py:11` and
  `test_hermes_python.py:16` **stay** (each is what lets its own file import
  `api_launchd` / `hermes_python` bare, and moving the insert into `conftest.py`
  would leak it to every file in the process instead of only the ones collected
  after). What stops a new file starting to depend on that residue is the nightly
  sweep: **the gate is the guard**, recorded rather than implied.
* `scripts/README.md` documents the job, the three exit codes and the cost
  reasoning next to the other scheduled watchdogs.
