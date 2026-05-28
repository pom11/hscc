#!/usr/bin/env python3
"""
HSCC Event-Driven Pattern Detector

Scans all HSCC plugin Python files to identify polling patterns and suggest
kqueue (macOS) / launchd / filesystem-watch replacements.

Usage:
    python hscc-event-detector.py [plugin_dir]

If no plugin_dir is provided, scans ~/.hermes/plugins/hscc-*/
"""

import ast
import os
import sys
import json
import re
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime


# ── Constants ──────────────────────────────────────────────────────────────

HSCC_PLUGINS_DIR = os.path.expanduser("~/.hermes/plugins")
HSCC_DIR = os.path.expanduser("~/.hscc")

# Patterns that indicate polling-based checking
POLLING_PATTERNS = {
    "time.sleep": {
        "description": "Static sleep interval (polling loop)",
        "severity": "HIGH",
        "recommendation": "Replace with kqueue file-watcher or launchd timer"
    },
    "while True:": {
        "description": "Infinite loop (likely polling with sleep)",
        "severity": "HIGH",
        "recommendation": "Convert to event-driven callback or kqueue monitoring"
    },
    "while(1):": {
        "description": "Infinite loop (likely polling with sleep)",
        "severity": "HIGH",
        "recommendation": "Convert to event-driven callback or kqueue monitoring"
    },
    "while 1:": {
        "description": "Infinite loop (likely polling with sleep)",
        "severity": "HIGH",
        "recommendation": "Convert to event-driven callback or kqueue monitoring"
    },
    "while True": {
        "description": "Infinite loop (likely polling with sleep)",
        "severity": "HIGH",
        "recommendation": "Convert to event-driven callback or kqueue monitoring"
    },
    "def run(.*):": {
        "description": "Potential daemon/run loop function",
        "severity": "MEDIUM",
        "recommendation": "Check if body contains polling logic"
    },
    "def run_loop(.*):": {
        "description": "Explicit run loop function",
        "severity": "MEDIUM",
        "recommendation": "Convert to event-driven architecture"
    },
    "def check_": {
        "description": "Recurring check function (likely called in polling loop)",
        "severity": "MEDIUM",
        "recommendation": "Convert to file-watch trigger or kqueue callback"
    },
    "def monitor": {
        "description": "Monitor function (potential polling)",
        "severity": "LOW",
        "recommendation": "Convert to kqueue-based filesystem watching"
    },
}

# Patterns that indicate file-based state checking
FILE_POLLING_PATTERNS = {
    "os.path.exists": {
        "description": "File existence polling",
        "severity": "MEDIUM",
        "recommendation": "Replace with kqueue EVFILT_VNODE or watchdog.FileObserver"
    },
    "open(path)": {
        "description": "Direct file open in loop",
        "severity": "MEDIUM",
        "recommendation": "Watch file changes instead of periodic open"
    },
    "os.listdir": {
        "description": "Directory listing polling",
        "severity": "MEDIUM",
        "recommendation": "Replace with kqueue directory watching"
    },
    "time.time()": {
        "description": "Time-based comparison (polling interval)",
        "severity": "LOW",
        "recommendation": "If used with sleep, consider event-driven alternative"
    },
    "time.sleep": {
        "description": "Sleep-based delay (polling interval)",
        "severity": "HIGH",
        "recommendation": "Replace with kqueue timer or launchd oneShot"
    },
}


# ── AST Analysis ───────────────────────────────────────────────────────────

class PollingAnalyzer(ast.NodeVisitor):
    """AST visitor that identifies polling patterns in plugin code."""

    def __init__(self, source_code):
        self.source_lines = source_code.splitlines()
        self.findings = []
        self.in_while_loop = False
        self.while_depth = 0
        self.functions = []
        self.imports = set()
        self.has_threading = False
        self.has_kqueue = False

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.name.split(".")[0]
            self.imports.add(name)
            if name == "threading":
                self.has_threading = True
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            name = node.module.split(".")[0]
            self.imports.add(name)
            if name == "kqueue" or name == "select":
                self.has_kqueue = True
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.functions.append({
            "name": node.name,
            "lineno": node.lineno,
            "args": [arg.arg for arg in node.args.args],
        })
        self.generic_visit(node)

    def visit_While(self, node):
        self.while_depth += 1
        line_text = self.source_lines[node.lineno - 1].strip() if node.lineno <= len(self.source_lines) else ""
        
        # Check if this while loop contains sleep (polling indicator)
        has_sleep = False
        has_file_check = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name) and child.func.id == "sleep":
                    has_sleep = True
                if isinstance(child.func, ast.Attribute) and child.func.attr == "sleep":
                    has_sleep = True
                if isinstance(child.func, ast.Name) and child.func.id == "exists":
                    has_file_check = True
                if isinstance(child.func, ast.Attribute) and child.func.attr == "exists":
                    has_file_check = True

        if has_sleep or line_text.startswith("while True") or line_text.startswith("while 1"):
            self.findings.append({
                "type": "polling_loop",
                "severity": "HIGH" if has_sleep else "MEDIUM",
                "line": node.lineno,
                "code": line_text,
                "description": f"Polling loop detected" + (" (with sleep)" if has_sleep else ""),
                "has_sleep": has_sleep,
                "has_file_check": has_file_check,
            })

        self.generic_visit(node)
        self.while_depth -= 1

    def visit_Call(self, node):
        # Check for known polling function calls
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name and func_name in POLLING_PATTERNS:
            line_text = self.source_lines[node.lineno - 1].strip() if node.lineno <= len(self.source_lines) else ""
            self.findings.append({
                "type": "pattern_match",
                "severity": POLLING_PATTERNS[func_name]["severity"],
                "line": node.lineno,
                "code": line_text,
                "description": POLLING_PATTERNS[func_name]["description"],
                "recommendation": POLLING_PATTERNS[func_name]["recommendation"],
            })

        # Detect time.sleep(x) with specific intervals
        if func_name == "sleep" or (func_name == "sleep" and isinstance(node.func, ast.Attribute)):
            if node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                    interval = arg.value
                    self.findings.append({
                        "type": "sleep_interval",
                        "severity": "MEDIUM" if interval <= 10 else "LOW",
                        "line": node.lineno,
                        "code": self.source_lines[node.lineno - 1].strip() if node.lineno <= len(self.source_lines) else "",
                        "description": f"time.sleep({interval}s) detected",
                        "interval_seconds": interval,
                    })

        self.generic_visit(node)

    def visit_If(self, node):
        # Detect polling condition patterns
        if self.while_depth > 0:
            # Inside a while loop — check for common polling patterns
            for child in ast.walk(node):
                if isinstance(child, ast.Compare):
                    for op in child.ops:
                        if isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE)):
                            for comparator in child.comparators:
                                if isinstance(comparator, ast.Call):
                                    if isinstance(comparator.func, ast.Name) and comparator.func.id == "time":
                                        if hasattr(comparator, 'args') and comparator.args:
                                            self.findings.append({
                                                "type": "time_based_poll",
                                                "severity": "MEDIUM",
                                                "line": node.lineno,
                                                "code": self.source_lines[node.lineno - 1].strip() if node.lineno <= len(self.source_lines) else "",
                                                "description": "Time-based comparison inside polling loop",
                                            })
        self.generic_visit(node)


def scan_file_for_patterns(filepath):
    """Scan a single Python file for polling patterns using regex and AST."""
    try:
        with open(filepath) as f:
            source = f.read()
    except (IOError, UnicodeDecodeError):
        return {"errors": ["Could not read file"]}

    findings = []
    lines = source.splitlines()

    # ── AST analysis ──
    try:
        tree = ast.parse(source)
        analyzer = PollingAnalyzer(source)
        analyzer.visit(tree)
        findings.extend(analyzer.findings)
    except SyntaxError:
        findings.append({
            "type": "parse_error",
            "severity": "LOW",
            "line": 0,
            "code": "",
            "description": "Could not parse as Python AST (may be incomplete)",
        })

    # ── Regex scan for additional patterns ──
    # Detect daemon/thread patterns
    thread_matches = re.findall(
        r'threading\.(Thread|Timer|active_count)', source, re.MULTILINE
    )
    if thread_matches:
        findings.append({
            "type": "threading_usage",
            "severity": "MEDIUM",
            "line": 0,
            "code": "",
            "description": f"Threading module used: {set(thread_matches)}",
            "recommendation": "Consider replacing with asyncio or kqueue for I/O bound work",
        })

    # Detect subprocess polling
    subprocess_matches = re.findall(
        r'subprocess\.(run|Popen|check_output)', source, re.MULTILINE
    )
    if subprocess_matches:
        # Count usages
        count = len(subprocess_matches)
        findings.append({
            "type": "subprocess_polling",
            "severity": "LOW",
            "line": 0,
            "code": "",
            "description": f"subprocess.run/popen used {count} times (each is a blocking call)",
            "recommendation": "For repeated calls, consider event-driven output capture",
        })

    # Detect JSON file polling (read in a loop)
    json_poll = re.findall(
        r'(with\s+open\s*\([^)]*json[^)]*\))', source, re.MULTILINE | re.IGNORECASE
    )
    if len(json_poll) > 3:
        findings.append({
            "type": "json_file_heavy_io",
            "severity": "MEDIUM",
            "line": 0,
            "code": "",
            "description": f"JSON file open() called {len(json_poll)} times in this file",
            "recommendation": "Consider a shared state manager with in-process caching",
        })

    return {
        "source_lines": analyzer.source_lines if 'analyzer' in locals() else lines,
        "findings": findings,
        "has_threading": analyzer.has_threading if 'analyzer' in locals() else False,
        "has_kqueue": analyzer.has_kqueue if 'analyzer' in locals() else False,
        "functions": analyzer.functions if 'analyzer' in locals() else [],
    }


def generate_kqueue_replacements(findings, filename):
    """Generate kqueue/launchd replacement code for each finding."""
    replacements = []

    for finding in findings:
        if finding["type"] not in ("polling_loop", "sleep_interval", "time_based_poll"):
            continue

        if finding["type"] == "polling_loop":
            interval = 5  # default
            for f2 in findings:
                if f2.get("interval_seconds"):
                    interval = f2["interval_seconds"]
                    break

            replacements.append({
                "finding": finding,
                "type": "kqueue_file_watch",
                "replacement_code": f"""
# ── kqueue replacement for polling loop at line {finding['line']} ──
import kqueue
import select

def create_file_watcher(paths, callback, poll_interval={interval}):
    '''Replace polling loop with kqueue-based file notification.'''
    kq = kqueue.Kevent()
    fd_map = {{}}

    for path in paths:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        fd_map[fd] = path
        # Watch for file modifications
        kq.register(fd, kqueue.EVFILT_VNODE,
                    flags=kqueue.EV_ADD | kqueue.EV_ENABLE,
                    filter=kqueue.EVFILT_VNODE,
                    fflags=kqueue.NOTE_WRITE | kqueue.NOTE_EXTEND | kqueue.NOTE_DELETE)

    timeout = poll_interval  # seconds between checks
    running = True

    while running:
        events = kq.control(None, 100, timeout)
        for event in events:
            fd = event.ident
            if event.filter == kqueue.EVFILT_VNODE:
                callback(fd_map[fd], event.fflags)
                # Re-register to continue watching
                kq.register(fd, kqueue.EVFILT_VNODE,
                           flags=kqueue.EV_ADD | kqueue.EV_ENABLE,
                           filter=kqueue.EVFILT_VNODE,
                           fflags=kqueue.NOTE_WRITE | kqueue.NOTE_EXTEND | kqueue.NOTE_DELETE)

        # Also check for timeout events (stale watches)
        for event in events:
            if event.flags & kqueue.EV_EOF or event.flags & kqueue.EV_ERROR:
                if event.ident in fd_map:
                    os.close(event.ident)
                    del fd_map[event.ident]

    for fd in fd_map:
        os.close(fd)


# Usage:
# paths_to_watch = ['{HSCC_DIR}/lifecycle.json', '{HSCC_DIR}/triggers.json']
# create_file_watcher(paths_to_watch, on_state_change, poll_interval={interval})
""",
                "notes": [
                    "kqueue is native to macOS — no pip install needed",
                    "Eliminates CPU-wasting sleep intervals",
                    "Callbacks fire immediately on file change",
                    f"Watched paths: {HSCC_DIR}/*.json",
                ]
            })

        elif finding["type"] == "sleep_interval":
            interval = finding.get("interval_seconds", 5)
            replacements.append({
                "finding": finding,
                "type": "launchd_timer",
                "replacement_code": f"""
# ── launchd oneShot timer replacement for sleep({interval}) ──
import subprocess
import plistlib
from pathlib import Path

def setup_launchd_timer(label, interval, program_args):
    '''Create a launchd oneShot job that fires every {interval} seconds.'''
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)

    plist_path = plist_dir / f"{{label}}.plist"

    plist_content = {{
        "Label": label,
        "ProgramArguments": program_args,
        "StartInterval": {interval},
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(plist_dir / f"{{label}}.log"),
        "StandardErrorPath": str(plist_dir / f"{{label}}.err"),
    }}

    with open(plist_path, "wb") as f:
        f.write(plistlib.dumps(plist_content))

    subprocess.run(["launchctl", "load", str(plist_path)])
    subprocess.run(["launchctl", "start", label])

    return str(plist_path)


# Usage:
# setup_launchd_timer(
#     "com.nousresearch.hscc.{filename.stem}.check",
#     {interval},
#     ["/usr/bin/python3", "path/to/hscc-check.py"]
# )
""",
                "notes": [
                    "launchd is macOS native — no cron needed",
                    "More reliable than cron for sub-minute intervals",
                    "Automatic restart on failure",
                    f"Interval: {interval}s",
                ]
            })

    return replacements


def analyze_plugin(plugin_dir):
    """Analyze a single HSCC plugin directory."""
    hscc_py = os.path.join(plugin_dir, "hscc.py")
    if not os.path.exists(hscc_py):
        return None

    name = os.path.basename(plugin_dir)
    result = scan_file_for_patterns(hscc_py)
    result["plugin"] = name
    result["path"] = hscc_py
    return result


def analyze_all_plugins():
    """Scan all HSCC plugins in the plugins directory."""
    results = []
    patterns = defaultdict(list)

    if not os.path.isdir(HSCC_PLUGINS_DIR):
        print(f"[ERROR] HSCC plugins directory not found: {HSCC_PLUGINS_DIR}")
        sys.exit(1)

    for entry in sorted(os.listdir(HSCC_PLUGINS_DIR)):
        plugin_dir = os.path.join(HSCC_PLUGINS_DIR, entry)
        if not os.path.isdir(plugin_dir) or not entry.startswith("hscc-"):
            continue

        result = analyze_plugin(plugin_dir)
        if result:
            results.append(result)
            for f in result["findings"]:
                patterns[f["type"]].append(entry)

    return results, patterns


def generate_report(results, patterns):
    """Generate a human-readable analysis report."""
    lines = []
    lines.append("=" * 78)
    lines.append("  HSCC EVENT-DRIVEN PATTERN ANALYSIS REPORT")
    lines.append(f"  Generated: {datetime.now().isoformat()}")
    lines.append(f"  Plugins scanned: {len(results)}")
    lines.append("=" * 78)
    lines.append("")

    # ── Summary ──
    total_findings = sum(len(r["findings"]) for r in results)
    high_sev = sum(1 for r in results for f in r["findings"] if f["severity"] == "HIGH")
    med_sev = sum(1 for r in results for f in r["findings"] if f["severity"] == "MEDIUM")
    low_sev = sum(1 for r in results for f in r["findings"] if f["severity"] == "LOW")

    lines.append("SUMMARY")
    lines.append("-" * 78)
    lines.append(f"  Total findings:    {total_findings}")
    lines.append(f"  HIGH severity:     {high_sev}  (replace with kqueue/launchd)")
    lines.append(f"  MEDIUM severity:   {med_sev}  (consider optimization)")
    lines.append(f"  LOW severity:      {low_sev}  (nice-to-improve)")
    lines.append("")

    # ── Pattern Distribution ──
    if patterns:
        lines.append("PATTERN DISTRIBUTION")
        lines.append("-" * 78)
        for ptype, plugins in sorted(patterns.items(), key=lambda x: -len(x[1])):
            lines.append(f"  {ptype:30s} → {len(plugins)} plugin(s): {', '.join(plugins)}")
        lines.append("")

    # ── Per-Plugin Details ──
    lines.append("PER-PLUGIN ANALYSIS")
    lines.append("=" * 78)

    for result in results:
        plugin = result["plugin"]
        findings = [f for f in result["findings"] if f.get("line", 0) > 0]
        if not findings:
            continue

        lines.append(f"\n{'─' * 78}")
        lines.append(f"  PLUGIN: {plugin}")
        lines.append(f"  PATH: {result['path']}")
        lines.append(f"  FUNCTIONS: {', '.join(f['name'] for f in result.get('functions', []))}")
        lines.append(f"  THREADING: {'Yes' if result.get('has_threading') else 'No'}")
        lines.append(f"  KQUEUE: {'Yes' if result.get('has_kqueue') else 'No'}")
        lines.append("")

        for finding in sorted(findings, key=lambda f: f.get("line", 0)):
            sev = finding["severity"]
            sev_marker = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            lines.append(f"  {sev_marker} Line {finding['line']:4d} [{sev:6s}] {finding['type']}")
            lines.append(f"      Code: {finding.get('code', '')[:80]}")
            lines.append(f"      {finding['description']}")
            if "recommendation" in finding:
                lines.append(f"      → {finding['recommendation']}")
            if "interval_seconds" in finding:
                lines.append(f"      → Sleep interval: {finding['interval_seconds']}s")
            lines.append("")

    # ── Recommendations ──
    lines.append("")
    lines.append("=" * 78)
    lines.append("  RECOMMENDED OPTIMIZATIONS")
    lines.append("=" * 78)
    lines.append("")

    recommendations = []

    if high_sev > 0:
        recommendations.append("""
1. 🔄  KQUEUE FILE WATCHER (macOS native)
   ─────────────────────────────────────────────────────────
   Replace sleep-based polling loops with kqueue EVFILT_VNODE watchers.

   Benefits:
   • Immediate response to file changes (no sleep interval delay)
   • Near-zero CPU usage (event-driven, not polling)
   • macOS native — no pip install required

   Apply to: {plugins}

   Example:
   import kqueue
   kq = kqueue.Kevent()
   kq.register(fd, kqueue.EVFILT_VNODE,
               flags=kqueue.EV_ADD | kqueue.EV_ENABLE,
               filter=kqueue.EVFILT_VNODE,
               fflags=kqueue.NOTE_WRITE)

   events = kq.control(None, 100, timeout=None)  # blocks until event
   for ev in events:
       on_file_changed(ev)  # callback fires immediately
""")

        high_plugins = set()
        for r in results:
            for f in r["findings"]:
                if f["severity"] == "HIGH":
                    high_plugins.add(r["plugin"])
        recommendations[-1] = recommendations[-1].format(plugins=", ".join(high_plugins))

    recommendations.append("""
2. ⏱  LAUNCHD ONE-SHOT TIMER (macOS native)
   ─────────────────────────────────────────────────────────
   For check cycles that must run periodically (not on file change):
   Replace time.sleep(n) + loop with launchd StartInterval.

   Benefits:
   • System-managed scheduling (survives sleep, reboots)
   • Sub-minute intervals (cron minimum is 1 minute)
   • Automatic restart on failure

   Apply to: daemon check streams (dgx every 5s, gateway every 10s)
""")

    recommendations.append("""
3. 📦  IN-PROCESS STATE CACHE
   ─────────────────────────────────────────────────────────
   All plugins read JSON state files on every command invocation.
   Add a module-level LRU cache in hscc-core/state.py:

   from functools import lru_cache
   import time

   _cache = {}
   _cache_ttl = 5  # seconds

   def read_json_cached(path, default=None):
       now = time.time()
       cached = _cache.get(path)
       if cached and now - cached['ts'] < _cache_ttl:
           return cached['data']
       with open(path) as f:
           data = json.load(f)
       _cache[path] = {'data': data, 'ts': now}
       return data

   Benefits:
   • 5s TTL — near-realtime freshness
   • Reduces I/O on frequent commands
   • Atomic writes invalidate cache automatically
""")

    recommendations.append("""
4. 🔔  EVENT BUS ARCHITECTURE (future)
   ─────────────────────────────────────────────────────────
   For cross-plugin coordination (currently each plugin polls state):

   When lifecycle.json changes → notify all plugins that care
   When agents.json changes → notify orchestrator + projects
   When triggers.json changes → notify daemon check loop

   Use file-watcher events as the signal, not time-based polling.
""")

    for i, rec in enumerate(recommendations, 1):
        lines.append(rec)

    lines.append("")
    lines.append("=" * 78)
    lines.append("  END OF REPORT")
    lines.append("=" * 78)

    return "\n".join(lines)


def generate_json_report(results, patterns):
    """Generate a machine-readable JSON report."""
    report = {
        "scan_time": datetime.now().isoformat(),
        "plugins_scanned": len(results),
        "total_findings": sum(len(r["findings"]) for r in results),
        "high_severity": sum(
            1 for r in results for f in r["findings"] if f["severity"] == "HIGH"
        ),
        "medium_severity": sum(
            1 for r in results for f in r["findings"] if f["severity"] == "MEDIUM"
        ),
        "low_severity": sum(
            1 for r in results for f in r["findings"] if f["severity"] == "LOW"
        ),
        "plugin_details": [],
        "pattern_distribution": dict(patterns),
    }

    for r in results:
        plugin_report = {
            "name": r["plugin"],
            "path": r["path"],
            "functions": r.get("functions", []),
            "has_threading": r.get("has_threading", False),
            "has_kqueue": r.get("has_kqueue", False),
            "findings": [
                {
                    "type": f["type"],
                    "severity": f["severity"],
                    "line": f.get("line", 0),
                    "code": f.get("code", ""),
                    "description": f["description"],
                    "recommendation": f.get("recommendation", ""),
                }
                for f in r["findings"]
            ],
        }
        report["plugin_details"].append(plugin_report)

    return json.dumps(report, indent=2)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="HSCC Event-Driven Pattern Detector — find polling patterns and suggest kqueue/launchd replacements"
    )
    parser.add_argument(
        "plugin_dir", nargs="?", default=None,
        help="Path to HSCC plugins directory (default: ~/.hermes/plugins)"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output machine-readable JSON report"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Write report to file instead of stdout"
    )

    args = parser.parse_args()

    scan_dir = args.plugin_dir or HSCC_PLUGINS_DIR

    if not os.path.isdir(scan_dir):
        print(f"[ERROR] Directory not found: {scan_dir}")
        sys.exit(1)

    results, patterns = analyze_all_plugins()

    if args.json_output:
        report = generate_json_report(results, patterns)
    else:
        report = generate_report(results, patterns)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"[OK] Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
