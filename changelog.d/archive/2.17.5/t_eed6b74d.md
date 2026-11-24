kind: Fixed
task: t_eed6b74d

* hscc-api: the stop-path tests no longer judge a live child's WHOLE LIFE against a
  25 s stopwatch. `_communicate(timeout=25)` is replaced by a progress-keyed wait —
  the child announces bounded stages (`hscc-api: listening on …`, `hscc-api: ready`,
  its `@@…@@` markers), each line resets a silence clock, and the budgets are load
  budgets that only run out on a child that stopped doing anything. A child that
  died *before* entering the stop window is now reported as `stalled` (it never
  entered the window the case is about), not as a wedged signal handler — the
  mis-attribution that bounced a merge-queue gate for a PR touching neither file.
* hscc-api: those tests no longer reserve a port and hand it back. They pass
  `port_override=0` and read the port the child actually bound off the child's own
  `hscc-api: listening on 127.0.0.1:<port>` line, so the window in which another
  process on a busy host could steal the number is gone (`_free_port` deleted; no
  product change — `create_servers` already adopts the OS-assigned port).
* hscc-api: every child is spawned as its own process group and the file owns it —
  the case's `finally:` and the fixture teardown both `killpg`, so a descendant
  (the real `gateway_bridge` spawns `hermes serve`) cannot outlive the run. Three
  guard arms prove it non-vacuously: the group is really the child's, killing it
  takes a real tagged descendant with it, and the fixture teardown reaps a child a
  case never reaped.
* hscc-api: the child's own readiness backstop is now the parent's constant
  (injected into the harness, pinned by `test_the_two_budgets_agree_about_who_waits_for_whom`),
  ending the 30 s child-side wait that lived inside a 25 s parent-side bound.
