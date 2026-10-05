# doctor + HERMES_HOME: is the CHECK wrong, or only the tests?

Task t_267f9d88. Scope: `hscc-bootstrap/` only (no `hscc-api/tests` — that is
t_163fa09f).

## Verdict

**Both are wrong, and the check is the primary defect.** A profile-scoped
`HERMES_HOME` is a *legitimate, ordinary* operator config — Hermes sets it that
way whenever it runs under a named profile, which is how every kanban worker
runs. So `doctor`'s `hermes` check has to locate the install at the root; the
non-hermetic tests are a second, independent defect that merely made the first
one invisible on the operator's machine.

Fixing only the tests would leave `doctor`/`bootstrap` fatal-failing on every
worker host — i.e. the tool would be unusable exactly where it is used most.

## Evidence for the verdict (from execution and from hermes' own source)

1. **hermes treats a profile-scoped home as normal, and provides the root for
   root-level ops.** `~/.hermes/hermes-agent/hermes_constants.py`:
   - `get_hermes_home()` = override → `HERMES_HOME` env → platform default;
     `get_config_path()` is `get_hermes_home()/config.yaml` (L1468-1470). So
     *config* is per-profile — `HERMES_HOME=<root>/profiles/<name>` is intended.
   - `get_default_hermes_root()` (L216+): "Root Hermes dir for profile-level
     ops: `<root>` when `HERMES_HOME=<root>/profiles/<name>`" — it strips the
     `profiles/<name>` tail. Hermes itself distinguishes the two.
2. **The install lives at the root, never in the profile dir.** Verified on this
   host: `~/.hermes/hermes-agent/` exists (a git checkout), and
   `~/.hermes/profiles/backend-engineer/` contains config.yaml, cache, cron,
   plugins... and **no `hermes-agent`**.
3. **HSCC already agreed on this rule elsewhere.** `hscc-roles/rolelib.py::
   _hermes_root` (L11-28) does exactly the "parent dir named `profiles` → root is
   the grandparent" strip, with the comment "Hermes sets HERMES_HOME to the
   PROFILE dir ... So the hermes root ... is NOT generically $HERMES_HOME".
   `doctor.py` was the one place that never learned it.
4. **The failure mode proves tolerating it is required.** Every dispatched kanban
   worker has `HERMES_HOME` = a profile dir; a fatal there makes doctor unusable
   fleet-wide, which is exactly what the full-suite run showed.

## Three env couplings found (the card named one)

All in `hscc-bootstrap/`, all read the *ambient* `HERMES_HOME`:

1. `_hermes_ok()` (doctor.py ~L93) — `os.path.join(hermes_home, "hermes-agent")`
   → false fatal under a profile-scoped home. **The check bug.**
2. `main()` (~L786) — builds `config_path` from ambient `HERMES_HOME` and ignores
   any test-supplied home. Consequence, measured: `main(["--json","--fix"])` in a
   `TestDoctorCLI` case does **not** read the test's `tmp_path/config.yaml` at
   all; it opens `$HERMES_HOME/config.yaml` and `run_doctor_fix` →
   `enable_plugins.enable()` **writes it**. Probe on pristine `origin/main`
   (59788b5) with `HERMES_HOME` pointed at a throwaway profile dir: a 2-key
   config came back fully HSCC-wired (113 lines: plugins, toolsets, kanban,
   delegation, hooks...). So this test can mutate **the operator's live
   config.yaml** — it only looked benign on the operator's machine because that
   file was already wired, so the write was idempotent, and because
   `enable()` also drops `config.yaml.bak-<ts>` + `hooks/cluster-guard.py`.
3. `_nas_ok()` via `run_doctor` — `run_doctor` threads `_cluster_runner` into
   `_sparkrun_cluster_ok` but calls `_nas_ok()` bare, so every `run_doctor()`
   shells out to the real `sparkrun cluster list` against the real cluster
   config under `$HOME`. Slower suite + a real-fleet dependency inside unit
   tests.

## Root cause of the regression

`c45577a` ("fix(bootstrap): helpers honour HERMES_HOME/HSCC_DIR") made
`run_doctor`, `_check_models_served` and `main()` read `HERMES_HOME` (correct —
nine helpers had been hardcoding `~/.hermes` and writing into live state). It
made resolution env-aware **without** making `_hermes_ok` profile-aware and
**without** isolating `TestDoctorCLI`. Before that commit the tests passed
*by accident*: everything ignored `HERMES_HOME` and looked at `~/.hermes`, where
a real install exists.

## Reproduction (re-confirmed on this host, hermes venv py3.11)

```
mkdir -p $SCR/root/profiles/p1            # 2-key config.yaml inside
env -u HSCC_DIR HERMES_HOME=$SCR/root/profiles/p1 \
  python -c 'import doctor; doctor.main(["--json","--fix"])'
# -> EXITCODE 1, fatal_failures ['hermes'],
#    detail "<SCR>/root/profiles/p1/hermes-agent missing"
# -> and that config.yaml was rewritten with the full HSCC wiring
```

## Fix

`doctor.py`
- `hermes_root_from_home(home)` → first candidate carrying `hermes-agent`:
  `<home>`, then grandparent-of-`profiles/<name>`, then `~/.hermes`; `None` when
  none does (genuinely absent). Same rule as `rolelib._hermes_root`.
- `_hermes_ok()` uses it; still **fatal** and honest when absent, and its detail
  names the resolved path (and says when it was resolved out of a profile home)
  so a wrong-but-passing answer can't hide.
- `main()` gained `--home PATH` and now passes one explicit home into
  `run_doctor`/`run_doctor_fix`, so config path and hermes check cannot diverge
  and callers/tests are not at the mercy of ambient env.
- `run_doctor()` threads `_cluster_runner` into `_nas_ok` too (kills the real
  `sparkrun cluster list` subprocess from unit tests).

`tests/test_doctor.py`
- autouse fixture: `HERMES_HOME`/`HSCC_DIR`/`HERMES_SCRIPTS` → tmp, `HOME` → tmp,
  sibling env the resolver reads cleared. No test inherits the ambient home.
- regression: profile-scoped `HERMES_HOME` + install present at root → `ok`
  (both directions asserted: check ok, no fatal).
- regression: `HOME` pointed at an empty tree (no install anywhere) → honest
  fatal, `main()` exits 1. **Not** weakened to always-pass.
- guard: a checksum of the ambient/real config file around a `--fix` CLI run so
  the suite can never again write live operator state and stay green.

## Second defect found while proving the first (worse than the red suite)

`TestDoctorCLI::test_main_json_output` ran `main(["--json","--fix"])`, and
`main()` built `config_path` from **ambient** `HERMES_HOME` — the test's
`tmp_path/config.yaml` was never opened. Measured on pristine `origin/main`
with `HERMES_HOME` at a throwaway profile dir: the 2-key config there came back
**fully HSCC-wired (113 lines)**. Under a dispatched worker that file is the
operator's live profile config; the write looked benign only because the live
config was already wired, which made it idempotent. `enable()` additionally
dropped `config.yaml.bak-<ts>` and — via module-level constants
`HOOKS_DIR`/`CLUSTER_GUARD_DST` captured at import time from ambient
`HERMES_HOME` — copied `cluster-guard.py` (+`.bak`) into the real hooks dir.

The hermetic fixtures cover all three surfaces (env tuple, config path, hook
constants); `test_ambient_home_does_not_leak_into_the_callers_home` and
`test_fix_mode_leaves_other_homes_untouched` pin them by checksum.

## Design note: why the `~/.hermes` fallback is safe

`hermes_root_from_home` tries `<home>` → profiles-grandparent → `~/.hermes`.
The last candidate means a machine whose `HERMES_HOME` points somewhere with no
install can still pass via the default-location install. That is deliberate:
the alternative is the false fatal this card removes. It is not silent — the
check's `detail` names the resolved install and, when it differs, says
`(hermes root resolved from profile home ...)`. A wrong-but-passing answer is
therefore readable in `doctor --json` output.

## Verification (executed, not asserted)

Targeted (both interpreters, `HERMES_HOME=$HOME/.hermes/profiles/<profile>`):
15/15 passed on py3.11 venv and on miniconda p313.
`tests/test_doctor.py` whole file: **72 passed** on both interpreters under the
worker env (69 before this card). Independently reproduced red→green by the
orchestrator: `origin/main 59788b5` → 2 failed vs this branch → 2 passed, p313.

Real-CLI smoke (not just pytest), `doctor.py --json` with `HERMES_HOME` at a
profile dir → `hermes ok=True`, detail
`~/.hermes/hermes-agent (hermes root resolved from profile home ...)`, overall
`ok=True`. Same CLI with `HOME` + `HERMES_HOME` pointed at an empty tree (no
install anywhere) → `hermes ok=False, fatal=True`, process **exit code 1**. The
check is honest in both directions at the real entry point.

Full suite `scripts/run_tests.sh`, every run with `HERMES_HOME` exported to a
profile dir (the regression guard) and `HOME` left real so preflight can find
sparkrun's cluster config — 9/9 packages, **ALL GREEN, 4/4 runs**:

| # | interpreter | result |
|---|-------------|--------|
| 1 | hermes venv py3.11 | ALL GREEN (`full-py311-run1.log`) |
| 2 | hermes venv py3.11 | ALL GREEN (`full-py311-run2.log`) |
| 3 | miniconda p313 (py3.13) | ALL GREEN (`full-p313-run1.log`) |
| 4 | miniconda p313 (py3.13) | ALL GREEN, EXIT=0 (`full-p313-run2.log`) |

Per-package counts (p313 run 1): hscc-bootstrap 342, hscc-commands 69,
hscc-roles 135, hscc-cluster 460 (15 skipped), hscc-project 1351, hscc_daemon
1179 (3 skipped), sparkrun-hermes 12, hscc-api 856 (16 skipped), memori_byodb 9.
Runs 2 and 4 ran concurrently with t_163fa09f's suites on this host; wall time
was longer, results were not.

Note on ordering: runs 1-4 were taken at branch tip `344d023`, which was then
rebased onto `origin/main` for landing. The rebase introduced only
`docs/audits/GOAL_LEDGER_2026-09-26.md` (an orchestrator heartbeat doc); no
Python or test file changed, so the runs remain valid for the merged content.

Live-state check — corrected after an operator filesystem audit (my earlier
"no new files under the real hooks dir" line was wrong as written).

What is true on disk: `~/.hermes/profiles/<worker-profile>/config.yaml` was
rewritten 40 -> 143 lines (HSCC-wired) with a `config.yaml.bak-*` drop at
22:30, and `hooks/cluster-guard.py.bak-*` has accumulated ~325 lifetime drops,
several in tonight's window. The writer signature is exactly the second defect
above: main's *unfixed* `TestDoctorCLI::test_main_json_output` calling
`main(["--json","--fix"])` with ambient worker-env `HERMES_HOME`. Attribution
(operator, corroborated here): the card's own reporting p313 full-suite run and
t_163fa09f's repeated full-suite runs all execute main's unfixed test_doctor,
so drops continued after this branch's fixes landed in this worktree. This
card's own early pre-fix runs (before commit `ec89cdf`, which redirects
enable_plugins' import-time hook constants inside `_pin_home`) also contributed
drops in the window; my md5 "unchanged" comparison was snapshotted *after* the
22:30 rewrite, so it proved only that post-23:00 runs changed nothing further,
not that the file was pristine. Stating it as "live config.yaml unchanged" was
overreach.

What the fix guarantees going forward, proven as a negative control (my
`test_doctor.py` run against main's unfixed `doctor.py`, worker env):
**7 failed** — `test_ambient_home_does_not_leak_into_the_callers_home`,
`test_bare_fix_stays_inside_the_home_it_chose`,
`test_main_passes_with_profile_scoped_home`, `test_fix_mode_leaves_other_homes_untouched`
and the four `TestHomeResolution` cases all fail on the unfixed code and pass on
this branch. The checksum-pinned guards catch exactly tonight's leak pattern;
the fixtures redirect HOME, the env tuple, the config path and the hook
constants, so post-merge full-suite runs cannot write that profile again.
Cleanup of the accumulated `.bak` files is deliberately out of scope (operator
is folding it into a follow-up; inert files, `*.bak-*` are not importable).
