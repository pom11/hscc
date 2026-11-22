kind: Fixed
task: t_2b2a6c3b

- **`hscc-cluster` retired two source cross-checks into silent skips whenever `HOME`
  moved, and the leg still printed green.** The locator was
  `SPARKRUN_SRC = Path(os.environ.get("SPARKRUN_SRC", str(Path.home() / "sparkrun" /
  "src" / "sparkrun")))` — one module-level read, `~` as the default, gating
  `test_hf_cache_root_matches_sparkrun_runtime_base` and
  `test_fraction_key_is_known_so_defaults_stay_warning_free` on a DIRECTORY existence
  test. Measured two-arm at `47e7f0d0` (t_d24a05ea) and re-measured at `96a9f38a`:
  `1166 passed, 3 skipped` with the operator's `HOME` → `1164 passed, 5 skipped` with
  `HOME`+`USERPROFILE` at a fresh empty dir under `/tmp`. The two new `-rs` lines read
  `no sparkrun checkout at /tmp/cihome-…/sparkrun/src/sparkrun` — a path that exists
  on no machine, so a reader cannot tell "this machine has no sparkrun checkout"
  (honest, and true on CI) from "the harness moved my HOME" (this repo's own class: a
  new skip that does not name the reason is a test that quietly stopped running). The
  locator is now a **seam**: `SPARKRUN_SRC` is the documented pin and, when set, is
  the ANSWER — nothing else is consulted, so the skip reason cannot contain a home
  path by construction; without it the candidates are a checkout beside this repo
  (`REPO.parent/sparkrun/src/sparkrun`, derived from `__file__`, which a seal cannot
  move) and then the login-home default, tried in order; and the gate is per FILE
  (`runtimes/base.py`), not per directory, so a checkout that lacks the file says so
  instead of raising `FileNotFoundError`. Resolution moved from import to **call**
  time, matching this leg's doctrine for capability answers (`hermes_gap.py`: "Every
  probe re-checks at CALL time and caches nothing at import"), and
  `SGLANG_SA_FIXTURE` got the same call-time shape. What is deliberately NOT done:
  the cases are not un-gated (a deployed plugin dir cannot `import sparkrun`
  in-process and CI has no checkout — `sparkrun recipe validate` on the installed
  overlay stays authoritative), and the locator is NOT a fifth `hermes_gap.py`
  capability, because that module's guard asserts every capability probes a
  hermes-rooted module and "is a checkout on this disk" is a different question with
  a different fix.
---
kind: Verified
order: 1
task: t_2b2a6c3b

- Before/after, same posture, same file (`pytest hscc-cluster/tests/test_media_recipes.py
  -rs`, one process, canonical interpreter). Operator `HOME`: `86 passed, 2 skipped`
  before and after — the two source cases **run** on a machine that has the checkout,
  so nothing was hidden by this change. Sealed `HOME`+`USERPROFILE` (fresh empty dir
  under `/tmp`): before `73 passed, 4 skipped` with the reason `no sparkrun checkout
  at /tmp/cihome-…/sparkrun/src/sparkrun`; after, the same count with the reason
  naming the missing FILE, both candidate paths and why each is a candidate, and the
  pin (`set SPARKRUN_SRC=<path to sparkrun source, the dir holding
  runtimes/base.py>`). Seal-compatibility proven at the same posture: sealed `HOME` +
  `SPARKRUN_SRC` pointed at the workstation's real `src/sparkrun` checkout → `75
  passed, 2 skipped` — the two cases run with `HOME` moved, which is exactly the
  coverage a roster-wide seal would otherwise have retired.
---
kind: Changed
order: 3
task: t_2b2a6c3b

- **`scripts/run_tests.sh` now seals `HOME` for every leg** (roster-wide, after the
  two lines that derive a value from `HOME` and before the leg loop):
  `HOME`+`USERPROFILE` = a fresh empty `mktemp -d` dir under `/tmp` — never `$TMPDIR`,
  never inside the login home (refused with rc 3, because hermes resolves a home under
  `~/.hermes` back to the live root, t_d39c720e) — and the runner **prints the path it
  sealed** so a roster log proves its own posture. `HSCC_TEST_NO_SEAL=1` restores the
  operator's HOME in one env var. `SPARKRUN_SRC` is **declared** from the machine's own
  `sparkrun` checkout when the operator did not set one — the one path
  `hscc-cluster`'s source cross-checks need, exported instead of the home, so the seal
  does not retire them as skips. Pinned by
  `scripts/tests/test_run_tests_home_seal.py` (10 cases: text + position in the file,
  executed seal, escape hatch, both fail-closed refusals via a `mktemp` shim, `TMPDIR`
  cannot move it, declaration wins/loses correctly). **DOES NOT MERGE YET** — this
  card's two-arm roster at `96a9f38a` found four legs with deltas of their own
  (`hscc_daemon` 1 red + 18 skips, `hscc-bootstrap` 35 skips, `scripts` 3 skips, plus
  the two lanes already known), and a seal that lands before those are closed is a
  seal that gets blamed for someone else's red. See the card for the gate.
---
kind: Verified
order: 4
task: t_2b2a6c3b

- Mutation probes on the seam (skill `mutation-probing`): **7 caught / 0 survived, of
  7**. Copy at an absolute path (`/private/tmp/…/copy-<pid>`), pristine reset +
  byte-compare before and after every probe, anchors asserted `count == 1`, every
  mutant `compile()`d before write, `__pycache__` dropped per run, multi-edit mutants
  composed on a running copy. Provenance gated from inside the run by a plugin written
  INTO the copy asserting that `test_media_recipes`, `test_sparkrun_checkout_seam` and
  `hermes_gap` all resolved under the copy, writing a sentinel the driver deletes
  before and requires after — it earned its keep twice before a single verdict was
  read (plugin not importable outside cwd/PYTHONPATH → `INVALID`; then macOS
  `/tmp`→`/private/tmp` prefix mismatch → `INVALID`), i.e. the driver failed closed
  instead of reporting catches it had not earned. Pristine control GREEN first, so
  every CAUGHT is earned. P1 pin ignored → the pinned-run case; P2 import-time freeze
  restored → the AST pin *and* the pinned-run case; P3 instruction clause deleted →
  the skip-reason case; P4 the missing file dropped from both places the reason spells
  it → the same case; P5 the pin stops short-circuiting →
  `test_an_explicit_pin_short_circuits_the_candidates` + the home-independence
  assertion; P6 candidate order flipped → the last-candidate-moves-with-home case; P7
  locator always skips → the pinned-run case + the first-match case. **P3 survived its
  first draft**: the assertion asked only that the reason contain the token
  `SPARKRUN_SRC`, and the candidate label inside the same reason (`… [SPARKRUN_SRC
  pin]`) already spells it — the pin now demands the pasteable form `SPARKRUN_SRC=`,
  which appears nowhere else. Same lesson t_dd814180 recorded for a reason string that
  quoted its own prose.
