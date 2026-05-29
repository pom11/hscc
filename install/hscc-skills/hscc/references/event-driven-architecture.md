# Event-Driven Architecture for HSCC Daemon

Replaces polling with kqueue + launchd for event-driven state monitoring.

## Problem
The daemon originally used 5 polling timer streams (DGX 5s, gateway 10s, local 30s, heartbeat 60s, NAS 30s). Polling wastes resources and introduces latency.

## Solution

### kqueue (macOS file change notifications)
Python `select.kqueue()` watches directories for file changes. Key class: `KqueueWatcher(directory, callbacks)` with methods `start()`, `stop()`, `add_watch()`. Properties: `is_running`, `using_kqueue`.

### launchd (fixed-interval checks)
For checks needing fixed timing, launchd periodic .plist jobs call daemon check functions. Key class: `LaunchdJobGenerator(hscc_script, python_path)` with `create_dgx_check()`, `create_gateway_check()`, `install_all_periodic()`, `uninstall_all_periodic()`, `is_installed(label)`, `status(label)`.

### EventBridge (routing)
Key class: `EventBridge()` with `register(event_type, callback)` and `fire(event_type, payload)`.

## Graceful Fallback
If kqueue unavailable (non-macOS): detect at init, set `using_kqueue=False`, drop to polling mode.

## Integration
- hscc-daemon: replace 5 polling loops with EventDrivenDaemon class
- hscc-config: config changes trigger kqueue callbacks instead of poll cycles
- hscc-governance: policy changes trigger EventBridge events

## Pitfalls
1. kqueue is macOS-only — check `EVENT_DRIVEN_SUPPORTED` first
2. File system buffering — kqueue may miss rapid writes
3. Thread safety — KqueueWatcher uses threading; callbacks should not modify shared state without locks
4. launchd label collisions — use unique labels with `hscc_` prefix
5. Empty directory false positive — when monitoring HuggingFace model caches, count actual files not just directories (rsync creates empty dirs before populating)
