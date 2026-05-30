"""Shell-out helper: invoke an HSCC CLI plugin and return a structured result."""
import json
import subprocess
import sys
from pathlib import Path

# hscc-mcp/ -> parent is plugins/, where the hscc-* plugins live.
PLUGINS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 60


def _plugin_script(plugin: str) -> str:
    return str(PLUGINS_DIR / plugin / "hscc.py")


def run_hscc(plugin: str, *args: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run ``python <plugin>/hscc.py <args...>`` and return a structured dict.

    Returns keys: ok(bool), exit_code(int|None), stdout(str), stderr(str),
    json(parsed|None), error(str|None).
    """
    argv = [sys.executable, _plugin_script(plugin), *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "exit_code": None, "stdout": "", "stderr": "",
            "json": None, "error": f"timeout after {timeout}s running {plugin} {args}",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False, "exit_code": None, "stdout": "", "stderr": "",
            "json": None, "error": f"plugin not found: {exc}",
        }

    parsed = None
    stdout = proc.stdout or ""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": proc.stderr or "",
        "json": parsed,
        "error": None,
    }
