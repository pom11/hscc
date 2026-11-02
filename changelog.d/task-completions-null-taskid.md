kind: Fixed
task: task-completions-null-taskid

- **`task_completions.jsonl` / `blocked_tasks.jsonl` stop recording
  unattributable rows, and the test suite stops writing to them.** 797 of 851
  completion rows (and 797 of 894 blocked rows) had `task_id: null`, so the
  fleet check could not join a single one back to a card. Root cause was not a
  hook payload drift — hermes' `kanban_task_completed` kwargs (`task_id`,
  `board`, `assignee`, `run_id`, `summary`, `telemetry_schema_version`) match
  the handler signature, and all 52 real rows in the ledger do carry a task id.
  Two `*_noop_without_context` tests in `hscc-cluster/tests/test_lifecycle_hooks.py`
  called the handlers with no args and no log-path patch, appending two
  all-null rows to the operator's live ledgers on every `scripts/run_tests.sh`
  run (73 rows on 2026-11-01 alone, against 1 real completion —
  `hscc_daemon/stats.py` counts any row with a parseable timestamp, inflating
  `completions.total` by 99% over 7 days). Fix: an autouse
  `_pin_lifecycle_logs_to_tmp` fixture in `hscc-cluster/tests/conftest.py` pins
  `_COMPLETION_LOG`, `_BLOCKED_LOG` and `_TOOL_EVENT_LOG` to `tmp_path` for
  every test in the suite (same doctrine as `_pin_serving_json_to_tmp`, so a
  test can no longer reach a live ledger by forgetting to patch); both handlers
  now normalise identity fields through `workflow._identity`, so a genuinely
  absent `task_id` is recorded as `"unknown"` and never a silent null; the
  auto-unblock scan skips `"unknown"` rows instead of chasing them as card ids.
  New payload-shape tests drive the handler through hermes' own
  `PluginDispatchMixin._invoke_hook_callback` binder, so an upstream kwarg drift
  fails in pytest instead of in the ledger. Verified hermetically: running the
  whole `hscc-cluster` suite against the fixed tree (1059 passed, 3 skipped)
  leaves `~/.hscc/task_completions.jsonl` at exactly the same row count — 857
  before, 857 after — so this suite no longer writes a single row to the
  operator's ledgers. Null rows did still appear during a later full
  `run_tests.sh`, from a SIBLING card's concurrent suite leg still running the
  unfixed code (the deployed `~/.hermes/plugins/hscc-cluster/workflow.py` also
  still assigns the raw `task_id`); that stops when this lands and the runtime
  copy is redeployed.
