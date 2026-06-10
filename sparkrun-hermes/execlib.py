"""sparkrun CLI passthrough for Hermes agents.

Mirrors the official OpenClaw plugin's design: a single guarded tool that runs
any ``sparkrun ...`` command. The agent learns *which* commands to run from the
bundled skills (run/setup/registry); this layer is just a safe executor.

Guard: the command MUST start with ``sparkrun`` so the tool can't be used as a
general shell. Output is captured (stdout+stderr), never raises.
"""
import shlex
import subprocess

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800


def sparkrun_exec(args, **kwargs):
    """Execute a `sparkrun ...` command. Returns a result dict.

    args: {"command": "sparkrun ...", "timeout"?: seconds}
    """
    command = (args.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "command is required (e.g. 'sparkrun status')."}
    if not command.startswith("sparkrun"):
        return {"ok": False,
                "error": "Command must start with 'sparkrun'. This tool only "
                         "runs sparkrun CLI commands; use a terminal tool for "
                         "anything else."}

    timeout = args.get("timeout") or DEFAULT_TIMEOUT
    try:
        timeout = min(int(timeout), MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    try:
        argv = shlex.split(command)
    except ValueError as e:
        return {"ok": False, "error": f"could not parse command: {e}"}

    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "sparkrun not found on PATH. Is sparkrun installed?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"sparkrun command timed out after {timeout}s",
                "command": command}
    except OSError as e:
        return {"ok": False, "error": str(e), "command": command}

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    return {
        "ok": r.returncode == 0,
        "exit_code": r.returncode,
        "command": command,
        "stdout": out,
        "stderr": err or None,
    }
