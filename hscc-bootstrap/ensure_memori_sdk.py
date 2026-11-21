"""Keep the memori SDK in every Hermes-managed Python environment (t_4b0e7956).

Hermes runs its gateway, REPLs and workers from package-manager environments
(``$HERMES_HOME/installs/<id>/environments/<env>/venv``), not from
``hermes-agent/venv``. Those environments are rebuilt on update and never carry
the memori SDK, so the memori_byodb memory provider failed to start on every
session ("Memori SDK dependency missing: memori") and no profile had memory.

The SDK cannot be declared in the plugin manifest: memori 3.3.6 (the latest)
pins ``protobuf<6`` while Hermes's Google libraries need ``protobuf>=6.33.5``,
so a resolver build would refuse the whole environment. The local BYODB path
(SQLite + fastembed + the local augmentation LLM) never imports protobuf, so the
SDK goes in with ``--no-deps`` next to its other runtime dependencies, pinned.

  ensure_memori_sdk.py            report what is missing (JSON)
  ensure_memori_sdk.py --apply    install where missing
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

#: memori's runtime dependencies other than protobuf; every other requirement it
#: declares (aiohttp, botocore, numpy, requests) is already in a Hermes env.
PINS = ("memori==3.3.6", "faiss-cpu==1.16.0", "grpcio==1.85.3", "pyfiglet==1.0.4")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=600)  # noqa: S603


def environments(hermes_home: Path) -> list[Path]:
    """Interpreters of every managed environment, current and side generations.

    Install roots marked ``test-environment`` belong to Hermes's own test runs
    (worktrees, CI checkouts), not to the live install: they are left alone.
    """
    found = sorted(hermes_home.glob("installs/*/environments/*/venv/bin/python"))
    return [p for p in found if p.exists() and not (p.parents[4] / "test-environment").exists()]


def has_sdk(python: Path, run: Runner = _run) -> bool:
    res = run([str(python), "-I", "-c", "import memori.embeddings"])
    return res.returncode == 0


def install(python: Path, run: Runner = _run, uv: str | None = None) -> subprocess.CompletedProcess:
    uv = uv if uv is not None else shutil.which("uv")
    if uv:
        return run([uv, "pip", "install", "--quiet", "--python", str(python), "--no-deps", *PINS])
    return run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", *PINS])


def ensure(hermes_home: Path, *, apply: bool, run: Runner = _run, uv: str | None = None) -> dict:
    report: dict = {"environments": [], "ok": True}
    for python in environments(hermes_home):
        env = {"python": str(python)}
        if has_sdk(python, run):
            env["status"] = "present"
        elif not apply:
            env["status"] = "missing"
        else:
            res = install(python, run, uv)
            if res.returncode == 0 and has_sdk(python, run):
                env["status"] = "installed"
            else:
                env["status"] = "failed"
                env["error"] = (res.stderr or res.stdout or "").strip()[-400:]
                report["ok"] = False
        report["environments"].append(env)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="install where missing (default: report only)")
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    args = ap.parse_args(argv)
    report = ensure(Path(args.hermes_home).expanduser(), apply=args.apply)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
