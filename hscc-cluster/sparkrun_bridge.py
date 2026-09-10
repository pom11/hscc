"""Route hscc-cluster's fleet SSH through sparkrun — never hand-rolled ssh.

hscc-cluster must rely on sparkrun for the fleet and must not reimplement what
it owns. sparkrun owns host resolution, the ssh kwargs (user, key, port, host
aliases, timeouts) and the cache dir. Hand-rolled ``ssh -o BatchMode=yes -o
ConnectTimeout=8 user@host <cmd>`` drifts from whatever sparkrun is configured
to do and fails in ways that are invisible until production (t_17cdc637).

We cannot ``import sparkrun`` in this process: hscc-cluster runs under the
Hermes agent venv, and sparkrun lives (with its transitive deps) only in
sparkrun's dedicated venv. So, exactly like hscc_daemon/health.py's
``_sparkrun_venv_python``, we resolve sparkrun's own interpreter from the
``sparkrun`` CLI binary's shebang and run a small script under it that drives
sparkrun's sanctioned remote-execution API with sparkrun's OWN ssh kwargs.

The bridge surfaces ONE function, ``remote_cmd(host, command, timeout)``, that
returns the same dict shape ``run_cmd``/``ssh_cmd`` historically produced
(``{ok, stdout, stderr, code}``) so every existing consumer (clusterlib,
discovery, heal, debug) keeps working. Tests inject a fake here — the sparkrun
seam — and never ssh anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

# Inline script run under sparkrun's OWN venv python. It resolves sparkrun's
# ssh kwargs (SparkrunConfig -> build_ssh_kwargs) and drives sparkrun's
# remote-execution primitive (run_remote_script), emitting structured JSON.
# The command is piped as a bash script via stdin (ssh host bash -s) exactly
# as sparkrun itself runs remote commands — no local shell-quoting drift.
_REMOTE_SCRIPT = (
    "import json,sys\n"
    "from sparkrun.core.config import SparkrunConfig\n"
    "from sparkrun.orchestration.primitives import build_ssh_kwargs\n"
    "from sparkrun.orchestration.ssh import run_remote_script\n"
    "config=SparkrunConfig()\n"
    "ssh_kwargs=build_ssh_kwargs(config)\n"
    "r=run_remote_script(__HOST__, __CMD__, timeout=__TIMEOUT__, **ssh_kwargs)\n"
    "print(json.dumps({'ok': r.success, 'stdout': r.stdout, 'stderr': r.stderr, 'code': r.returncode}))\n"
)


def _sparkrun_venv_python() -> Optional[str]:
    """Return the python interpreter that owns the ``sparkrun`` CLI.

    Resolved from the ``sparkrun`` executable's shebang (``#!/path/to/python``)
    so we reuse sparkrun's own venv — where the sparkrun package and its
    transitive deps actually live. Returns None if ``sparkrun`` is not on PATH.
    """
    sparkrun_bin = shutil.which("sparkrun")
    if not sparkrun_bin:
        return None
    try:
        with open(sparkrun_bin, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
        if first.startswith("#!"):
            interp = first[2:].strip()
            if interp and (shutil.which(interp) is not None or os.path.exists(interp)):
                return interp
    except OSError:
        pass
    return None


def remote_cmd(host: str, command: str, timeout: int = 30) -> Dict[str, Any]:
    """Run ``command`` on ``host`` through sparkrun's own remote-execution.

    Resolves sparkrun's venv python, drives ``run_remote_script`` with
    sparkrun's ssh kwargs, and returns ``{ok, stdout, stderr, code}`` — the
    shape clusterlib's ``ssh_cmd``/``run_cmd`` historically produced.

    If sparkrun is genuinely unreachable (no CLI / no interpreter / script
    failure) the call fails open to an unreachable result, exactly like the
    old transport failing — it never fabricates output.
    """
    venv_py = _sparkrun_venv_python()
    if not venv_py:
        return {"ok": False, "stdout": "", "stderr": "sparkrun not on PATH", "code": 127}

    script = (_REMOTE_SCRIPT
              .replace("__HOST__", json.dumps(host))
              .replace("__CMD__", json.dumps(command))
              .replace("__TIMEOUT__", str(int(timeout))))
    try:
        r = subprocess.run([venv_py, "-c", script], capture_output=True, text=True,
                           timeout=timeout + 15)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "code": 127}

    if r.returncode != 0:
        return {"ok": False, "stdout": r.stdout, "stderr": r.stderr or "sparkrun remote script failed",
                "code": r.returncode}
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return {"ok": False, "stdout": r.stdout, "stderr": f"sparkrun bridge JSON parse failed: {e}",
                "code": 127}
    # Normalize to the legacy dict shape (always carry all four keys).
    return {
        "ok": bool(data.get("ok")),
        "stdout": data.get("stdout") or "",
        "stderr": data.get("stderr") or "",
        "code": data.get("code") if data.get("code") is not None else (0 if data.get("ok") else 1),
    }


def ssh_cmd(host: str, command: str, timeout: int = 30) -> Dict[str, Any]:
    """Route a remote command through sparkrun (back-compat alias)."""
    return remote_cmd(host, command, timeout=timeout)
