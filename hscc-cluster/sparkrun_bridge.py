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


# ── proxy.yaml WRITE path (spec §4.4) ──────────────────────────────────────
#
# `docs/design/2026-10-15-template-declarations.md` §2.3/§4.4 is explicit: hscc
# never opens ``~/.config/sparkrun/proxy.yaml`` for write and never touches the
# regenerated ``~/.cache/sparkrun/proxy/litellm_config.yaml``. What it may do is
# hand sparkrun the regions its template declares and let sparkrun merge them.
#
# Two findings from reading sparkrun 0.4.0 (the live build: `sparkrun` ->
# ``.venv-sparkrun-040`` -> editable ``~/dev/sparkrun-live`` on
# ``feat/proxy-least-in-flight-040``) shape this:
#
#   * ``ProxyConfig.save`` (``src/sparkrun/proxy/config.py:112``) is exactly the
#     sanctioned merge-save §4.4 asks for — a writer lock, a re-read of the
#     newest document, and only the pending keys applied.
#   * BUT its pending mechanism covers only two top-level sections: the private
#     ``_pending_proxy`` fed by ``set_proxy`` and ``_pending_aliases`` fed by
#     ``add_alias``. There is NO setter for ``gateways.*``: ``gateway_settings``
#     (``config.py:293``) is READ-ONLY ("sparkrun neither validates it nor
#     WRITES it"). So ``gateways.litellm.balancing`` — the only region apply
#     must write today (§2.2) — has no contract yet. That gap is §4.4 "Open —
#     config write API surface", restated with evidence in this card's report.
#
# Until it lands, the ONLY code path that writes a `gateways.` region through
# sparkrun's own module is `ProxyConfig._write_atomic` under
# `ProxyConfig._write_lock()`. That is a private-by-name path, so it is treated
# as such: kept in one function, in the ONE module allowed to speak to sparkrun,
# and pinned by `test_sparkrun_write_path.py::test_private_write_path_contract`
# — if sparkrun renames it, that test fails loudly instead of apply degrading
# into a hand-written yaml file.
_PROXY_WRITE_SCRIPT = (
    "import json,sys\n"
    "from sparkrun.proxy.config import ProxyConfig\n"
    "PC=ProxyConfig()\n"
    "regions=json.loads(sys.argv[1])\n"
    "mode=sys.argv[2]\n"
    "def _merge(dst,src):\n"
    "    for k,v in src.items():\n"
    "        if isinstance(v,dict) and isinstance(dst.get(k),dict):\n"
    "            _merge(dst[k],v)\n"
    "        else:\n"
    "            dst[k]=v\n"
    "before=PC._read(strict=True)\n"
    "if mode=='pending':\n"
    "    PC.set_proxy(**regions['proxy'])\n"
    "    PC.save()\n"
    "else:\n"
    "    # Read-modify-write INSIDE sparkrun's own writer lock, the same\n"
    "    # ordering ProxyConfig.save() uses, so a concurrent discovery sweep\n"
    "    # cannot be clobbered.\n"
    "    with PC._write_lock():\n"
    "        latest=PC._read(strict=True)\n"
    "        for sec,reg in regions.items():\n"
    "            _merge(latest.setdefault(sec,{}),reg)\n"
    "        PC._write_atomic(latest)\n"
    "after=PC._read()\n"
    "print(json.dumps({'ok':True,'before':before,'after':after}))\n"
)


def proxy_config_write(regions: Dict[str, Any], *, _runner=None) -> Dict[str, Any]:
    """Merge *regions* into ``~/.config/sparkrun/proxy.yaml`` via sparkrun.

    ``regions`` maps a top-level proxy.yaml section to a (possibly nested)
    mapping, e.g. ``{"gateways": {"litellm": {"balancing":
    "least-in-flight"}}}``. The merge is DEEP, so an apply never drops a key
    another writer owns: writing the balancing knob cannot erase the bind host,
    the master key, an alias, or another gateway's namespace.

    Runs under sparkrun's OWN venv python (``_sparkrun_venv_python``, the same
    resolution ``hscc_daemon/health.py`` uses for its status script), so the
    file format, the writer lock and the atomic replace are all sparkrun's code.
    Nothing here opens proxy.yaml.

    ``mode='pending'`` (sparkrun's public merge-save, used only for the
    ``proxy:`` section) is preferred; ``mode='direct'`` is the private-
    ``_write_atomic`` path required for sections sparkrun has no setter for —
    see the module comment.

    Fail-closed: no sparkrun interpreter, a non-zero exit, or unparseable output
    all return ``{"ok": False, ...}`` and write NOTHING. It never fabricates
    success, and the caller must surface the failure rather than fall back to
    writing the file itself.

    ``_runner(cmd, timeout) -> CompletedProcess``-shaped is injectable for tests
    (t_ac795424 doctrine: no real sparkrun, no real file, in the suite).
    """
    if not isinstance(regions, dict) or not regions:
        return {"ok": False, "error": "no regions to write", "changed": False,
                "before": None, "after": None}
    for sec, reg in regions.items():
        if not isinstance(sec, str) or not sec or not isinstance(reg, dict):
            return {"ok": False,
                    "error": f"region {sec!r}: expected a mapping", "changed": False,
                    "before": None, "after": None}

    run = _runner or _run_venv_script
    mode = "pending" if set(regions) == {"proxy"} else "direct"
    payload = json.dumps(regions, sort_keys=True)
    try:
        proc = run(payload, mode)
    except Exception as e:  # interpreter missing / spawn failure
        return {"ok": False, "error": f"sparkrun write path unreachable: {e}",
                "changed": False, "before": None, "after": None}
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "")
               or "sparkrun proxy config write failed")
        return {"ok": False, "error": str(err).strip(), "changed": False,
                "before": None, "after": None}
    try:
        data = json.loads(getattr(proc, "stdout", "") or "")
    except (TypeError, ValueError) as e:
        return {"ok": False,
                "error": f"sparkrun write path output not parseable: {e}",
                "changed": False, "before": None, "after": None}
    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False, "error": "sparkrun write path reported failure",
                "changed": False, "before": None, "after": None}
    before, after = data.get("before"), data.get("after")
    return {"ok": True, "error": None, "mode": mode,
            "changed": before != after, "before": before, "after": after}


def _run_venv_script(payload: str, mode: str, timeout: int = 30):
    """Real runner: the write script under sparkrun's venv python (no shell)."""
    import subprocess
    venv_py = _sparkrun_venv_python()
    if not venv_py:
        raise FileNotFoundError("sparkrun not on PATH")
    return subprocess.run([venv_py, "-c", _PROXY_WRITE_SCRIPT, payload, mode],
                          capture_output=True, text=True, timeout=timeout)
