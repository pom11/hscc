kind: Changed
task: t_0df7fc35

* proxy health: ONE predicate now answers "is this PID ours". `hscc_daemon/health.py`
  carried three spellings of the same question, and they disagreed about the case
  that matters most. (1) `_pid_is_tracked_or_descendant` (t_af7d5486) — a tri-state
  upward `ppid` walk, and `None` for "the walk could not be completed", which the
  reap reads as REFUSE TO KILL. (2) `_proxy_owner_is_tracked_proxy` + its
  `_proc_children` (t_640be7a2) — a DOWNWARD BFS returning `(matched, depth)`, whose
  `_proc_children` returns an EMPTY LIST when `ps` cannot be consulted: an unreadable
  process table answered `(False, None)`, i.e. "not ours", which is the fail-open
  shape on the very path t_af7d5486 exists to close. (3) `_proxy_gateway_identity`'s
  own inline `ppid` loop (t_f4ee3ce6), a third budget (`PROXY_TRACKED_TREE_DEPTH`)
  with a fourth answer. All consumers now consult the tri-state upward walk:
  the repair verifier `_proxy_repair_orphan` asks it once and reads `True` →
  verified, `False` → UNVERIFIED (proven foreign), `None` → UNVERIFIED (not
  evaluable — the log line says which, and neither is green nor a licence to
  signal); `_proxy_gateway_identity` gets its `child` kind from it, where both
  `None` and `False` answer `foreign` = "not adopted, not trusted, and NOT killed"
  (the only signalling path is the reap, which refuses on `None`).
  `_proxy_owner_is_tracked_proxy`, `_proc_children`,
  `PROXY_TRACKED_TREE_MAX_DEPTH` / `HSCC_PROXY_TRACKED_TREE_MAX_DEPTH`,
  `PROXY_TRACKED_TREE_MAX_PIDS` / `HSCC_PROXY_TRACKED_TREE_MAX_PIDS` and
  `PROXY_TRACKED_TREE_DEPTH` are deleted; `PROXY_ANCESTRY_MAX_HOPS` / `max_hops`
  is the single budget. t_640be7a2's depth-carrying log line survives — depth now
  comes from `_pid_ancestry_walk`, the one walk that already computed it, and the
  tri-state return is unchanged (`_pid_is_tracked_or_descendant` is that value with
  the depth dropped, so the two can never disagree). No verdict changed: a foreign
  listener is still reaped, the uvx wrapper/child pair is still left alone, and a
  hollow tracked PID still heals.
---
kind: Verified
task: t_0df7fc35

* The two reap-level cases that are the behaviour-preservation proof keep their
  verdicts with NO assertion changed:
  `test_proxy_repair_pid_identity_t_640be7a2.py::TestRepairVerdictUsesTheTree::test_reap_never_signals_a_child_of_the_tracked_proxy`
  (still `is False`, still `kill_sink == []`) and
  `...::test_reap_still_reaps_a_foreign_litellm_orphan` (still `is True`, still
  signals `FOREIGN` first). Only the fakes moved seams — the fixture no longer
  installs a children table at all, because there is nothing left that reads one.
* The three predicate-level cases that asserted the BFS contract were REWRITTEN
  onto the tri-state with every old assertion re-pinned, not dropped:
  `test_unreadable_process_table_fails_closed` →
  `test_unreadable_process_table_is_not_evaluable` (`is None`, and the walk stops
  at the first unreadable parent — one lookup), with its verdict half landing as
  the new `test_unreadable_table_verifies_nothing` (verified False, healthy False,
  `owner_depth` None, port not reported down, and the log says "could not be read"
  rather than "PROVEN FOREIGN"); `test_walk_stops_at_the_depth_cap` →
  `test_walk_stops_at_the_hop_budget` (not credited + ≤ `max_hops` lookups); and
  `test_walk_stops_at_the_pid_budget` → `test_a_wide_process_table_costs_no_extra_walk`,
  which keeps the guarantee in the only unit an upward walk has — a 114-child
  fan-out costs exactly 1 lookup and does not change the answer either way.
  `(True, d)` became `is True` plus the depth from `_pid_ancestry_walk`, so
  `owner_depth` (0 for the tracked PID, 1 for the incident's wrapper/child pair)
  stays pinned where it was.
* The card's new case: `test_proxy_reattach_no_stop_t_f4ee3ce6.py::TestGatewayIdentity::test_the_child_verdict_never_consults_a_children_table`
  — the uvx wrapper/child pair classifies as `child`, `_proc_ppid` was consulted
  exactly ONCE and nothing else consulted, and the structural half asserts
  `_proc_children` / `_proxy_owner_is_tracked_proxy` are gone so a second answer
  to this question cannot quietly come back. `test_the_walk_is_bounded` moved from
  the deleted `PROXY_TRACKED_TREE_DEPTH` onto `PROXY_ANCESTRY_MAX_HOPS` and now
  pins the lookup count too, so the budget patch cannot be a no-op.
* One process-table seam in the fixtures, not one per direction: the
  `_hermetic_process_table` autouse fence in `hscc_daemon/tests/conftest.py`
  fenced `_proc_children → []`; it now fences `_proc_ppid → None`, i.e. the
  hermetic default is NOT-EVALUABLE — the direction that cannot turn a forgotten
  fake green and cannot turn it into a kill either.
* Suites, separate invocations at this head: guard
  `test_proxy_reap_descendant_guard_t_af7d5486.py` 15 passed,
  `test_proxy_repair_pid_identity_t_640be7a2.py` 23 passed (22 before, +1 the new
  unreadable-verifier case), `test_proxy_reattach_no_stop_t_f4ee3ce6.py` 15 passed
  (14 before, +1 the children-table case), together 53 passed, and
  `test_health.py` 167 passed.
* All fakes: injected `ppid_fn` / stubbed `_proc_ppid`, `_proxy_listener`,
  `os.kill` and `log` — no real `lsof`, no real `ps`, no signal that could reach a
  process. The live proxy's `<CACHE>/sparkrun/proxy/state.yaml` was never opened
  for write; the sparkrun fence reported 0 calls resolved to the live CLI.
