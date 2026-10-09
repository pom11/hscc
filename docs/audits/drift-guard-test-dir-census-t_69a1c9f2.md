# The DIRS drift guard only saw payload packages — widening it to every `tests/` dir

Task: t_69a1c9f2. Workspace branch: `wt/t_69a1c9f2`. Base: `main` @ `3c20990c` (v2.5.8).

## Status

- [x] Re-measured the card's premise at the base (the DIRS half is already landed — see below)
- [x] Drift guard widened to every tracked `*/tests/` dir (the remaining scope)
- [x] Guard proven failing closed on a throwaway dir, and green on the real tree
- [x] Changelog fragment (`changelog.d/t_69a1c9f2.md`, via `changelog_fragments.py add`; CHANGELOG.md untouched)
- [ ] Full sequential two-interpreter gate at the frozen tip (py3.11.16 + p313 3.13.12) —
      results land in the card's completion metadata + comment, citing this file's commit
      as `stamp_for` (same chicken-and-egg convention as t_95d864fc's "Final re-stamp")

## Premise re-check (measured, not assumed)

The card was written at `4e180dc1`, when `DIRS` held neither `scripts` nor
`.github/scripts`. The orchestrator's 02:45 note said to verify by measurement
rather than trust comment dates. At this base (`3c20990c`, the 2.5.8 release),
`scripts/run_tests.sh` line 40 holds:

    DIRS=(hscc-bootstrap hscc-commands hscc-roles hscc-cluster hscc-project hscc_daemon
          sparkrun-hermes hscc-api memori_byodb scripts .github/scripts)

`scripts` is present (t_95d864fc) and `.github/scripts` is present (t_9a4b7687).
**The "Required change" half of this card is a no-op at this base** — re-adding
`scripts` would duplicate a token on the 3-card hotspot line. The card's unique
remaining value, per the same orch note, is the guard widening. Confirmed by
measurement, matching the orch's branch 1 ("your only remaining work is the
'Consider' section").

## The finding that remains: the guard's scope was drawn as "packages"

`hscc-bootstrap/tests/test_install_payload.py::test_run_tests_sh_covers_every_tested_payload_package`
enumerates `install_payload.DEFAULT_PAYLOAD` and checks each payload dir that
ships `tests/` against DIRS. Correct for its own 2026-09-08 failure mode
(hscc-project shipped but was unregistered) — but it can never see a test dir
that is *not a deployed plugin*. Both known instances of the hole
(`.github/scripts/tests`, `scripts/tests`) are exactly that shape: neither is in
DEFAULT_PAYLOAD, so the guard held a green stamp while 56+ cases went unrun.
Registering each instance fixes a sighting; only a scope widening prevents the
next one.

**Conclusion on the card's judgement call: extend the guard.** (The card asked
for a stated conclusion either way.) Measured support for "extend": the repo
today has 12 tracked test dirs, 2 of them non-package (17%), and both of those
went uncollected — the miss-rate for the non-package class was 2/2.

## Change

### Judgement calls made before writing code

1. **Census source = `git ls-files '*test_*.py'` (the index), not a filesystem
   walk.** An untracked dir is not the repo's promise to test anything; the
   address guard ships the same principle (`--tracked` scope, per t_9a4b7687).
   It also keeps the guard immune to whatever scratch dirs a worker leaves in
   its own worktree, and it matches the card's own evidence recipe.
2. **Reachability test = the runner's actual resolution rule**, `pytest
   "$ROOT/$d/tests"` — a test dir `rel` is reachable iff `rel` ends in `/tests`
   and `rel[:-len("/tests")]` is a DIRS entry. A naive "first path component in
   DIRS" rule would FALSE-FAIL on the legitimate multi-component entry
   `.github/scripts` (whose tests live at `.github/scripts/tests`) — caught by
   measurement before committing, since that entry is on main now. Any shape the
   loop cannot resolve (`foo/test/`, `a/b/tests`) is reported too, with the
   advice to add a leg rather than a token.
3. **Exemptions mirror `pytest.ini norecursedirs`, read back from the file**
   (`.worktrees`, `_archive`, plus `.git`/`__pycache__`/`.venv`/`node_modules`/
   `*.egg-info` hardcoded): a dir the runner never descends into cannot be
   collected by it, so demanding registration would be a false failure. The
   norecursedirs line is parsed rather than duplicated so the two lists cannot
   drift silently; a missing/blank file falls back to the hardcoded set (fails
   closed toward the smaller census = stricter guard).
4. **A vacuity meta-check.** The wide guard compares against a computed census,
   so a broken census (bad glob, skip-list bug, moved pytest.ini) would make it
   pass vacuously — the exact failure mode of the original hole.
   `test_test_dir_census_helper_sees_every_registered_leg` asserts the census
   reproduces the runner's own registered legs (every DIRS entry whose
   `<entry>/tests` holds a `test_*.py` must come back), so a silently-empty
   census is impossible.
5. **Both guards kept.** The payload guard stays (verbatim assertions; it names
   DEFAULT_PAYLOAD drift specifically, and its failure mode is different:
   payload registered but runner missing). The wide guard is additive. Shared
   helpers (`_REPO_ROOT`, `_norecurse_names`, `_test_dirs_on_tree`,
   `_dirs_listed_in_runner`) are module-level; the payload guard's body now
   parses DIRS through the shared helper with its assertions unchanged.

### Evidence

**Census at base `3c20990c`** (`git ls-files '*test_*.py'` grouped; also
reproduced by the guard itself):

| dir | registered as DIRS entry | files |
|---|---|---|
| hscc-api/tests | hscc-api | 40 |
| hscc-bootstrap/tests | hscc-bootstrap | 21 |
| hscc-cluster/tests | hscc-cluster | 22 |
| hscc-commands/tests | hscc-commands | 2 |
| hscc-project/tests | hscc-project | 50 |
| hscc-roles/tests | hscc-roles | 9 |
| hscc_daemon/tests | hscc_daemon | 47 |
| memori_byodb/tests | memori_byodb | 2 |
| scripts/tests | scripts | 4 |
| sparkrun-hermes/tests | sparkrun-hermes | 2 |
| .github/scripts/tests | .github/scripts | 2 |
| _archive/2026-06-10/hscc-provision/tests | **exempt** (norecursedirs `_archive`) | 1 |

Every other tracked `test_*.py` lives under `_archive/**` (excluded by
`norecursedirs`) or is itself inside a `tests/` dir; there are 0 tracked test
files whose immediate parent is a test dir with any other name (e.g. `test/`),
measured via `git ls-files '*/tests/*' | awk -F/ '$(NF-1)!="tests"'` → empty.

**Positive proof (real tree, this branch):**
`pytest -q hscc-bootstrap/tests/test_install_payload.py` → **20 passed in 0.11 s**
(3.11.16) — 18 pre-existing + the new wide guard + the meta-check. The three
DIRS guards collected: `test_run_tests_sh_covers_every_tested_payload_package`,
`test_run_tests_sh_covers_every_tested_dir_not_just_packages`,
`test_test_dir_census_helper_sees_every_registered_leg`.

**Negative proof (throwaway dir; card's acceptance item 3):** created
`throwaway_probe/tests/test_probe.py` (1 trivial case, `git add -N` so it is in
the index), re-ran:

```
FAILED ...::test_run_tests_sh_covers_every_tested_dir_not_just_packages
E  AssertionError: test dir(s) hold test_*.py but are unreachable from
   scripts/run_tests.sh DIRS — the runner would print ALL GREEN while never
   running them: throwaway_probe/tests. Fix: if the dir is <top>/tests, add
   <top> to DIRS in scripts/run_tests.sh; ...
FAILED ...::test_test_dir_census_helper_sees_every_registered_leg   (meta-check also trips)
```

fails closed, names the missing dir, and the meta-check agrees. The probe was
then removed (`git rm --cached`, dir deleted); `git status --short` afterwards
showed only this branch's intended diff, and the guard is green again
(20 passed in 0.09 s).

**Standalone, no new coupling:** `env -u HERMES_DELEGATED_CHILD_CONTEXT pytest -q
scripts/tests` → 97 passed in 3.08 s (py3.11.16) / 97 passed in 2.88 s (p313
3.13.12). (97, not the card's 56: t_a98c009f's 41-case scrubber suite landed
since the card was written — matching t_95d864fc's measurement.)

## Scope discipline (deliberate non-changes)

- `scripts/run_tests.sh` untouched — both DIRS tokens are already on main
  (measured above); touching the hotspot line for a no-op risks the exact race
  this card family was sequencing around.
- `install_payload.DEFAULT_PAYLOAD` untouched; the payload guard's assertions
  untouched.
- Detector pattern-class limits (`100.64.0.0/24` fixture block, `10.x`/`172.16.x`)
  untouched, per the card's "Do not".
- No dir dropped or skipped (the card's other "Do not"): the change only ever
  *adds* coverage.

## Sequencing note for the landing step

This branch touches `hscc-bootstrap/tests/test_install_payload.py`,
`changelog.d/t_69a1c9f2.md`, this file, and nothing else — disjoint from the
`DIRS` line by design. No known sibling card edits `test_install_payload.py`
today (t_95d864fc and t_9a4b7687 both recorded it as out-of-scope and are
merged).
