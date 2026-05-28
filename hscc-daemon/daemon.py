#!/usr/bin/env python3
"""HSCC Monitoring Daemon — core loop, escalator, CLI.

Usage:
  hscc-daemon start    # Run daemon in foreground
  hscc-daemon stop     # Graceful shutdown
  hscc-daemon status   # Show latest health report
  hscc-daemon alerts   # Show pending alerts
  hscc-daemon check    # Run self-diagnostic

State directory: ~/.hscc/daemon/
Config file:     ~/.hscc/daemon/config.json
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


# ── Constants ──────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
DAEMON_DIR = os.path.join(HSCC_DIR, "daemon")
STATUS_FILE = os.path.join(DAEMON_DIR, "status.json")
ALERTS_FILE = os.path.join(DAEMON_DIR, "alerts.jsonl")
CONFIG_FILE = os.path.join(DAEMON_DIR, "config.json")
PID_FILE = os.path.join(DAEMON_DIR, "daemon.pid")

DEFAULT_CONFIG = {
    "poll_interval_sec": 60,
    "handlers": {
        "vllm": {"url": "http://localhost:8000/health"},
        "gateway": {"url": "http://localhost:18789/health"},
        "container": {"id": "hscc-orchestrator"},
        "nas": {"host": "nas.local", "path": "/", "key_path": None},
    },
    "telegram": {
        "chat_id": None,
        "bot_token": None,
        "max_restarts": 1,
        "max_alerts_per_60s": 5,
    },
}

TELEGRAM_TIMEOUT = 10
TELEGRAM_MAX_PER_MINUTE = 5


# ── Config helpers ─────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict):
    """Deep merge override into base (in-place)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_config() -> dict:
    """Load daemon config, falling back to defaults. Warns on missing file."""
    if not os.path.exists(CONFIG_FILE):
        print(
            f"[WARN] Config not found at {CONFIG_FILE}, using defaults",
            file=sys.stderr,
        )
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with open(CONFIG_FILE, "r") as f:
        user_config = json.load(f)

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    _deep_merge(config, user_config)
    return config


def ensure_daemon_dir():
    """Create ~/.hscc/daemon/ if it doesn't exist."""
    os.makedirs(DAEMON_DIR, exist_ok=True)


# ── Handler registry ───────────────────────────────────────────────────────

def instantiate_handlers(config: dict) -> dict:
    """Create handler instances from config. Returns {name: handler}."""
    from handlers.vllm import VLLMHandler
    from handlers.container import ContainerHandler
    from handlers.gateway import GatewayHandler
    from handlers.nas import NASHandler

    handlers = {}
    h_conf = config.get("handlers", {})

    vllm_conf = h_conf.get("vllm", {})
    handlers["vllm"] = VLLMHandler(
        url=vllm_conf.get("url", "http://localhost:8000/health")
    )

    gw_conf = h_conf.get("gateway", {})
    handlers["gateway"] = GatewayHandler(
        url=gw_conf.get("url", "http://localhost:18789/health")
    )

    ct_conf = h_conf.get("container", {})
    handlers["container"] = ContainerHandler(
        container_id=ct_conf.get("id", "hscc-orchestrator")
    )

    nas_conf = h_conf.get("nas", {})
    handlers["nas"] = NASHandler(
        host=nas_conf.get("host", "nas.local"),
        path=nas_conf.get("path", "/"),
        key_path=nas_conf.get("key_path"),
    )

    return handlers


# ── Daemon class ───────────────────────────────────────────────────────────

class Daemon:
    """Main daemon process — runs the monitoring loop."""

    def __init__(self, config=None, dry_run=False):
        if config is None:
            config = load_config()
        self.config = config
        self.dry_run = dry_run
        self.handlers = instantiate_handlers(config)
        self.running = False
        self.cycle = 0
        self.telegram_alert_timestamps = []
        self.pending_alerts = []

    # ── Core loop ───────────────────────────────────────────────────────

    def run(self):
        """Start the monitoring loop. Blocks until shutdown signal."""
        ensure_daemon_dir()

        # Write PID file
        try:
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        except IOError:
            pass

        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        print(
            f"[INFO] HSCC Daemon starting (PID {os.getpid()}, "
            f"poll_interval={self.config['poll_interval_sec']}s, "
            f"dry_run={self.dry_run})"
        )
        print(f"[INFO] Handlers: {', '.join(self.handlers.keys())}")

        try:
            while self.running:
                self._run_cycle()
                if self.running:
                    time.sleep(self.config["poll_interval_sec"])
        finally:
            # Clean up PID file
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            print("[INFO] Daemon shutting down cleanly")

    def _run_cycle(self):
        """Execute one monitoring cycle."""
        self.cycle += 1
        print(f"\n[cycle {self.cycle}] Running health checks...")

        report = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cycle": self.cycle,
            "checks": {},
            "actions": [],
        }

        # Phase 1: Run all handlers (sequential, each with timeout)
        for name, handler in self.handlers.items():
            print(f"  checking {name}...", end=" ", flush=True)
            result = handler.run()
            report["checks"][name] = result.to_dict()
            status_icon = {
                "healthy": "\u2713",
                "unhealthy": "\u2717",
                "unknown": "?",
            }[result.status]
            print(f"{status_icon} {result.status}")

        # Phase 2: Escalator
        self._escalate(report)

        # Phase 3: Execute actions
        self._execute_actions(report)

        # Phase 4: Persist
        self._persist_status(report)
        self._persist_alerts()

        actions_str = ", ".join(report["actions"]) if report["actions"] else "none"
        print(f"[cycle {self.cycle}] Done. Actions: {actions_str}")

    # ── Escalator ───────────────────────────────────────────────────────

    def _escalate(self, report):
        """Read report and decide actions.

        Rules:
          - Orchestrator unhealthy (vllm + container) -> restart (max 1/cycle)
          - Non-orchestrator unhealthy -> telegram_alert
          - ALL unknown -> telegram_alert_all_unknown
          - Individual unknown -> warn only
        """
        checks = report["checks"]
        actions = report["actions"]

        statuses = [c["status"] for c in checks.values()]
        all_unknown = all(s == "unknown" for s in statuses)

        vllm_status = checks.get("vllm", {}).get("status", "unknown")
        container_status = checks.get("container", {}).get("status", "unknown")
        gateway_status = checks.get("gateway", {}).get("status", "unknown")
        nas_status = checks.get("nas", {}).get("status", "unknown")

        # Orchestrator: restart only if BOTH vllm AND container are unhealthy
        # AND neither is unknown (explicit confirmation needed)
        orchestrator_down = (
            vllm_status == "unhealthy"
            and container_status == "unhealthy"
            and vllm_status != "unknown"
            and container_status != "unknown"
        )
        if orchestrator_down and "restart_orchestrator" not in actions:
            actions.append("restart_orchestrator")

        # Non-orchestrator unhealthy
        if not orchestrator_down and ("unhealthy" in statuses):
            if "telegram_alert" not in actions:
                actions.append("telegram_alert")

        # All unknown - system blind
        if all_unknown and "telegram_alert_all_unknown" not in actions:
            actions.append("telegram_alert_all_unknown")

    # ── Action executor ─────────────────────────────────────────────────

    def _execute_actions(self, report):
        """Execute escalated actions with safety guards."""
        actions = report["actions"]
        if not actions:
            return

        if self.dry_run:
            print("  [DRY-RUN] Actions that WOULD be taken:")
            for action in actions:
                print(f"    - {action}")
            return

        if "restart_orchestrator" in actions:
            print("  [ACTION] Restarting orchestrator container...")
            self._restart_orchestrator()

        if "telegram_alert" in actions:
            self._send_telegram_alert(
                "non-orchestrator unhealthy", report
            )

        if "telegram_alert_all_unknown" in actions:
            self._send_telegram_alert(
                "all handlers unknown (system blind)", report
            )

    def _restart_orchestrator(self):
        """Restart the orchestrator container. Max 1 per cycle."""
        container_id = self.config["handlers"]["container"]["id"]
        try:
            subprocess.run(
                ["docker", "restart", container_id],
                capture_output=True, text=True, timeout=30,
            )
            print("    Orchestrator restart initiated")
        except subprocess.TimeoutExpired:
            print("    [ERROR] Orchestrator restart timed out")
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "critical",
                "message": "Orchestrator restart failed (timeout)",
                "auto_resolved": False,
            })
        except Exception as e:
            print(f"    [ERROR] Orchestrator restart failed: {e}")
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "critical",
                "message": f"Orchestrator restart failed: {e}",
                "auto_resolved": False,
            })

    def _send_telegram_alert(self, reason, report):
        """Send Telegram alert via orchestrator subprocess. Rate-limited."""
        telegram_conf = self.config.get("telegram", {})
        chat_id = telegram_conf.get("chat_id")
        bot_token = telegram_conf.get("bot_token")

        if not chat_id or not bot_token:
            print(
                "    [WARN] Telegram not configured - alert logged to file only"
            )
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "warning",
                "message": f"{reason} - Telegram not configured",
                "auto_resolved": False,
                "report": report,
            })
            return

        # Rate limit: max 5 per 60s
        now = time.time()
        self.telegram_alert_timestamps = [
            t for t in self.telegram_alert_timestamps if now - t < 60
        ]
        if len(self.telegram_alert_timestamps) >= TELEGRAM_MAX_PER_MINUTE:
            print(
                f"    [WARN] Telegram rate limited ({TELEGRAM_MAX_PER_MINUTE}/min)"
            )
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "warning",
                "message": f"Rate limited Telegram alert: {reason}",
                "auto_resolved": False,
                "report": report,
            })
            return

        # Build alert text
        alert_text = (
            f"\U0001f514 HSCC Alert\n"
            f"Reason: {reason}\n"
            f"Cycle: {report['cycle']}\n"
            f"Time: {report['timestamp']}\n"
            f"Checks:\n"
        )
        for name, check in report["checks"].items():
            alert_text += f"  {name}: {check['status']}\n"

        try:
            # Delegate to orchestrator's Telegram bot
            result = subprocess.run(
                ["hscc-daemon", "telegram", "send", alert_text],
                capture_output=True, text=True, timeout=TELEGRAM_TIMEOUT,
            )
            if result.returncode == 0:
                self.telegram_alert_timestamps.append(now)
            else:
                self.pending_alerts.append({
                    "timestamp": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "severity": "warning",
                    "message": f"Telegram send failed: {result.stderr.strip()}",
                    "auto_resolved": False,
                })
        except FileNotFoundError:
            print(
                "    [WARN] hscc-daemon CLI not found - alert logged to file"
            )
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "warning",
                "message": f"Telegram send failed (CLI not found): {reason}",
                "auto_resolved": False,
            })
        except Exception as e:
            self.pending_alerts.append({
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "severity": "warning",
                "message": f"Telegram send failed: {e}",
                "auto_resolved": False,
            })

    # ── State persistence ───────────────────────────────────────────────

    def _persist_status(self, report):
        """Write latest cycle report to status.json."""
        try:
            with open(STATUS_FILE, "w") as f:
                json.dump(report, f, indent=2)
        except IOError as e:
            print(f"    [ERROR] Failed to write status.json: {e}")

    def _persist_alerts(self):
        """Append pending alerts to alerts.jsonl."""
        if not self.pending_alerts:
            return

        try:
            with open(ALERTS_FILE, "a") as f:
                for alert in self.pending_alerts:
                    f.write(json.dumps(alert) + "\n")
            self.pending_alerts.clear()
        except IOError as e:
            print(f"    [ERROR] Failed to write alerts.jsonl: {e}")

    # ── Signal handler ──────────────────────────────────────────────────

    def _signal_handler(self, signum, frame):
        """Handle shutdown signal - finish current cycle then exit."""
        print(
            f"\n[INFO] Received signal {signum}, "
            f"finishing current cycle..."
        )
        self.running = False

    # ── Self-check ──────────────────────────────────────────────────────

    def self_check(self):
        """Run daemon self-diagnostic checks at startup.

        Returns dict with {check_name: {status, detail}}.
        """
        results = {}

        # Check 1: Config file
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    json.load(f)
                results["config_file"] = {
                    "status": "ok",
                    "detail": f"{CONFIG_FILE} exists and is valid JSON",
                }
            except json.JSONDecodeError as e:
                results["config_file"] = {
                    "status": "fail",
                    "detail": f"{CONFIG_FILE} is invalid JSON: {e}",
                }
            except IOError as e:
                results["config_file"] = {
                    "status": "warn",
                    "detail": f"Cannot read {CONFIG_FILE}: {e}",
                }
        else:
            results["config_file"] = {
                "status": "warn",
                "detail": f"{CONFIG_FILE} not found (using defaults)",
            }

        # Check 2: Daemon directory writable
        try:
            test_file = os.path.join(DAEMON_DIR, ".write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            results["daemon_dir"] = {
                "status": "ok",
                "detail": f"{DAEMON_DIR} is writable",
            }
        except IOError as e:
            results["daemon_dir"] = {
                "status": "fail",
                "detail": f"{DAEMON_DIR} not writable: {e}",
            }

        # Check 3: Required commands exist
        for cmd in ["docker", "ssh"]:
            found = (
                subprocess.run(
                    ["which", cmd], capture_output=True, text=True
                ).returncode
                == 0
            )
            results[f"cmd_{cmd}"] = {
                "status": "ok" if found else "warn",
                "detail": f"{cmd} {'found' if found else 'not found'}",
            }

        # Check 4: Python handlers importable
        for handler_name in ["vllm", "gateway", "container", "nas"]:
            try:
                __import__(
                    f"handlers.{handler_name}", fromlist=[""]
                )
                results[f"handler_{handler_name}"] = {
                    "status": "ok",
                    "detail": f"handlers.{handler_name} imports OK",
                }
            except ImportError as e:
                results[f"handler_{handler_name}"] = {
                    "status": "fail",
                    "detail": f"Cannot import handlers.{handler_name}: {e}",
                }

        # Check 5: PID file check
        pid_file = os.path.join(DAEMON_DIR, "daemon.pid")
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                os.kill(pid, 0)
                results["pid_file"] = {
                    "status": "warn",
                    "detail": f"PID file exists with PID {pid} (may be stale)",
                }
            except (ValueError, ProcessLookupError):
                results["pid_file"] = {
                    "status": "warn",
                    "detail": f"PID file stale (PID {pid} not running)",
                }
        else:
            results["pid_file"] = {
                "status": "ok",
                "detail": "No PID file (daemon not running)",
            }

        # Check 6: Previous status file integrity
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE) as f:
                    data = json.load(f)
                required = {"timestamp", "cycle", "checks", "actions"}
                missing = required - set(data.keys())
                if missing:
                    results["status_file"] = {
                        "status": "warn",
                        "detail": f"status.json missing keys: {missing}",
                    }
                else:
                    results["status_file"] = {
                        "status": "ok",
                        "detail": f"status.json valid (cycle {data.get('cycle', '?')})",
                    }
            except (json.JSONDecodeError, IOError) as e:
                results["status_file"] = {
                    "status": "fail",
                    "detail": f"status.json corrupted: {e}",
                }
        else:
            results["status_file"] = {
                "status": "ok",
                "detail": "No status file yet (first run)",
            }

        return results

    def run_self_check(self):
        """Run self-check and print results. Returns True if all ok."""
        print("[SELF-CHECK] Running daemon self-diagnostic...\n")
        results = self.self_check()
        all_ok = True
        for check_name, result in results.items():
            icon = {"ok": "\u2713", "warn": "\u26a0", "fail": "\u2717"}[
                result["status"]
            ]
            print(f"  {icon} {check_name}: {result['detail']}")
            if result["status"] == "fail":
                all_ok = False
        print(f"\n[SELF-CHECK] {'All checks passed' if all_ok else 'Some checks FAILED'}")
        return all_ok


# ── CLI helpers ─────────────────────────────────────────────────────────────

def cmd_status():
    """Show latest health report from status.json."""
    if not os.path.exists(STATUS_FILE):
        print("No status file found. Run 'hscc-daemon start' first.")
        sys.exit(1)

    with open(STATUS_FILE) as f:
        report = json.load(f)

    print(f"Cycle {report['cycle']} — {report['timestamp']}\n")
    for name, check in report.get("checks", {}).items():
        status = check["status"]
        icon = {"healthy": "\u2713", "unhealthy": "\u2717", "unknown": "?"}[status]
        print(f"  {icon} {name}: {status}")
        if check.get("detail"):
            print(f"    {json.dumps(check['detail'])}")

    if report.get("actions"):
        print(f"\nActions taken: {', '.join(report['actions'])}")


def cmd_alerts():
    """List pending alerts from alerts.jsonl."""
    if not os.path.exists(ALERTS_FILE):
        print("No alerts file found.")
        return

    count = 0
    with open(ALERTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            alert = json.loads(line)
            severity = alert.get("severity", "unknown")
            ts = alert.get("timestamp", "?")
            msg = alert.get("message", "")
            resolved = alert.get("auto_resolved", False)
            icon = "\u2713" if resolved else "\u26a0"
            print(f"[{icon}] [{severity.upper()}] {ts}: {msg}")
            count += 1

    if count == 0:
        print("No alerts found.")


def cmd_telegram_send(message):
    """Delegate Telegram message sending via orchestrator bot."""
    print(f"[TELEGRAM-SEND] Would send: {message}")
    sys.exit(0)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__.strip(), prog="hscc-daemon"
    )
    parser.add_argument(
        "command",
        choices=["start", "stop", "status", "alerts", "telegram", "check"],
        help="Command to run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks but don't restart or send alerts",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: ~/.hscc/daemon/config.json)",
    )
    args = parser.parse_args()

    # Override config path if specified
    if args.config:
        global CONFIG_FILE, STATUS_FILE, ALERTS_FILE, PID_FILE
        config_dir = os.path.dirname(args.config) or "."
        CONFIG_FILE = args.config
        STATUS_FILE = os.path.join(config_dir, "status.json")
        ALERTS_FILE = os.path.join(config_dir, "alerts.jsonl")
        PID_FILE = os.path.join(config_dir, "daemon.pid")

    # Dispatch
    if args.command == "start":
        daemon = Daemon(dry_run=args.dry_run)
        daemon.run()

    elif args.command == "stop":
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (PID {pid})")
        except (FileNotFoundError, ProcessLookupError, ValueError) as e:
            print(f"Daemon not running: {e}")
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
            sys.exit(1)

    elif args.command == "status":
        cmd_status()

    elif args.command == "alerts":
        cmd_alerts()

    elif args.command == "telegram" and len(sys.argv) >= 4 and sys.argv[2] == "send":
        cmd_telegram_send(" ".join(sys.argv[3:]))

    elif args.command == "check":
        daemon = Daemon()
        ok = daemon.run_self_check()
        sys.exit(0 if ok else 1)

    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
