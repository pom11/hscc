# t_18318fd9 — autodown status --json flake (live gh probe WARN on stdout)

Status: IN PROGRESS. Workspace: wt/t_18318fd9 @ origin/main 16b04b0f.

## Task restatement

`hscc_daemon/tests/test_kanban_cli.py::TestStatusNamesBlockingTasks::test_status_json_names_blocking_tasks`
fails ~1 in 2 daemon-leg runs with `JSONDecodeError: Expecting ',' delimiter: line 1 column 6`
because a WARN line emitted by `daemon_ops.log` in "foreground mode" (no PID_FILE)
is prefixed to the JSON that the test parses out of capsys stdout.

Root cause chain is already verified in the card body (live `gh` PR/CI probe blip ->
`_PRCI_UNREACHABLE` -> `_note_prci_unavailable()` -> `daemon_ops.log` -> stdout print
because the daemon-leg conftest redirects PID_FILE to a path that never exists).

## Decision (a vs b)

TBD — evidence below.

## Findings so far

(none yet)

## Next steps

1. Read autodown_cli.cmd_autodown, autodown.prci_blocking_signal/_screen_prci/_note_prci_unavailable,
   daemon_ops.log, tests/test_kanban_cli.py, tests/conftest.py.
2. Reproduce deterministically (stub gh to fail) rather than waiting for a live blip.
3. Fix + guard test.
4. `bash scripts/run_tests.sh` green x2 on daemon leg; address_guard --tracked clean.
5. PR only, never merge.
