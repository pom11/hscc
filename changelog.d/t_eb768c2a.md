kind: Fixed
task: t_eb768c2a

* **Every doctor row's in-suite refusal now reaches the door a test spawns.**
  The registry fence in `hscc-bootstrap/doctor_rows.py` and the fifteen fences on
  the doctor row path (the five rows that keep their own function, the fleet
  block's auto-detect, its ssh-transport and role-SOUL gates, and the row modules'
  own checks in `doctor_main_only.py`, `doctor_node_pauses.py`,
  `doctor_node_recovery.py`, `doctor_notifier_mask.py`, `install_gpu_clock_cap.py`)
  all tested `"pytest" in sys.modules`, which is process-local. The agent tool's
  door is a separate interpreter — `doctor_tools.doctor_findings` execs
  `python3 hscc-bootstrap/doctor.py --json`, because the flat sibling imports only
  resolve when the script runs as a script — so in a suite-spawned door none of
  them fired: each row fell through to its own real check. Measured by running the
  door in a child under the marker pytest exports per item (`PYTEST_CURRENT_TEST`,
  which every child inherits) and without it, the two payloads differed in exactly
  one row — `orch_wake_subs`, the one whose module already honoured the marker. The
  rest read anyway: the live service manager, the deployed plugin tree, the profile
  trees, the node-pause overlay, and the `sparkrun` CLI (7 calls) from a test child,
  which is the class `hscc-cluster/tests/sparkrun_fence.py` exists to stop and
  cannot see across a spawn.
* **The predicate is defined once.** `doctor_rows.under_pytest()` is the module-local
  test OR the exported marker, and the registry fence plus every row-level fence call
  it — so a new row that declares `loader`/`refusal`/`fallback_name` is fenced in a
  spawned child by construction, whatever its own module does. Tests that exercise a
  row's human-CLI branch close both channels in the test body (a setup fixture is too
  early: pytest re-exports the marker for the call phase after fixtures run).
* **Guarded by the branch, not the log.**
  `hscc-bootstrap/tests/test_doctor_registry_fence_reaches_children_t_eb768c2a.py`
  runs the door in a child under a planted HOME, fake `sparkrun`/`ssh`/`curl` and
  TEST-NET-1-only state, and reports which branch it took — a call count on each live
  seam (board enumeration, the pause overlay, the profile sweep, the topology read,
  and whether a production ssh transport was produced at all). Marked child: every
  row that reads operator state refuses and reaches none of them, and the fleet block
  reports itself skipped. Unmarked child: the same door reads. Nothing pins
  `.hscc/daemon.log`, whose borrowed WARN #716 muted.
