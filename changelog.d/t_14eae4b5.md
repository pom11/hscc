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
  operator's boards, opened them, and the fail-closed enumeration called
  `autodown._note_kanban_unavailable`, whose once-per-process WARN appended a
  line to `$HOME/.hscc/daemon.log`. With HOME sealed by the caller — exactly what
  the door's own read-only suite does — that is a new file inside the sandbox:
  `test_doctor_tool.py::TestReadOnly::test_the_read_writes_nothing_anywhere`
  failed with `a read wrote into the sandbox: ['.hscc/daemon.log']` on every leg
  whose tree contained the row, and the write also meant a suite-spawned door
  could read the operator's live boards and live session registries at all.
  The fence now honours `PYTEST_CURRENT_TEST` too, which pytest exports for the
  duration of each item and every subprocess of that item inherits — the same
  channel `hscc_daemon/main_cli.py`, `hscc-api/repl_inject.py` and
  `flightdeck/core/tmux_chat.py` already ask of a child, not a new marker. An
  operator's own `doctor --json` is unaffected (no marker in its environment, so
  the row still reads and reports); the sealed sandbox and every suite spawn now
  get the refusal the fence always meant to give them. Both halves run as tests:
  the same probe child with the marker must refuse and write nothing, and without
  it must take the ambient path and leave that very log line behind.
