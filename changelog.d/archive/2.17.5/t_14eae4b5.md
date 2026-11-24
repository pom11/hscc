kind: Fixed
task: t_14eae4b5

- **A doctor row's pytest fence now reaches the door's process, so a suite that
  spawns `doctor.py --json` no longer performs the live read the fence forbids.**
  `orch_wake_subs` (t_639b2834) refuses an uninjected call inside a pytest
  process, but it detected "inside pytest" with `"pytest" in sys.modules`, which
  is process-local. The agent tool's door is a separate interpreter —
  `doctor_tools.doctor_findings` execs `python3 hscc-bootstrap/doctor.py --json`
  because the flat sibling imports only resolve when the script runs as a script
  — so the row rendered in a child that had never imported pytest, the fence
  refused nothing, and the child took the ambient path: it enumerated the
  operator's boards and opened them. Under a sealed HOME the fail-closed
  enumeration then reached `autodown._note_kanban_unavailable`, whose
  once-per-process WARN appended a line to `$HOME/.hscc/daemon.log` — a new file
  inside the sandbox — and that is what made
  `test_doctor_tool.py::TestReadOnly::test_the_read_writes_nothing_anywhere`
  fail on every leg whose tree contained the row.
  t_2c218de8 made the row quiet (the borrowed logger is muted around the one
  call), which restored the read-only invariant for every host, including the
  operator's own `doctor --json`. The read itself survived: a suite-spawned door
  still resolved the operator's boards, and with the WARN muted a file-diffing
  guard can no longer see it. That residual is what this closes.
  The fence now honours `PYTEST_CURRENT_TEST` too, which pytest exports for the
  duration of each item and every subprocess of that item inherits — the same
  channel `hscc_daemon/main_cli.py`, `hscc-api/repl_inject.py` and
  `flightdeck/core/tmux_chat.py` already ask of a child, not a new marker. An
  operator's own `doctor --json` is unaffected (no marker in its environment, so
  the row still reads and reports); the sealed sandbox and every suite spawn now
  get the refusal the fence always meant to give them. Both halves run as tests,
  and both are judged on which branch the row took rather than on what it logged:
  the probe child reports how often it reached `autodown.board_db_paths`, and
  with the marker it must refuse without ever reaching it.
