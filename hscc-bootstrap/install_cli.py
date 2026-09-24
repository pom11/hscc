"""Install the `hscc` CLI entry point into the Hermes venv — durably.

The `hscc` CLI runs operator-facing verbs that read the Hermes runtime from the
SAME interpreter: `hscc project chat/sessions/ask` import ``hermes_cli`` /
``hermes_state`` (which live ONLY in the Hermes venv), and `hscc check` needs
the macOS Local Network (TCC) grant that the Hermes venv carries. Installing
the console script into any OTHER interpreter (e.g. a bare conda p313) bakes a
wrong shebang that silently degrades those verbs:

    HermesRuntimeUnavailable: cannot import the Hermes runtime
    (No module named 'hermes_cli'). The `hscc` CLI runs under
    /Users/desac/miniconda3/envs/p313/bin/python3.13

The durable fix is to install ``hscc-cli`` INTO the Hermes venv as part of
bootstrap. ``hscc-cli/pyproject.toml`` declares ``hscc = "hscc_cli:main"``, so
pip/uv generates the console script with the venv's interpreter baked into the
shebang BY CONSTRUCTION — no hand-editing, survives every reinstall.

Idempotent: a normal install is a no-op when the requirement is already
satisfied (it does not rebuild the console script, so a correct shebang is
preserved). We then VERIFY the resolved entry point's shebang; only if it
drifted do we force-reinstall. Every run reports a structured status so
bootstrap can SAY what it changed — a silent rewrite is how this bug hid.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

def _expected_shebang(venv_python: Path) -> str:
    return f"#!{venv_python}"


def _resolve_entry(venv_python: Path) -> Path:
    """The `hscc` console script lives in the venv's bin/ (Scripts on win)."""
    bin_dir = venv_python.parent
    cand = bin_dir / "hscc"
    if os.name == "nt":
        cand = bin_dir / "hscc.exe"
    return cand


def _shebang_of(entry: Path):
    """First line of the console script, or None if unreadable/absent."""
    try:
        first = entry.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first


def _run(cmd, *, env=None, capture=True):
    """Run a subprocess; return (returncode, captured output or None)."""
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True, env=env)
        out = p.stdout if capture else None
        if p.returncode != 0 and capture:
            out = (out or "") + (p.stderr or "")
        return p.returncode, out
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


def _pip_cmd(venv_python, cli_dir, *, force=False):
    """The install command. `uv` is the package manager (the venv is uv-created)."""
    cmd = ["uv", "pip", "install", "--no-deps",
           "--python", str(venv_python), str(cli_dir)]
    if force:
        # `--reinstall` forces a re-run so pip/uv regenerates the console script
        # with the correct shebang (a plain run is a no-op once satisfied).
        cmd.insert(3, "--reinstall")
    return cmd


def install_cli(venv_python, cli_dir, *, runner=None):
    """Ensure `hscc-cli` is installed into the Hermes venv with the right shebang.

    Parameters
    ----------
    venv_python : PathLike
        The Hermes venv's python (``$PYBIN`` in bootstrap.sh).
    cli_dir : PathLike
        The repo's ``hscc-cli`` package directory (the pip source).
    runner : callable, optional
        For tests: ``runner(cmd, env) -> (returncode, output)``. Defaults to a
        real subprocess call to ``uv pip install``.

    Returns a status dict::

        {"action": installed|verified|repaired|failed,
         "entry": "<path to hscc console script>",
         "shebang": "<current shebang or None>",
         "expected": "<expected shebang>",
         "decision": "human-readable summary",
         "error": "..."}   # only when failed
    """
    venv_python = Path(venv_python).expanduser()  # do NOT .resolve(): a venv's
    cli_dir = Path(cli_dir).expanduser().resolve()  # bin/python may symlink to the
    # uv-managed base; resolving it would make uv target the externally-managed
    # base instead of the venv (uv refuses that). Keep the literal venv path so
    # `uv pip install --python` detects the venv via its pyvenv.cfg.
    run = runner or _run

    entry = _resolve_entry(venv_python)
    expected = _expected_shebang(venv_python)

    existed_before = entry.exists()

    # 1. Plain install first (no-op when already satisfied; preserves shebang).
    rc, out = _run_pip(run, venv_python, cli_dir, force=False)
    if rc != 0:
        return {"action": "failed", "entry": str(entry),
                "shebang": _shebang_of(entry), "expected": expected,
                "error": (out or f"uv pip install exited {rc}").strip()}

    # 2. Verify the resolved entry point carries the venv shebang.
    current = _shebang_of(entry)
    if current == expected:
        action = "installed" if not existed_before else "verified"
        return {"action": action, "entry": str(entry), "shebang": current,
                "expected": expected,
                "decision": "shebang points at the Hermes venv; nothing to repair"}

    # 3. Drift → force-reinstall so pip regenerates the script w/ venv shebang.
    rc, out = _run_pip(run, venv_python, cli_dir, force=True)
    current = _shebang_of(entry)
    if rc == 0 and current == expected:
        return {"action": "repaired", "entry": str(entry), "shebang": current,
                "expected": expected,
                "decision": "shebang drifted; force-reinstalled into the Hermes venv"}
    return {"action": "failed", "entry": str(entry), "shebang": current,
            "expected": expected,
            "error": (out or f"force-reinstall gave shebang {current!r}").strip()}


def _run_pip(run, venv_python, cli_dir, *, force):
    return run(_pip_cmd(venv_python, cli_dir, force=force))


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    if len(args) != 2:
        print("usage: install_cli.py <venv-python> <hscc-cli-dir>", file=sys.stderr)
        sys.exit(2)
    result = install_cli(args[0], args[1])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["action"] in ("installed", "verified", "repaired")
             else 1)
