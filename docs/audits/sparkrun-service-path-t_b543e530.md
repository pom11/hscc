# t_b543e530 — sparkrun resolution under a service-supervised PATH

Task: [infra] Daemon cannot resolve sparkrun after service restart — PATH omits
~/.local/bin; auto-heal relaunch broken + DGX check fails open.

## What was measured live (orchestrator evidence at claim time, 2026-10-08)

- Daemon (launchd) env via `ps eww <pid>`: PATH =
  `/Users/<user>/.hermes/hermes-agent/venv/bin:/opt/homebrew/bin:/Users/<user>/bin:/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin`
  — no `~/.local/bin`.
- `sparkrun` CLI lives at `~/.local/bin/sparkrun` (symlink into
  `~/sparkrun/.venv-sparkrun-py313/bin/sparkrun`).
- 230 `structured sparkrun status unavailable — falling back … (venv_py=None)`
  WARNs since the last restart, none stopping; DGX stream logged ok=True the
  whole window (fail-open); 25 `sparkrun stop … [Errno 2]` failures +
  worker-launch failure bursts earlier the same day.

The plist that produced that PATH is the live
`~/Library/LaunchAgents/com.hermes.hscc_daemon.plist`, which is rendered
verbatim from `hscc_daemon/com.hermes.hscc_daemon.plist.template` by
`launchd-setup.sh` (sed over `__HOME__`/`__PYBIN__`) — the template's PATH
string matched the live plist entry-for-entry. `install.py::_daemon_path_env`
and flightdeck's `daemon_install._path_env` already emitted `~/.local/bin`;
the template did not. That mismatch is why a machine could be correct under
one install path and broken under the other.

## What changed

1. **`hscc_daemon/sparkrun_bin.py` (new)** — the single PATH-independent
   resolver.
   - `sparkrun_cli()`: `HSCC_SPARKRUN_BIN` override → PATH → deterministic
     candidate list (`~/.local/bin/sparkrun`, sparkrun venv bin dirs,
     `/opt/homebrew/bin`, `/usr/local/bin`), first usable (regular, readable,
     executable) file wins, `realpath`-ed, cached (a None is NOT cached — a
     later install is picked up without a restart).
   - `sparkrun_venv_python()`: interpreter from the CLI's shebang. For the
     `/usr/bin/env <prog>` form the CLI's own `bin/` sibling is tried BEFORE
     PATH — the service PATH's `python3` is the Hermes/host python and cannot
     `import sparkrun`.
   - `exec_argv()` / `argv()`: the execution chokepoint. Any argv whose
     argv[0] is exactly `sparkrun` is rewritten to the resolved absolute path
     at exec time. PATH-first resolution means a healthy environment execs the
     SAME file (mechanical rewrite, no drift); a truly absent CLI passes
     through so the failure stays the loud `[Errno 2]`, never a silent no-op.
   - `ensure_on_path()`: prepends the resolved CLI's dir to the process PATH
     once at daemon startup — the belt for the remaining `shell=True` sparkrun
     call sites (an argv rewrite cannot help a shell string).
2. **`hscc_daemon/util.py::run_cmd`** routes every non-shell sparkrun argv
   through `exec_argv` (the braces-around-the-call trick: call sites keep
   reading `run_cmd([...])`). `health.py`'s own sparkrun call sites build via
   `sparkrun_bin.argv(...)` so none carries a bare literal.
3. **`hscc_daemon/health.py`**
   - `_sparkrun_venv_python()` / `_sparkrun_cli()` delegate to the resolver.
   - `_sparkrun_workloads()` returns `(workloads, degraded)` — degraded=True
     only when BOTH status paths are dead. The old code returned `[]` on a
     failed shell-out and `check_dgx` logged `ok=True`: an empty read
     indistinguishable from a healthy empty fleet.
   - `check_dgx` publishes `fleet_visibility: degraded`, `ok=False` and a
     named message when degraded, never excused as "intentional". The
     *returned* value stays the serving verdict (ssh + vLLM) so a
     status-polling outage cannot push the watchdog into restart/breaker
     cycles against a healthy vLLM.
   - Auto-heal relaunch `Popen` argv resolved at exec time.
4. **plist template PATH** now includes `__HOME__/.local/bin` (declared
   environment belt; code fix does not depend on it).
5. **Tests**: `test_sparkrun_resolution.py` (26 cases) reproduces the exact
   sanitized PATH with a fake HOME — resolution, shebang forms, exec_argv,
   util routing, end-to-end check_workers relaunch/stop absoluteness,
   degraded-not-ok, PATH emission. `conftest.py` pins resolution hermetically
   so no test can exec the operator's real CLI.

## Deliberately NOT done

- No plist edit on the live host, no daemon restart (operator territory).
  The fix must work under the *existing* service environment; after the next
  supervised restart the template change also repairs the environment itself.
  The code fix alone already closes the defect under the current broken PATH.
- No sparkrun transport reimplementation: the structured path still runs
  sparkrun's own `api.status` under sparkrun's own venv python via the
  existing `_SPARKRUN_STATUS_SCRIPT`.

## Residual (not a regression)

`check_dgx` degrades (ok=False) whenever sparkrun status is unobservable even
though the watchdog return value is unchanged — expect a RED dgx stream during
any sparkrun outage instead of a false green. That is the point; the trigger
pipeline shows it as `state.dgx.degraded`.
