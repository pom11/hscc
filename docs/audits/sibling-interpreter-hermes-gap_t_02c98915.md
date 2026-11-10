# The sibling-interpreter stamp is unusable while `p313` lacks hermes' runtime deps

Card: `t_02c98915` · measured at `origin/main` = `3aca67dd` (2026-11-10) ·
host: Razvans-Mac-Studio (macOS), load recorded below with each timed number.

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
   interpreter (`~/.hermes/hermes-agent/venv/bin/python`, 3.11, hermes' own
   deps present) all 21 still RUN — the gate only fires when the real hermes
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
