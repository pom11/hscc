kind: Fixed
task: t_e17fc3c5

- **`hscc_daemon`'s api stop/sidecar cases could orphan a whole process
  tree, and reserved an API port they then handed back.** Two gaps in
  `hscc_daemon/tests/test_api_cli_stop_sidecar.py`, both of which let a live
  server survive a red case. (1) `hscc api start` double-forks and
  `_handle_start` calls `os.setsid()`, so the serving grandchild — and the
  real `dummy_sidecar.py` the fake bridge spawns inside it — left the process
  group the test owned; the group-wide SIGKILL a queue-gate timeout performs
  (`merge_queue._kill_gate_group`) never reached them, and a SIGKILL of the
  leg skips every fixture teardown anyway, so the pid-file poll was the only
  cleanup that ever ran. `_reap` compounded it: its `killpg` sat behind
  `if proc.poll() is None`, which is false for the normal `start` arm because
  the CLI parent exits by design. The harness now fences `os.setsid` for the
  `start` verb (both forks, `_detach_stdio`, the pid-file handover and the
  whole `_serve` path still run for real) and `_reap` signals the group
  unconditionally. (2) `_free_port()` bound port 0 to learn a number, CLOSED
  the socket, then told the child to bind it — a window in which any other
  suite on this host could take it. Every child now binds `--port 0` /
  `port_override=0` and the bound port is read back out of the child's own
  `HSCC API listening on 127.0.0.1:<port>` line. Two new cases make both
  fixes non-vacuous (the sidecar is proved alive before anything is killed,
  and the grandchild's pgid is asserted to be the harness's group), and the
  load budgets moved to the sibling leg's measured values — stall 180s ->
  **120s**, life cap 420s -> **360s**, ready 180s -> **120s**, i.e. 7% and
  20% of the queue gate's 1800s instead of 10% and 23%. No product code
  touched.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_api_cli_stop_sidecar.py`: 14 -> **16** cases.
  Mutation probes, each red on exactly its own arm with the intended message:
  the setsid fence made a no-op -> `the serving grandchild (pid 72097) lives
  in process group 72096, not in the harness's group 72095`; `_reap`'s
  `killpg` removed -> `the sidecar (pid 79837) outlived `_reap``. Both
  mutations reverted; `grep MUTATED` clean at the pushed head.
