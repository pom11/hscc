# Daemon Integration Pattern

How to add a new periodic check to the HSCC daemon. Use this when integrating a standalone cron job or script into the daemon's periodic check loop.

## Steps

1. **Add the check function** to `~/.hermes/plugins/hscc_daemon/hscc.py` as `def check_<name>():` — must return `True` on success, `False` on failure.
   - Always call `write_state("name", result_dict)` at the end to persist results.
   - Wrap in `try/except` and call `write_state("name", {"error": str(e)})` on failure.

2. **Add `import re`** to the daemon imports (near top of file) if regex is used in the check.

3. **Register the stream** in the `STREAMS` dict (top-level):
   ```python
   STREAMS = {
       ...
       "name": 300,  # interval in seconds
   }
   ```

4. **Register in event-driven mode** — add `"name": check_name` to the `check_map` in `_run_event_driven_daemon()`.

5. **Register in event_driven.py** — add `"name": interval` to `PERIODIC_STREAMS` and `"name"` to `STATE_STREAMS` set in `event_driven.py`.

6. **Register for manual invocation** — add `"name": check_name` to the `check_map` in `cmd_check()` so `hscc_daemon check name` works.

7. **Restart the daemon** — `python3 ~/.hermes/plugins/hscc_daemon/hscc.py stop && start`.

## Example: Idle Monitor Integration (2026-05-29)

This session integrated the standalone cron-based idle monitor into the daemon:
- Removed cron job `9508e87f9729` (HSCC Model Idle Monitor, every 5 min)
- Moved `model-idle-monitor.py` logic into `check_idle_monitor()` in daemon
- Added to STREAMS (300s), event-driven check_map, event_driven.py PERIODIC_STREAMS + STATE_STREAMS
- ACC plugin also got `idle-monitor` command for manual dry-run scans
- Results persist to `~/.hscc/state/idle.json`

## Key Notes
- Daemon runs in event-driven mode by default (kqueue + launchd) on macOS
- Periodic checks use launchd `.plist` files, not polling threads
- State files live in `~/.hscc/state/<stream>.json`
- Check logs appear in `~/.hscc/daemon.log`