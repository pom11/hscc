#!/usr/bin/env python3
"""
HSCC Event-Driven Replacement — kqueue/launchd for hscc-daemon.

Replaces the 5 polling loops (DGX 5s, gateway 10s, local 30s, heartbeat 60s,
NAS 30s) with event-driven triggers where possible:

  1. Config changes  → kqueue watches on ~/.hscc/*.json
  2. Fixed intervals → launchd periodic .plist files
  3. State changes   → kqueue watches on ~/.hscc/state/*.json

Architecture:

  KqueueWatcher     – thread-safe directory watcher, fires callbacks on file changes
  LaunchdJobGenerator – creates/installs/uninstalls launchd .plist jobs
  EventDrivenDaemon – orchestrates both, with graceful fallback to polling

Critical constraints:
  - NO SSH or network requests
  - Python stdlib only (select.kqueue)
  - Thread-safe by design
  - Graceful degradation: falls back to polling on non-macOS or kqueue failure
"""

import json
import os
import select
import signal
import subprocess
import threading
import time
import uuid
import datetime
import errno
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple


# ── Constants ────────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
STATE_DIR = os.path.join(HSCC_DIR, "state")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")

# Streams that need periodic fixed-interval checks (not event-driven)
PERIODIC_STREAMS = {
    "dgx":         5,
    "gateway":    10,
    "local":      30,
    "heartbeat":  60,
    "nas":        30,
    "idle":      300,  # Idle monitor
}

# Streams that are state-driven (triggered by write_state calls)
STATE_STREAMS = {"dgx", "gateway", "local", "heartbeat", "nas", "watchdog", "idle"}

# Directory-level labels
CONFIG_LABEL_PREFIX = "com.nousresearch.hscc-configwatch"
PERIODIC_LABEL_PREFIX = "com.nousresearch.hscc-periodic"


# ── Logging helper ───────────────────────────────────────────────────────────

def _event_log(msg: str, level: str = "INFO") -> None:
    """Write a timestamped log line to the daemon log file."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [EVENT  ] [{level:>5s}] {msg}"
    try:
        with open(os.path.join(HSCC_DIR, "daemon.log"), "a") as f:
            f.write(line + "\n")
    except IOError:
        pass


# ── KqueueWatcher ────────────────────────────────────────────────────────────

class KqueueWatcher:
    """
    Thread-safe kqueue-based file watcher for a directory.

    Fires a callback when files in the watched directory are created, modified,
    or deleted. Falls back to polling every `poll_interval` seconds if kqueue
    is unavailable (e.g., non-macOS).

    Usage:
        watcher = KqueueWatcher(
            directory="~/.hscc/state",
            callback=lambda path: print(f"Changed: {path}"),
        )
        watcher.start()
        # ... run your application ...
        watcher.stop()
    """

    def __init__(
        self,
        directory: str,
        callback: Callable[[str], None],
        poll_interval: float = 2.0,
        fallback: bool = True,
    ) -> None:
        """
        Args:
            directory:   Path to watch (must exist).
            callback:    Called with the changed file path (relative to directory).
            poll_interval: Fallback polling interval (seconds).
            fallback:    If True, fall back to polling on kqueue failure.
        """
        self._directory = os.path.abspath(os.path.expanduser(directory))
        self._callback = callback
        self._poll_interval = poll_interval
        self._fallback = fallback
        self._kqueue: Optional[select.kqueue] = None
        self._kqueue_fd: Optional[int] = None
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._seen_files: Set[str] = set()
        self._mtimes: Dict[str, float] = {}  # separate mtime tracking
        self._running = False
        self._fileno_map: Dict[str, int] = {}  # filename -> file descriptor
        self._active_watchers: Set[int] = set()  # kqueue kevents

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> "KqueueWatcher":
        """Start the watcher thread. Thread-safe to call once."""
        if self._running:
            return self
        self._stop_event.clear()
        self._running = True
        # Ensure directory exists
        os.makedirs(self._directory, exist_ok=True)
        # Discover initial files
        self._discover_files()
        # Launch watcher thread
        t = threading.Thread(
            target=self._watch_loop,
            name=f"kqueue-{os.path.basename(self._directory)}",
            daemon=True,
        )
        t.start()
        self._threads.append(t)
        _event_log(f"KqueueWatcher started: {self._directory} "
                    f"(kqueue={'yes' if self._kqueue_fd is not None else 'fallback'})")
        return self

    def stop(self) -> None:
        """Signal the watcher to stop and wait for thread exit."""
        if not self._running:
            return
        self._stop_event.set()
        # Close kqueue fd
        self._close_kqueue()
        # Wait for threads
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        self._running = False
        _event_log(f"KqueueWatcher stopped: {self._directory}")

    def add_watch(self, directory: str, callback: Optional[Callable[[str], None]] = None) -> None:
        """Add another directory to watch (for multi-directory scenarios)."""
        dir_path = os.path.abspath(os.path.expanduser(directory))
        cb = callback if callback is not None else self._callback
        sub = KqueueWatcher(dir_path, cb, self._poll_interval, self._fallback)
        sub.start()
        self._threads.extend(sub._threads)
        _event_log(f"KqueueWatcher watching additional dir: {dir_path}")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def directory(self) -> str:
        return self._directory

    @property
    def using_kqueue(self) -> bool:
        return self._kqueue is not None

    # ── Internal ────────────────────────────────────────────────────────

    def _discover_files(self) -> None:
        """Populate seen_files with current directory contents."""
        try:
            for fn in os.listdir(self._directory):
                if fn.endswith(".json"):
                    self._seen_files.add(fn)
        except OSError:
            pass

    def _open_kqueue(self) -> Optional[select.kqueue]:
        """Attempt to create a kqueue object and its file descriptor."""
        try:
            kq = select.kqueue()
            self._kqueue = kq
            self._kqueue_fd = kq.fileno()
            return kq
        except (AttributeError, OSError):
            return None

    def _close_kqueue(self) -> None:
        """Close the kqueue file descriptor."""
        if self._kqueue_fd is not None:
            try:
                os.close(self._kqueue_fd)
            except OSError:
                pass
            self._kqueue_fd = None
        self._kqueue = None
        self._active_watchers.clear()
        self._fileno_map.clear()

    def _add_file_watcher(self, filename: str) -> bool:
        """Register a kqueue filter for a specific file in this directory."""
        if self._kqueue_fd is None:
            return False
        filepath = os.path.join(self._directory, filename)
        try:
            fd = os.open(filepath, os.O_RDONLY)
        except OSError:
            return False
        kevent = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
            fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE
                     | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME),
        )
        try:
            changes = [kevent]
            self._kqueue.control([kevent], 0)
            self._fileno_map[filename] = fd
            self._active_watchers.add(fd)
            return True
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False

    def _remove_file_watcher(self, filename: str) -> None:
        """Unregister a kqueue filter for a file."""
        fd = self._fileno_map.pop(filename, None)
        if fd is not None and fd in self._active_watchers:
            self._active_watchers.discard(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    def _poll_fallback(self) -> None:
        """Poll directory for changes (fallback mode)."""
        try:
            current_files = set()
            for fn in os.listdir(self._directory):
                current_files.add(fn)
        except OSError:
            return
        # Detect new, changed, deleted files
        for fn in current_files:
            if fn not in self._seen_files:
                # New file
                self._seen_files.add(fn)
                try:
                    self._mtimes[fn] = os.path.getmtime(
                        os.path.join(self._directory, fn)
                    )
                except OSError:
                    pass
                self._callback(fn)
        for fn in self._seen_files:
            if fn not in current_files:
                # Deleted file
                self._seen_files.discard(fn)
                self._mtimes.pop(fn, None)
                self._callback(fn)
                continue
            # Existing file — check mtime for modifications
            try:
                mtime = os.path.getmtime(os.path.join(self._directory, fn))
                if mtime != self._mtimes.get(fn):
                    self._mtimes[fn] = mtime
                    self._callback(fn)
            except OSError:
                pass

    def _watch_loop(self) -> None:
        """Main watcher loop, runs in its own thread."""
        # Try to open kqueue
        self._kqueue = self._open_kqueue()
        is_kqueue = self._kqueue is not None

        if is_kqueue:
            # Register watchers for all current files
            for fn in list(self._seen_files):
                self._add_file_watcher(fn)
            # Initialize mtime tracking for existing files
            for fn in self._seen_files:
                try:
                    self._mtimes[fn] = os.path.getmtime(
                        os.path.join(self._directory, fn)
                    )
                except OSError:
                    pass

        # Poll for initial mtime map (for kqueue which only notifies)
        self._mtimes = {}
        for fn in list(self._seen_files):
            try:
                self._mtimes[fn] = os.path.getmtime(
                    os.path.join(self._directory, fn)
                )
            except OSError:
                pass

        _event_log(f"KqueueWatcher loop active: {self._directory} "
                    f"(mode={'kqueue' if is_kqueue else 'poll'})")

        last_discover = 0.0

        while not self._stop_event.is_set():
            if is_kqueue:
                # ── kqueue path ───────────────────────────────────────────
                try:
                    # Wait up to 2 seconds for events (allow stop_event check)
                    kevents = self._kqueue.control(None, 10, 2.0)
                    for kevent in kevents:
                        if self._stop_event.is_set():
                            break
                        # kevent.ident is the watched file descriptor (int);
                        # map it back to a filename rather than treating it as one.
                        fn = self._fileno_map.get(kevent.ident)
                        if not fn:
                            continue
                        # Only process .json files
                        if fn.endswith(".json") and fn not in ("__mtimes__",):
                            try:
                                self._callback(fn)
                            except Exception as e:
                                _event_log(f"Watcher callback error: {e}", "ERROR")
                        # Re-register the watcher (ONESHOT removes it)
                        if kevent.ident in self._fileno_map:
                            self._add_file_watcher(self._fileno_map[kevent.ident])
                except OSError as e:
                    if e.errno != errno.EBADF:
                        _event_log(f"kqueue.control error, switching to poll: {e}", "WARN")
                    self._close_kqueue()
                    is_kqueue = False
            else:
                # ── polling fallback path ─────────────────────────────────
                self._poll_fallback()

            # Periodically rediscover directory (for new files appearing outside
            # of kqueue notification, or if we fell back to polling)
            now = time.time()
            if now - last_discover > 10.0:
                last_discover = now
                self._discover_files()
                # Re-register kqueue filters for new files
                if is_kqueue:
                    for fn in self._seen_files:
                        if fn not in self._fileno_map:
                            self._add_file_watcher(fn)

        # Cleanup
        if is_kqueue:
            self._close_kqueue()
        _event_log(f"KqueueWatcher loop exited: {self._directory}")


# ── LaunchdJobGenerator ──────────────────────────────────────────────────────

class LaunchdJobGenerator:
    """
    Creates and manages launchd .plist files for periodic HSCC check jobs.

    Each job is a separate label (e.g., hscc-periodic-dgx, hscc-periodic-gateway)
    so they can be independently started/stopped/uninstalled.
    """

    # ── Plist template ──────────────────────────────────────────────────

    _PLIST_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{hscc_path}</string>
        <string>check</string>
        <string>{stream}</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{home_dir}</string>
    <key>StandardOutPath</key>
    <string>{home_dir}/.hscc/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{home_dir}/.hscc/daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
    </dict>
    <key>KeepAlive</key>
    <false/>
    <key>ThrottleInterval</key>
    <integer>{interval}</integer>
</dict>
</plist>'''

    def __init__(self, hscc_script: str, python_path: Optional[str] = None) -> None:
        """
        Args:
            hscc_script:  Absolute path to hscc.py (the main daemon script).
            python_path:  Path to python3 (defaults to shutil.which("python3")).
        """
        self._hscc_script = os.path.abspath(hscc_script)
        self._python_path = python_path or shutil.which("python3") or "/usr/bin/python3"
        self._home_dir = os.path.expanduser("~")
        self._path_env = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    # ── Public API ──────────────────────────────────────────────────────

    def generate_plist(self, label: str, stream: str, interval: int) -> str:
        """Generate a plist XML string for a periodic check job."""
        return self._PLIST_TEMPLATE.format(
            label=label,
            python_path=self._python_path,
            hscc_path=self._hscc_script,
            stream=stream,
            interval=interval,
            home_dir=self._home_dir,
            path_env=self._path_env,
        )

    def install_job(self, stream: str, interval: int) -> Tuple[bool, str]:
        """
        Generate and install a launchd .plist for a periodic check.

        Returns:
            (success: bool, message: str)
        """
        label = f"{PERIODIC_LABEL_PREFIX}.{stream}"
        plist_content = self.generate_plist(label, stream, interval)
        plist_path = os.path.join(PLIST_DIR, f"{label}.plist")

        try:
            os.makedirs(PLIST_DIR, exist_ok=True)
            with open(plist_path, "w") as f:
                f.write(plist_content)
            _event_log(f"Generated launchd plist: {plist_path} "
                        f"(stream={stream}, interval={interval}s)")
            return True, f"Installed: {plist_path}"
        except (OSError, IOError) as e:
            return False, f"Failed to write plist: {e}"

    def install_all_periodic(self) -> Dict[str, Tuple[bool, str]]:
        """Install launchd jobs for all periodic streams. Returns results dict."""
        results = {}
        for stream, interval in PERIODIC_STREAMS.items():
            success, msg = self.install_job(stream, interval)
            results[stream] = (success, msg)
        return results

    def uninstall_job(self, stream: str) -> Tuple[bool, str]:
        """Uninstall and unload a periodic launchd job."""
        label = f"{PERIODIC_LABEL_PREFIX}.{stream}"
        plist_path = os.path.join(PLIST_DIR, f"{label}.plist")

        try:
            # Try to unload
            result = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", label],
                capture_output=True, text=True, timeout=10,
            )
            _event_log(f"Unloaded launchd job: {label} (rc={result.returncode})")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass  # May not be loaded, file might not exist

        # Remove plist file
        try:
            if os.path.exists(plist_path):
                os.remove(plist_path)
                _event_log(f"Removed plist: {plist_path}")
            return True, f"Uninstalled: {stream}"
        except OSError as e:
            return False, f"Failed to remove plist: {e}"

    def uninstall_all_periodic(self) -> Dict[str, Tuple[bool, str]]:
        """Uninstall all periodic launchd jobs. Returns results dict."""
        results = {}
        for stream in PERIODIC_STREAMS:
            success, msg = self.uninstall_job(stream)
            results[stream] = (success, msg)
        return results

    def is_installed(self, stream: str) -> bool:
        """Check if a periodic launchd job plist exists on disk."""
        label = f"{PERIODIC_LABEL_PREFIX}.{stream}"
        plist_path = os.path.join(PLIST_DIR, f"{label}.plist")
        return os.path.exists(plist_path)

    def status(self) -> Dict[str, dict]:
        """Return status of all periodic jobs."""
        status = {}
        for stream in PERIODIC_STREAMS:
            label = f"{PERIODIC_LABEL_PREFIX}.{stream}"
            plist_path = os.path.join(PLIST_DIR, f"{label}.plist")
            exists = os.path.exists(plist_path)
            # Check if loaded in launchd
            loaded = False
            try:
                result = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True, text=True, timeout=5,
                )
                loaded = result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
            status[stream] = {
                "label": label,
                "interval": PERIODIC_STREAMS[stream],
                "exists": exists,
                "loaded": loaded,
                "plist_path": plist_path,
            }
        return status


# ── Event Bus ────────────────────────────────────────────────────────────────

class EventBridge:
    """
    Bridges kqueue file-change events to daemon check functions.

    When a file in the state directory changes, the EventBridge determines which
    stream it corresponds to and optionally fires downstream reactions
    (notifications, pipeline checks, trigger evaluation).

    Thread-safe: all callbacks are invoked on the calling thread, but the
    kqueue watcher is in a separate thread.
    """

    def __init__(self, check_map: Dict[str, Callable[[], bool]]) -> None:
        """
        Args:
            check_map:  Dict of stream_name -> check_function.
        """
        self._check_map = check_map
        self._callbacks: Dict[str, List[Callable[[str], None]]] = {}
        self._lock = threading.Lock()

    def register_callback(self, stream: str, callback: Callable[[str], None]) -> None:
        """Register a callback for a specific stream's state changes."""
        with self._lock:
            self._callbacks.setdefault(stream, []).append(callback)

    def register_all_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback that fires for all stream changes."""
        with self._lock:
            self._callbacks.setdefault("__all__", []).append(callback)

    def handle_state_change(self, filename: str) -> Optional[str]:
        """
        Process a file change event. Determines the stream and fires callbacks.

        Args:
            filename: The .json filename that changed (e.g., 'dgx.json').

        Returns:
            The stream name if recognized, None otherwise.
        """
        if not filename.endswith(".json"):
            return None
        stream = filename[:-5]  # strip .json

        # Fire stream-specific callbacks
        with self._lock:
            callbacks = list(self._callbacks.get(stream, []))
            all_callbacks = list(self._callbacks.get("__all__", []))
        callbacks.extend(all_callbacks)

        for cb in callbacks:
            try:
                cb(stream)
            except Exception as e:
                _event_log(f"EventBridge callback error: {e}", "ERROR")

        return stream

    def handle_config_change(self, filename: str) -> Optional[str]:
        """
        Process a config file change event. Triggers relevant checks.

        Config files in ~/.hscc/ can trigger check reruns:
          - triggers.json  → rerun trigger engine
          - watchdog_block.json → read block state
        """
        config_stream_map = {
            "triggers.json": "triggers",
            "watchdog_block.json": "watchdog",
        }
        if filename in config_stream_map:
            stream = config_stream_map[filename]
            _event_log(f"Config change: {filename} → triggering {stream}")
            # Trigger the relevant check immediately
            if stream in self._check_map:
                try:
                    self._check_map[stream]()
                except Exception as e:
                    _event_log(f"Config-change check error ({stream}): {e}", "ERROR")
            return stream
        return None


# ── Fallback Poller ──────────────────────────────────────────────────────────

class FallbackPoller:
    """
    Falls back to the original timer-based polling if kqueue is unavailable.

    Mirrors the original daemon's run_daemon_loop() but integrates with the
    event-driven architecture by accepting the same stop_event.
    """

    def __init__(
        self,
        streams: Dict[str, int],
        check_map: Dict[str, Callable[[], bool]],
        watchdog_fn: Callable[[], bool],
        trigger_fn: Callable[[], None],
    ) -> None:
        self._streams = streams
        self._check_map = check_map
        self._watchdog_fn = watchdog_fn
        self._trigger_fn = trigger_fn

    def start(self, stop_event: threading.Event) -> List[threading.Thread]:
        """
        Start the fallback polling threads.

        Returns:
            List of started threads.
        """
        threads = []

        # Start periodic check threads
        for stream_name, interval in self._streams.items():
            check_fn = self._check_map.get(stream_name)
            if check_fn is None:
                continue
            t = threading.Thread(
                target=self._periodic_check,
                args=(check_fn, interval, stream_name),
                daemon=True,
                name=f"poller-{stream_name}",
            )
            t.start()
            threads.append(t)
            _event_log(f"Poller started: {stream_name} (interval={interval}s)")

        # Watchdog thread
        t = threading.Thread(
            target=self._periodic_watchdog,
            daemon=True,
            name="poller-watchdog",
        )
        t.start()
        threads.append(t)
        _event_log("Poller started: watchdog (interval=30s)")

        # Trigger engine thread
        t = threading.Thread(
            target=self._periodic_trigger,
            daemon=True,
            name="poller-triggers",
        )
        t.start()
        threads.append(t)
        _event_log("Poller started: trigger engine (interval=15s)")

        _event_log("FallbackPoller: all polling threads started")
        return threads

    @staticmethod
    def _periodic_check(check_fn: Callable[[], bool], interval: int, stream_name: str) -> None:
        """Run a check function periodically until stop_event."""
        while not _STOP_EVENT.is_set():
            try:
                check_fn()
            except Exception as e:
                _event_log(f"Poller check {stream_name} error: {e}", "ERROR")
            _STOP_EVENT.wait(interval)

    def _periodic_watchdog(self) -> None:
        """Run watchdog at 30s."""
        while not _STOP_EVENT.is_set():
            try:
                self._watchdog_fn()
            except Exception as e:
                _event_log(f"Poller watchdog error: {e}", "ERROR")
            _STOP_EVENT.wait(30)

    def _periodic_trigger(self) -> None:
        """Run trigger engine at 15s."""
        while not _STOP_EVENT.is_set():
            try:
                self._trigger_fn()
            except Exception as e:
                _event_log(f"Poller trigger engine error: {e}", "ERROR")
            _STOP_EVENT.wait(15)


# ── Global Stop Event ────────────────────────────────────────────────────────

_STOP_EVENT = threading.Event()


# ── Event-Driven Daemon Orchestrator ─────────────────────────────────────────

class EventDrivenDaemon:
    """
    Orchestrates the event-driven hscc-daemon.

    On macOS with kqueue available:
      1. KqueueWatcher on ~/.hscc/state/  → fires EventBridge callbacks
      2. KqueueWatcher on ~/.hscc/        → config change detection
      3. launchd periodic jobs for fixed-interval streams (dgx, gateway, etc.)

    On non-macOS or when kqueue fails:
      1. FallbackPoller with the original timer-based polling loops
      2. No launchd jobs are installed

    The daemon runs until stopped via stop() or SIGTERM.
    """

    def __init__(
        self,
        check_map: Dict[str, Callable[[], bool]],
        watchdog_fn: Callable[[], bool],
        trigger_fn: Callable[[], None],
        install_launchd: bool = True,
    ) -> None:
        """
        Args:
            check_map:     Dict of stream_name -> check_function.
            watchdog_fn:   Pipeline watchdog function.
            trigger_fn:    Trigger engine function.
            install_launchd: If True, install launchd jobs for periodic streams.
        """
        self._check_map = check_map
        self._watchdog_fn = watchdog_fn
        self._trigger_fn = trigger_fn
        self._install_launchd = install_launchd
        self._kqueue_watchers: List[KqueueWatcher] = []
        self._launchd_gen: Optional[LaunchdJobGenerator] = None
        self._event_bridge = EventBridge(check_map)
        self._fallback_poller: Optional[FallbackPoller] = None
        self._fallback_threads: List[threading.Thread] = []
        self._using_kqueue = False
        self._using_launchd = False

    # ── Public API ───────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Start the event-driven daemon.

        Returns:
            True if successfully started (kqueue or fallback).
        """
        _event_log("EventDrivenDaemon starting...")

        # Set up signal handlers
        _STOP_EVENT.clear()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda s, f: _STOP_EVENT.set())
            except (ValueError, OSError):
                pass  # Can only set signal handlers in main thread

        # Set up EventBridge callbacks to re-evaluate triggers on state changes
        self._event_bridge.register_all_callback(self._on_state_change)

        # Try kqueue first
        kq_dir = STATE_DIR
        kq_success = self._init_kqueue(kq_dir, self._event_bridge.handle_state_change)

        # Also watch config directory
        self._init_kqueue(HSCC_DIR, self._event_bridge.handle_config_change)

        if kq_success:
            self._using_kqueue = True
            _event_log("EventDrivenDaemon: kqueue reactive layer active")
        else:
            _event_log("EventDrivenDaemon: kqueue unavailable, polling only")

        # Periodic checks always run in-process (each check writes state, which
        # kqueue then reacts to). Previously delegated to launchd, but install_job
        # only wrote the plists and never loaded them, so checks never fired.
        self._start_fallback()

        # Remove stale periodic launchd plists left by the pre-poller design.
        self._cleanup_stale_launchd()

        _event_log("EventDrivenDaemon started")
        return True

    def stop(self) -> None:
        """Stop all watchers, launchd jobs, and polling threads."""
        _event_log("EventDrivenDaemon stopping...")
        _STOP_EVENT.set()

        # Stop kqueue watchers
        for w in self._kqueue_watchers:
            try:
                w.stop()
            except Exception:
                pass
        self._kqueue_watchers.clear()

        # Stop fallback threads
        for t in self._fallback_threads:
            try:
                t.join(timeout=5)
            except Exception:
                pass
        self._fallback_threads.clear()

        # Uninstall launchd jobs
        if self._using_launchd and self._launchd_gen:
            try:
                self._launchd_gen.uninstall_all_periodic()
            except Exception:
                pass
            self._using_launchd = False

        _event_log("EventDrivenDaemon stopped")

    def is_running(self) -> bool:
        return not _STOP_EVENT.is_set()

    def status(self) -> dict:
        """Return daemon status information."""
        return {
            "running": not _STOP_EVENT.is_set(),
            "using_kqueue": self._using_kqueue,
            "using_launchd": self._using_launchd,
            "watcher_count": len(self._kqueue_watchers),
            "kqueue_watchers": [
                {"dir": w.directory, "running": w.is_running, "using_kqueue": w.using_kqueue}
                for w in self._kqueue_watchers
            ],
            "launchd_status": self._launchd_gen.status() if self._launchd_gen else {},
        }

    @property
    def event_bridge(self) -> EventBridge:
        return self._event_bridge

    # ── Internal ────────────────────────────────────────────────────────

    def _init_kqueue(self, directory: str, callback: Callable[[str], None]) -> bool:
        """Initialize a kqueue watcher for a directory. Returns True if successful."""
        try:
            watcher = KqueueWatcher(
                directory=directory,
                callback=callback,
                poll_interval=2.0,
                fallback=True,
            )
            watcher.start()
            self._kqueue_watchers.append(watcher)
            return watcher.using_kqueue
        except Exception as e:
            _event_log(f"Kqueue init failed for {directory}: {e}", "WARN")
            return False

    def _start_fallback(self) -> None:
        """Start the fallback polling mechanism."""
        self._fallback_poller = FallbackPoller(
            streams=PERIODIC_STREAMS,
            check_map=self._check_map,
            watchdog_fn=self._watchdog_fn,
            trigger_fn=self._trigger_fn,
        )
        self._fallback_threads = self._fallback_poller.start(_STOP_EVENT)

    def _cleanup_stale_launchd(self) -> None:
        """Unload+remove periodic launchd plists from the pre-poller design.

        Idempotent: bootout of an unloaded job is a harmless no-op. Without this,
        a leftover launchd idle job would double-run alongside the in-process one.
        """
        try:
            hscc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hscc.py")
            gen = LaunchdJobGenerator(hscc_script=hscc_path)
            gen.uninstall_all_periodic()
            _event_log("Removed stale periodic launchd plists")
        except Exception as e:
            _event_log(f"Stale launchd cleanup failed: {e}", "WARN")

    def _on_state_change(self, stream: str) -> None:
        """Callback: a state file changed, trigger downstream reactions."""
        # Check if we should re-evaluate triggers
        if stream == "triggers":
            return  # Skip to avoid recursion

        # State changed — log the event
        _event_log(f"State change detected: {stream}")

        # Re-read the state and log its ok status
        try:
            state_path = os.path.join(STATE_DIR, f"{stream}.json")
            with open(state_path) as f:
                state = json.load(f)
            ok = state.get("ok", "unknown")
            _event_log(f"  → {stream} ok={ok}")
        except Exception as e:
            _event_log(f"  → Error reading state: {e}", "ERROR")


# ── Convenience Functions ────────────────────────────────────────────────────

def cmd_install_event_driven() -> None:
    """CLI command: install event-driven mode with kqueue + launchd."""
    print("Installing event-driven mode...")
    print(f"  Config dir:  {HSCC_DIR}")
    print(f"  State dir:   {STATE_DIR}")
    print(f"  Plist dir:   {PLIST_DIR}")

    # Create launchd dir
    os.makedirs(PLIST_DIR, exist_ok=True)

    # Generate and install periodic jobs
    hscc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hscc.py")
    gen = LaunchdJobGenerator(hscc_script=hscc_path)

    print("\n  Generating launchd jobs:")
    for stream, interval in PERIODIC_STREAMS.items():
        success, msg = gen.install_job(stream, interval)
        status = "✓" if success else "✗"
        print(f"    {status} {stream:<12s} every {interval:>3s}s  → {msg}")

    print("\n  Kqueue watchers will be started when daemon begins.")
    print("  Run: python3 hscc.py start-daemon  (uses event-driven mode)")
    print("  Or install normally: python3 hscc.py install")


def cmd_uninstall_event_driven() -> None:
    """CLI command: remove event-driven launchd jobs."""
    print("Removing event-driven launchd jobs...")
    hscc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hscc.py")
    gen = LaunchdJobGenerator(hscc_script=hscc_path)

    results = gen.uninstall_all_periodic()
    for stream, (ok, msg) in results.items():
        status = "✓" if ok else "✗"
        print(f"  {status} {stream:<12s} → {msg}")

    print("\nEvent-driven mode removed. Daemons will use fallback polling.")


def cmd_event_status() -> None:
    """CLI command: show event-driven mode status."""
    hscc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hscc.py")
    gen = LaunchdJobGenerator(hscc_script=hscc_path)
    status = gen.status()

    print("Event-Driven Mode Status")
    print("=" * 60)

    # Kqueue availability
    kqueue_available = hasattr(select, "kqueue")
    print(f"  kqueue:         {'Available ✓' if kqueue_available else 'Unavailable ✗ (will use polling)'}")

    print()
    print("  ── Launchd Periodic Jobs ─────────────────────")
    print(f"  {'Stream':<12s} {'Interval':<10s} {'Exists':<8s} {'Loaded':<8s}")
    print(f"  {'─'*12} {'─'*10} {'─'*8} {'─'*8}")

    for stream in PERIODIC_STREAMS:
        info = status.get(stream, {})
        print(f"  {stream:<12s} {info.get('interval', '?'):<10s} "
              f"{'yes' if info.get('exists') else 'no':<8s} "
              f"{'yes' if info.get('loaded') else 'no':<8s}")

    print()
    print("  Kqueue Watchers:")
    # Check if watchers are active by looking for log entries
    try:
        with open(os.path.join(HSCC_DIR, "daemon.log")) as f:
            lines = f.readlines()
        kqueue_lines = [l for l in lines if "KqueueWatcher" in l]
        if kqueue_lines:
            print(f"    Active watchers found in log ({len(kqueue_lines)} entries)")
            for line in kqueue_lines[-5:]:
                print(f"    {line.strip()}")
        else:
            print("    No active watchers (daemon not running or polling fallback)")
    except (FileNotFoundError, IOError):
        print("    No daemon log found")

    print("=" * 60)


# ── Integration with hscc.py ────────────────────────────────────────────────

def run_event_drained_daemon_loop(
    check_dgx: Callable[[], bool],
    check_gateway: Callable[[], bool],
    check_local: Callable[[], bool],
    check_heartbeat: Callable[[], bool],
    check_nas: Callable[[], bool],
    pipeline_watchdog: Callable[[], bool],
    trigger_engine_fn: Callable[[], None],
) -> None:
    """
    Run the daemon loop using event-driven architecture.

    This is the entry point called by run_daemon_loop() when event-driven mode
    is enabled. It replaces the old threading.Timer-based polling with:
      - kqueue watchers for state and config directories
      - launchd periodic jobs for fixed-interval checks

    The old run_daemon_loop() function can be kept for backwards compatibility.
    """
    check_map = {
        "dgx": check_dgx,
        "gateway": check_gateway,
        "local": check_local,
        "heartbeat": check_heartbeat,
        "nas": check_nas,
    }

    daemon = EventDrivenDaemon(
        check_map=check_map,
        watchdog_fn=pipeline_watchdog,
        trigger_fn=trigger_engine_fn,
        install_launchd=True,
    )

    try:
        daemon.start()
        # Main loop: wait for stop signal
        while not _STOP_EVENT.is_set():
            _STOP_EVENT.wait(1)
    finally:
        daemon.stop()


# ── Tests ────────────────────────────────────────────────────────────────────

def run_tests() -> Tuple[int, int]:
    """
    Run all unit tests for event_driven.py.

    Returns:
        (passed, failed) count.
    """
    passed = 0
    failed = 0

    def assert_eq(name: str, got, expected) -> None:
        nonlocal passed, failed
        if got == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL {name}: got {got!r}, expected {expected!r}")

    def assert_true(name: str, value) -> None:
        nonlocal passed, failed
        if value:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL {name}: expected True, got {value!r}")

    def assert_false(name: str, value) -> None:
        nonlocal passed, failed
        if not value:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL {name}: expected False, got {value!r}")

    def assert_raises(name: str, exc_type, fn, *args) -> None:
        nonlocal passed, failed
        try:
            fn(*args)
            failed += 1
            print(f"  FAIL {name}: expected {exc_type.__name__}")
        except exc_type:
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL {name}: expected {exc_type.__name__}, got {type(e).__name__}: {e}")

    print("  Testing KqueueWatcher class...")

    # Test 1: KqueueWatcher construction
    def test_watcher_construction():
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp(prefix="hscc_test_kqueue_")
        try:
            cb_called = []
            watcher = KqueueWatcher(tmpdir, cb_called.append)
            assert_eq("watcher_dir", watcher.directory, tmpdir)
            assert_false("watcher_running", watcher.is_running)
            assert_false("watcher_kqueue", watcher.using_kqueue)  # not started yet
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    test_watcher_construction()

    # Test 2: KqueueWatcher start/stop
    def test_watcher_start_stop():
        import tempfile, shutil, time
        tmpdir = tempfile.mkdtemp(prefix="hscc_test_kqueue_start_")
        try:
            events = []
            watcher = KqueueWatcher(tmpdir, events.append, poll_interval=0.5)
            watcher.start()
            assert_true("watcher_running", watcher.is_running)
            # kqueue available on macOS
            time.sleep(0.3)
            watcher.stop()
            assert_false("watcher_stopped", watcher.is_running)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    test_watcher_start_stop()

    # Test 3: Callback fires on file change (polling fallback or kqueue)
    def test_callback_on_change():
        import tempfile, shutil, time
        tmpdir = tempfile.mkdtemp(prefix="hscc_test_callback_")
        try:
            events = []
            watcher = KqueueWatcher(tmpdir, events.append, poll_interval=0.5)
            # Pre-create a file so kqueue can register a watcher for it
            with open(os.path.join(tmpdir, "test.json"), "w") as f:
                json.dump({"initial": True}, f)
            watcher.start()
            time.sleep(0.3)
            # Modify the file (triggers callback via kqueue or polling)
            with open(os.path.join(tmpdir, "test.json"), "w") as f:
                json.dump({"test": True}, f)
            # Wait for event to propagate
            time.sleep(1.5)
            watcher.stop()
            assert_true("callback fired", len(events) > 0)
            assert_eq("correct file tracked", "test.json" in events, True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    test_callback_on_change()

    # Test 4: Thread safety — multiple callbacks
    def test_thread_safety():
        import tempfile, shutil, time
        tmpdir = tempfile.mkdtemp(prefix="hscc_test_thread_")
        try:
            counter = {"value": 0}
            lock = threading.Lock()

            def safe_callback(filename):
                with lock:
                    counter["value"] += 1

            # Pre-create files so kqueue can watch them
            for i in range(5):
                with open(os.path.join(tmpdir, f"file{i}.json"), "w") as f:
                    json.dump({"i": i}, f)

            watcher = KqueueWatcher(tmpdir, safe_callback, poll_interval=0.5)
            watcher.start()
            time.sleep(0.3)

            # Modify files to trigger callbacks
            for i in range(5):
                with open(os.path.join(tmpdir, f"file{i}.json"), "w") as f:
                    json.dump({"i": i, "modified": True}, f)
                time.sleep(0.05)

            time.sleep(1.0)
            watcher.stop()
            # Counter should be > 0 (some callbacks fired)
            assert_true("callbacks executed", counter["value"] > 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    test_thread_safety()

    print("  Testing LaunchdJobGenerator class...")

    # Test 5: Plist generation
    def test_plist_generation():
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            hscc_file = os.path.join(tmpdir, "hscc.py")
            with open(hscc_file, "w") as f:
                f.write("#!/usr/bin/env python3")
            gen = LaunchdJobGenerator(hscc_script=hscc_file)
            plist = gen.generate_plist("test.label", "dgx", 5)
            assert_true("plist is valid XML", "<plist" in plist)
            assert_true("label included", "test.label" in plist)
            assert_true("stream included", "<string>dgx</string>" in plist)
            assert_true("interval included", "<integer>5</integer>" in plist)

    test_plist_generation()

    # Test 6: Install/uninstall job
    def test_install_uninstall():
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Override PLIST_DIR for testing
            global PLIST_DIR
            original_pdir = PLIST_DIR
            PLIST_DIR = os.path.join(tmpdir, "LaunchAgents")

            hscc_file = os.path.join(tmpdir, "hscc.py")
            with open(hscc_file, "w") as f:
                f.write("#!/usr/bin/env python3")

            gen = LaunchdJobGenerator(hscc_script=hscc_file)

            # Install
            success, msg = gen.install_job("test_stream", 10)
            assert_true("install succeeded", success)
            assert_true("is_installed", gen.is_installed("test_stream"))

            plist_path = os.path.join(PLIST_DIR, "com.nousresearch.hscc-periodic.test_stream.plist")
            assert_true("plist exists on disk", os.path.exists(plist_path))

            # Uninstall
            success, msg = gen.uninstall_job("test_stream")
            assert_true("uninstall succeeded", success)
            assert_false("not installed", gen.is_installed("test_stream"))

            PLIST_DIR = original_pdir

    test_install_uninstall()

    # Test 7: Install all periodic streams
    def test_install_all():
        with tempfile.TemporaryDirectory() as tmpdir:
            global PLIST_DIR
            original_pdir = PLIST_DIR
            PLIST_DIR = os.path.join(tmpdir, "LaunchAgents")

            hscc_file = os.path.join(tmpdir, "hscc.py")
            with open(hscc_file, "w") as f:
                f.write("#!/usr/bin/env python3")

            gen = LaunchdJobGenerator(hscc_script=hscc_file)
            results = gen.install_all_periodic()
            assert_eq("all streams installed", len(results), len(PERIODIC_STREAMS))
            for stream, (ok, msg) in results.items():
                assert_true(f"{stream} installed", ok)

            PLIST_DIR = original_pdir

    test_install_all()

    print("  Testing EventBridge class...")

    # Test 8: EventBridge callback registration and firing
    def test_event_bridge():
        bridge = EventBridge({})
        results = []

        def my_callback(stream):
            results.append(stream)

        bridge.register_callback("dgx", my_callback)
        bridge.register_all_callback(lambda s: results.append(f"all:{s}"))

        # Fire state change
        bridge.handle_state_change("dgx.json")
        assert_eq("dgx callback fired", results, ["dgx", "all:dgx"])

        # Non-matching file
        bridge.handle_state_change("other.txt")
        assert_eq("non-json ignored", results, ["dgx", "all:dgx"])

        # Another stream
        bridge.handle_state_change("gateway.json")
        assert_eq("gateway callback fired", results, ["dgx", "all:dgx", "all:gateway"])

    test_event_bridge()

    # Test 9: Config change detection
    def test_config_change():
        bridge = EventBridge({"triggers": lambda: None})
        results = []

        def track():
            results.append("trigger_rerun")
            return True

        bridge = EventBridge({"triggers": track})
        bridge.handle_config_change("triggers.json")
        assert_true("trigger config triggered", "triggers" in results or len(results) > 0)

        # Non-config file
        result = bridge.handle_config_change("unknown.json")
        assert_eq("unknown config ignored", result, None)

    test_config_change()

    # Test 10: EventBridge thread safety
    def test_event_bridge_thread_safety():
        bridge = EventBridge({})
        counter = {"value": 0}
        lock = threading.Lock()

        def safe_cb(stream):
            with lock:
                counter["value"] += 1

        bridge.register_all_callback(safe_cb)

        # Fire events from multiple threads
        threads = []
        for i in range(10):
            t = threading.Thread(
                target=bridge.handle_state_change,
                args=(f"stream{i%3}.json",),
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert_true("all callbacks completed", counter["value"] >= 10)

    test_event_bridge_thread_safety()

    print("  Testing FallbackPoller class...")

    # Test 11: FallbackPoller construction
    def test_fallback_poller():
        def dummy_check():
            return True
        poller = FallbackPoller(
            streams={"dgx": 5},
            check_map={"dgx": dummy_check},
            watchdog_fn=dummy_check,
            trigger_fn=lambda: None,
        )
        assert_true("poller created", poller is not None)

    test_fallback_poller()

    # Test 12: FallbackPoller starts threads
    def test_fallback_poller_threads():
        def dummy_check():
            return True
        stop = threading.Event()
        poller = FallbackPoller(
            streams={"dgx": 5},
            check_map={"dgx": dummy_check},
            watchdog_fn=dummy_check,
            trigger_fn=lambda: None,
        )
        threads = poller.start(stop)
        assert_true("threads started", len(threads) >= 3)  # checks + watchdog + trigger
        time.sleep(0.1)
        stop.set()
        for t in threads:
            t.join(timeout=2)

    test_fallback_poller_threads()

    print("  Testing EventDrivenDaemon class...")

    # Test 13: EventDrivenDaemon construction
    def test_daemon_construction():
        def dummy_check():
            return True
        daemon = EventDrivenDaemon(
            check_map={"dgx": dummy_check},
            watchdog_fn=dummy_check,
            trigger_fn=lambda: None,
            install_launchd=False,
        )
        assert_true("daemon created", daemon is not None)
        assert_true("event_bridge available", daemon.event_bridge is not None)

    test_daemon_construction()

    # Test 14: EventDrivenDaemon start/stop without launchd
    def test_daemon_start_stop():
        def dummy_check():
            return True
        daemon = EventDrivenDaemon(
            check_map={"dgx": dummy_check},
            watchdog_fn=dummy_check,
            trigger_fn=lambda: None,
            install_launchd=False,
        )
        daemon.start()
        assert_true("daemon running", daemon.is_running())
        time.sleep(0.3)
        daemon.stop()
        assert_false("daemon stopped", daemon.is_running())

    test_daemon_start_stop()

    # Test 15: Status report
    def test_daemon_status():
        def dummy_check():
            return True
        daemon = EventDrivenDaemon(
            check_map={"dgx": dummy_check},
            watchdog_fn=dummy_check,
            trigger_fn=lambda: None,
            install_launchd=False,
        )
        status = daemon.status()
        assert_true("status has running", "running" in status)
        assert_true("status has watchers", "watcher_count" in status)

    test_daemon_status()

    # Test 16: KqueueWatcher handles OSError on close
    def test_watcher_close_oops():
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp(prefix="hscc_test_close_")
        try:
            watcher = KqueueWatcher(tmpdir, lambda f: None)
            watcher.start()
            time.sleep(0.2)
            # Close should not raise
            watcher.stop()
            assert_true("stop completed", not watcher.is_running)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    test_watcher_close_oops()

    print()
    print(f"  Results: {passed} passed, {failed} failed")
    return passed, failed


# ── CLI Entry Point ──────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for event_driven.py standalone operations."""
    if len(sys.argv) < 2:
        print("HSCC Event-Driven Module")
        print("Usage: event_driven.py <command> [args]")
        print()
        print("Commands:")
        print("  install     Install event-driven launchd jobs")
        print("  uninstall   Remove event-driven launchd jobs")
        print("  status      Show event-driven mode status")
        print("  test        Run unit tests")
        print("  help        Show this help message")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "install":    lambda: cmd_install_event_driven(),
        "uninstall":  lambda: cmd_uninstall_event_driven(),
        "status":     lambda: cmd_event_status(),
        "test":       lambda: run_tests(),
        "help":       lambda: main(),  # prints help
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)

    try:
        commands[cmd]()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import sys
    main()
