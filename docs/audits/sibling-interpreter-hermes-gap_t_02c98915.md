# The sibling-interpreter stamp is unusable while `p313` lacks hermes' runtime deps

Card: `t_02c98915` · measured at `origin/main` = `3aca67dd` (2026-11-10) ·
host: Razvans-Mac-Studio (macOS), load recorded below with each timed number.

## Amendment (2026-11-18, t_0fd2c149) — this page is the measurement; the
runner's roster is the live authority

Everything below this block is the 2026-11-10 record, unchanged: the 21-test
roster, the before/after table and the option-1-vs-option-2 reasoning, all at
`3aca67dd`. It is a dated measurement with a sha. Drift is expected and is not a
reason to rewrite it — the fix for drift is the pointer you are reading.

Two documents answer two different questions:

| | authoritative for | where |
|---|---|---|
| **live authority** | which capability probes exist right now, which family uses which, and what a green sibling leg means | the `sibling interpreter is THIN on purpose` roster block in `scripts/run_tests.sh` — it sits directly above the loop that runs the legs and states the stamp rule |
| **measurement of record** | why the gates exist at all, why option 2 rather than option 1, and what was measured when that was decided | this page |

When they disagree the runner is right and this page is stale. That
disagreement is a red test now, not a doc nobody re-reads:
`hscc-api/tests/test_hermes_gap_single_posture.py::test_the_live_roster_in_the_runner_names_exactly_the_probes_that_exist`
fails if a probe exists that the roster does not name, or the roster names a
gate no probe module defines.

### Two families were added after this measurement

Neither is in the 21-test roster below. Case counts measured at `d000bf20`
(2026-11-18), thin sibling, `-rs`:

| family | cases | capability | probed by | provenance |
|---|---|---|---|---|
| gateway tail — `test_gateway_backfill.py`, `test_gateway_store_tail.py`, `test_gateway_degraded_tail.py` | 7 + 8 + 3 = **18** | real `hermes_state.SessionDB` | `hermes_gap.require_hermes_state` (all three, since t_0fd2c149) | the files predate this audit (`38e4375b`, `b9553afa`, `596cd7fc`); the first two were folded onto the probe by t_02c98915 (`c08aa9b7`), the third only by t_0fd2c149 |
| hermes profile library — `test_profile_root.py` | **6** | `hermes_cli.profiles` (resolver + writer) — **not** `hermes_state` | `hermes_gap.require_hermes_profile_library` | t_2cc3d482, PR #359, merged as `d000bf20` |

The second is a second *capability*, deliberately a separate probe: an
interpreter that lacks `ruamel.yaml` does not necessarily lack `hermes_cli`, and
skipping one family for the other's reason is a blind spot wearing a green leg.
`hermes_gap.py` holds one function per capability, one posture per function.

### The roster and this page's own table already disagreed

The verification table at the bottom of this page records
`hscc-api | 0 failed, 970 passed, **25 skipped**`. Those 25 are, measured:

| | skips |
|---|---|
| `test_session_resolver.py` — the rostered six (reported at `hermes_gap.py:74`) | 6 |
| gateway tail — backfill 7 + store_tail 8 + degraded_tail 3 | **18** |
| `test_gateway_driver.py` — "captured corpus not present", nothing to do with hermes | 1 |
| **= the 25 the table above records** | **25** |
| + profile library — `test_profile_root.py` (`hermes_gap.py:136`, t_2cc3d482) | +6 |
| **thin-sibling `hscc-api` leg at `d000bf20`** | **31** |

The roster was therefore already short by 18 the day it was written: the gateway
tail skips sat in the table above it and were never listed as a family. The
document written to prevent an unreadable leg went unread itself, which is the
argument for pinning the roster to code rather than trusting a list.

### One posture corrected (t_0fd2c149)

`test_gateway_degraded_tail.py` was the last file in the leg carrying its own
probe — a module-level `try: from hermes_state import SessionDB` /
`except: _HAS_HERMES_STATE = False`, plus a skip reason it wrote for itself. Its
three cases are now gated through `hermes_gap.require_hermes_state()` like their
three siblings. Not for tidiness: the private copy was strictly weaker in
exactly the two ways GAP-D-1 (t_b82ad0fe) is about.

* **It cached the answer at collection.** `hermes_state_import_error()` re-tries
  the import at call time on purpose, and that is not hypothetical: in ONE
  full-leg process on the thin sibling the same probe reports
  `ModuleNotFoundError: No module named 'hermes_state'` for the early gateway
  files and `ModuleNotFoundError: No module named 'ruamel'` for the later
  `test_session_resolver` — so the answer to "can this interpreter run hermes?"
  changes mid-run, because something puts the hermes-agent tree on `sys.path`
  partway through the leg (which one, this card did not establish). A flag
  computed at import keeps answering the question it asked at collection, and it
  can only ever skip — including cases that had become runnable.
* **Its reason dropped the import error.** The stamp rule is read off `-rs`.
  "hermes_state not importable" names a capability; `No module named 'ruamel'`
  names the missing module, which is what tells an operator whether the sibling
  env is thin (correct) or has hermes' deps and is still broken (a finding).

Measured effect at leg scale: nothing moved — same 31 skips, same skip sites, and
the three reasons now carry the import error like the other 28. Under the
canonical interpreter all three cases still RUN (30 passed, 0 skipped across the
four real-DB files). And the posture cannot silently regrow:
`hscc-api/tests/test_hermes_gap_single_posture.py` sweeps the leg for a `def` of
a probe name, for the `try`/`except`-around-a-hermes-import shape, for a private
skip reason, and for a file that imports the real `hermes_state` without the
shared gate.

## TL;DR

`scripts/run_tests.sh` takes any interpreter through `HSCC_TEST_PY`. The
sanctioned 3.13 sibling (`~/miniconda3/envs/p313/bin/python`, 3.13.12,
pytest 9.1.1) is a **thin pytest env**: it carries pytest but not hermes'
runtime deps. 21 tests therefore hard-fail there — on `origin/main`, with no
branch under test — so every dual-interpreter stamp is red until the suite
says which of its own tests cannot run on a thin interpreter.

**Decision: option 2 — make them skip honestly, and document the posture.**
Not option 1 (install hermes' deps into `p313`). Reasons below.

## Measured (reproduced independently in this run)

One pytest process **per dir**, exactly as `scripts/run_tests.sh` does (the
card's constraint: a merged-dir probe gives 18 failures instead of 15 because
cross-file isolation is load-bearing — see
`hscc_daemon/tests/test_hermes_cli_import_isolation.py::test_a_planted_fake_does_not_reach_the_next_file`).

| leg | result under `p313` | other legs |
|---|---|---|
| `hscc-project` | 4 failed, 1458 passed (7.26 s) | green |
| `hscc_daemon` | 11 failed, 3667 passed, 12 skipped (94.83 s) | green |
| `hscc-api` | 6 failed, 970 passed, 19 skipped (247.63 s) | green |

21 failures total — the same set the card reported at PR #288's head and at
`origin/main`, so it is the environment, not a branch.

### The roster (11 + 4 + 6)

`hscc_daemon/tests/`:

| test | why it fails on a thin interpreter |
|---|---|
| `test_autodown.py::TestLoadKanbanDb::test_state_resets_to_ok_on_success` | asserts a resolve succeeds; hermes' lib cannot load |
| `test_hermes_cli_import_isolation.py::test_a_planted_fake_does_not_reach_the_next_file` | nested run asserts the REAL lib resolves after the plants |
| `test_orch_waker.py::test_enumeration_reports_unevaluable_when_hermes_is_unreachable` | part (a) asserts `unevaluable: False`, which needs a loadable lib |
| `test_orch_waker_dead_pane.py::test_backoff_survives_the_stamp_at_the_deployed_entry` | `deployed` fixture: `check_orch_waker()` must report ok |
| `test_orch_waker_state_collision.py` (6 tests: `test_nudge_is_delivered_across_ticks_at_the_deployed_entry`, `test_watermarks_survive_the_stamp`, `test_the_single_document_holds_both_schemas`, `test_audit_trail_lands_on_the_default_path`, `test_stale_counters_are_not_resurrected_by_the_merge`, `test_corrupt_document_does_not_break_the_stamp`) | same `deployed` fixture, same `check_orch_waker()` assert |
| `test_profile_roster_multiplex_t1c545ebb.py::TestRosterGate::test_mirror_matches_real_hermes_predicates` | the REAL `profile_is_standalone` raises inside hermes (`hermes_yaml` → `ruamel`), the injected-branch helper swallows it, so the real roster silently disagrees with the mirror |

`hscc-project/tests/`: `test_mcp_server.py::test_standup_thin_adapter`
(`standup.gather_data` → `kanban.list_board_watermarks` → `_load_kanban_db`),
and `test_session_discovery.py::test_real_sessiondb_read_only_discovers_telegram`,
`::test_real_readonly_connection_is_truly_read_only`,
`::test_real_sessiondb_readonly_with_binding_store` (build a real
`hermes_state.SessionDB`).

`hscc-api/tests/test_session_resolver.py`: `test_resolve_returns_real_session_id`,
`test_resolve_no_session_honest_none`, `test_resolve_general_sentinel_is_honest`,
`test_resolve_uses_registry_persisted_session_as_title`,
`test_endpoint_no_session_honest`, `test_endpoint_known_session_returns_id`
(each builds a real `hermes_state.SessionDB` through the module's one `_db()` helper).

## Why option 2, not option 1

1. **Option 1 is host state, not repo state.** `p313` lives outside the tree.
   A stamp that is only green on a machine where someone `pip install`ed
   `ruamel.yaml` + hermes' transitive deps into a conda env is not a stamp: the
   next reviewer, the next node, and CI each get a different answer, and the
   repo cannot pin or verify the difference. Option 2's fix is reviewable,
   versioned, and identical everywhere.
2. **It makes the sibling env a second copy of hermes' dependency set.** Hermes
   resolves its *own* dependency generation at runtime (`hermes_bootstrap`
   execv's a "selected generation" interpreter — the hazard
   `scripts/run_tests.sh` documents as t_82fa20f5). A hand-maintained 3.13 copy
   of those deps drifts from that generation, and the drift shows up as
   *wrong-green* legs rather than red ones.
3. **The thin env is the point of the sibling leg.** Its job is "does this code
   parse and behave under a different CPython", not "is hermes installed here".
   The tests that need real hermes are already, in this repo's own words,
   "the documented env gap" (see `hscc-api/tests/test_gateway_store_tail.py`:
   *"the p313 interpreter lacks it — same documented env gap as backfill"*).
4. **The suite already does exactly this, in three places.** `hscc-api`'s
   `hs_state` fixture, `test_dispatcher_wedge_benign_holds.py`'s
   `pytest.skip("hermes_cli not importable in this interpreter")`, and
   `test_board_pin.real_root`'s `pytest.skip("real hermes_cli not importable in
   this interpreter: …")`. The 21 are the inconsistent minority; option 1 would
   have made *those* the rule and invalidated the existing skips' premise.
5. **Coverage is not lost on the interpreter that matters.** Under the primary
   interpreter (`~/.hermes/hermes-agent/venv/bin/python`, 3.14.10 on this host,
   hermes' own deps present) all 21 still RUN — the gate only fires when the real hermes
   import genuinely fails, and the verification below pins that.

What option 2 costs is stated plainly: a 3.13 run no longer exercises
hermes-integration code paths. That was never true on a thin env — it only
*looked* like it did, because it failed instead of skipping.

## The posture now written down

`scripts/run_tests.sh` carries the roster + the rule. In short:

- A sibling-interpreter leg is expected to report these as **skips with a
  visible reason** (`-rs` is already in the canonical runner's argv, added by
  GAP-D-1 / t_b82ad0fe for exactly this reason: a skip a reader cannot see is
  a blind spot, not a green leg).
- **A red sibling leg therefore means a regression** — a new failure, or one of
  the gated tests failing for a reason other than "hermes not importable".
- The gate is a *capability* probe, never an interpreter-version test: under any
  interpreter that CAN import hermes, every gated test runs.

## Reproduce

```sh
HSCC_TEST_PY=~/miniconda3/envs/p313/bin/python scripts/run_tests.sh
```

Compare against the primary interpreter:

```sh
scripts/run_tests.sh            # ~/.hermes/hermes-agent/venv/bin/python
```

## Verification of the gates (this branch, rebased on `6d541dab`)

One process per dir, under `p313`:

| leg | before (baseline) | after (gated) |
|---|---|---|
| `hscc-project` | 4 failed, 1458 passed | 0 failed, 1458 passed, 4 skipped |
| `hscc_daemon` | 11 failed, 3667 passed, 12 skipped | 0 failed, 3672 passed, 23 skipped |
| `hscc-api` | 6 failed, 970 passed, 19 skipped | 0 failed, 970 passed, 25 skipped |

All 21 baseline failures are now skips whose reason names the missing Hermes
import, and all three legs are green. Under the primary interpreter every
gated test RUNS: `hscc-project` 1462 passed; `hscc-api` 994 passed (1
pre-existing corpus skip); `hscc_daemon` 3693 passed (1 pre-existing
wake-claim skip) plus one transient `test_api_cli_help_inert` port-tripwire
failure in the full run that passed on an isolated rerun (40 passed).
