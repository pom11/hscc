#!/usr/bin/env python3
"""Fail when a process whose ``sys.path`` contains a repo/deploy root cannot
import the installed ``memori`` SDK (t_2ab0eda1).

WHY THIS EXISTS
---------------
This repo stages TWO provider packages at its root: ``memori/`` (the Memori
*cloud* Hermes provider) and ``memori_byodb/`` (the BYODB provider). The first
is a package named ``memori`` — the same import name as the third-party SDK the
providers themselves need (``memori/client.py:30`` and
``memori_byodb/_client.py:90`` both run ``from memori import Memori``). Any
``sys.path`` entry holding ``memori/`` therefore shadows the SDK for that
process, and because the SDK's own ``__init__`` performs absolute
intra-package imports (95 files do), the shadowing is total: the SDK cannot be
imported *at all* while the provider owns the name — it is not a partial or
degraded import. Measured, with both roots, in
``docs/audits/memori-sdk-shadowing-t_2ab0eda1.md``.

Hermes never hits it (it loads a directory plugin as
``hermes_plugins.<slug>``, so ``sys.modules["memori"]`` stays the SDK), which is
exactly why the hazard stayed invisible until a child process was pointed at the
repo root.

WHAT IS ALREADY REAL (measured at b87ad6b5)
-------------------------------------------
``scripts/run_tests.sh:247`` runs ``"$PY" -m pytest ...`` with the repo root as
cwd, and ``python -m`` prepends the cwd to ``sys.path`` — so **every leg of the
canonical suite runs with the repo root on ``sys.path``** and cannot import the
SDK in process today. The suite's current defence is to spawn a child whose path
holds only a one-symlink shim dir (``memori_byodb/tests/test_cache_env.py::
_provider_shim_dir``, ``scripts/verify_fastembed_cache_dir.py``), which is why
those exist and why this check must not be "simplified" away.

CONTRACT
--------
One child per case. Each child resolves ``memori``, imports two public SDK
symbols, and reports where the name resolved. A case PASSES when both symbols
import — i.e. the child really reached the SDK — and the ``resolved`` path it
reports is the evidence of who won the name. Exit 0 = every applicable case
passed, 1 = at least one case cannot import the SDK (i.e. it is shadowed), 2 = the
environment cannot decide (the SDK is not importable even in the control, so no
verdict about shadowing is available).

Usage:
    python3 scripts/verify_memori_sdk_shadowing.py [--cases a,b] [--path DIR]
                                                   [--skip-control] [--json]

``--path`` adds an arbitrary root (repeatable) and is what the guard test uses to
drive this check over a tmp fixture, so the check's own logic is covered
hermetically without depending on this host having the SDK installed;
``--skip-control`` drops the real-SDK case for exactly that reason.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

#: The public SDK names the providers import. Literals on purpose: importing the
#: provider to discover them would be the very import this check is about.
SDK_SYMBOLS = ("Memori", "embed_texts")

#: What the child prints on success for a symbol.
OK = "OK"

_CHILD = """
import json, os
out = {"path_entries": [p for p in __import__("sys").path if p]}
try:
    import memori
    out["resolved"] = getattr(memori, "__file__", None)
except Exception as exc:  # noqa: BLE001
    out["resolved"] = None
    out["import_memori"] = "%s: %s" % (type(exc).__name__, exc)
else:
    out["import_memori"] = "OK"
for name in json.loads(os.environ["PROBE_SYMBOLS"]):
    try:
        exec("from memori import " + name, {})
    except Exception as exc:  # noqa: BLE001
        out[name] = "%s: %s" % (type(exc).__name__, exc)
    else:
        out[name] = "OK"
print(json.dumps(out))
"""


def _child_env(pythonpath: str | None, base: dict[str, str]) -> dict[str, str]:
    """A minimal child env: only what an interpreter needs, plus one path entry."""
    env = {
        "HOME": base.get("HOME", str(Path.home())),
        "PATH": base.get("PATH", os.defpath),
        "SYSTEMROOT": base.get("SYSTEMROOT", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROBE_SYMBOLS": json.dumps(list(SDK_SYMBOLS)),
    }
    for var in ("VIRTUAL_ENV", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG"):
        if var in base:
            env[var] = base[var]
    env = {k: v for k, v in env.items() if v}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


def run_case(name: str, root: str | None, cwd: Path | None,
             pythonpath: str | None) -> dict:
    """Run one child and judge whether the SDK was reachable as ``memori``.

    ``root`` is the directory whose ``memori/`` (if any) must NOT win the name.
    ``pythonpath`` is what the child gets on ``PYTHONPATH``; ``cwd`` is its
    working directory — ``python -c`` prepends the cwd too, which is how the
    suite shape (``python -m pytest`` from the repo root) reproduces.
    """
    with tempfile.TemporaryDirectory(prefix="memori-shadow-") as box:
        env = _child_env(pythonpath, dict(os.environ))
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            cwd=str(Path(cwd) if cwd else Path(box)),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    out: dict = {"case": name, "root": root, "returncode": proc.returncode}
    if proc.returncode != 0:
        out["verdict"] = "FAIL"
        out["detail"] = "child exited %d: %s" % (
            proc.returncode, str(proc.stderr or "").strip().splitlines()[-1:])
        return out
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        out["verdict"] = "FAIL"
        out["detail"] = "unparseable child output (%s): %r" % (exc, proc.stdout[:200])
        return out
    out.update({
        "resolved": data.get("resolved"),
        "symbols": {s: data.get(s) for s in SDK_SYMBOLS},
    })
    reasons = []
    if data.get("import_memori") != OK:
        reasons.append("`import memori` failed: %s" % data.get("import_memori"))
    for symbol in SDK_SYMBOLS:
        if data.get(symbol) != OK:
            reasons.append("`from memori import %s` failed: %s" % (symbol, data[symbol]))
    resolved = data.get("resolved")
    out["shadowed"] = bool(
        root and resolved and Path(resolved).is_relative_to(Path(root).resolve()))
    if out["shadowed"]:
        # Diagnostic, not a verdict: a root may legitimately hold the SDK itself
        # (the guard test's clean fixture does), and then resolving inside it is
        # correct. What decides the case is whether the SDK's symbols imported.
        out["resolved_inside_root"] = True
    out["verdict"] = "FAIL" if reasons else "PASS"
    out["detail"] = "; ".join(reasons) or "the SDK's public symbols imported"
    return out


def default_cases(repo_root: Path, home: Path) -> list[tuple[str, str | None, Path, str | None]]:
    """(name, root-under-test, child cwd, child PYTHONPATH) for the real shapes.

    ``leg-shape`` is the one that is already realised: no PYTHONPATH at all, the
    cwd alone puts the repo root on the path, exactly as every leg of
    ``scripts/run_tests.sh`` runs.
    """
    cases = [
        ("leg-shape", str(repo_root), repo_root, None),
        ("repo-root", str(repo_root), Path(tempfile.gettempdir()), str(repo_root)),
    ]
    deployed = home / ".hermes" / "plugins"
    if (deployed / "memori" / "__init__.py").is_file():
        cases.append(("deployed-plugins", str(deployed),
                      Path(tempfile.gettempdir()), str(deployed)))
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--home", default=str(Path.home()),
                    help="home whose .hermes/plugins holds the deployed provider "
                         "(default: this process's HOME)")
    ap.add_argument("--cases", default="",
                    help="comma list of case names to run — built-ins (leg-shape, "
                         "repo-root, deployed-plugins) and/or \"path:<dir>\" names "
                         "added with --path")
    ap.add_argument("--path", action="append", default=[],
                    help="extra root to test (repeatable); drives the guard test's "
                         "hermetic fixtures. When --path is given and --cases is not, "
                         "ONLY the --path roots are checked, so a fixture run never "
                         "depends on this repo's own state.")
    ap.add_argument("--json", action="store_true", help="emit one JSON object instead "
                    "of the line protocol (the guard test parses this)")
    ap.add_argument("--skip-control", action="store_true",
                    help="do not run the clean-process control (hermetic fixture "
                         "runs, where the real SDK is deliberately not involved)")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    wanted = {c for c in args.cases.split(",") if c}
    builtins = [] if (args.path and not wanted) else default_cases(
        repo_root, Path(args.home).expanduser())
    cases = [c for c in builtins if not wanted or c[0] in wanted]
    for extra in args.path:
        resolved = str(Path(extra).resolve())
        name = "path:" + resolved
        if wanted and name not in wanted:
            continue
        cases.append((name, resolved, Path(tempfile.gettempdir()), resolved))

    # Control first: if the SDK is not importable in a CLEAN process, no case
    # below can distinguish "shadowed" from "not installed" and the honest
    # answer is UNDECIDED, not PASS.
    control = (None if args.skip_control else
               run_case("control", None, Path(tempfile.gettempdir()), None))
    results = ([] if control is None else [control]) + [run_case(*c) for c in cases]

    if control is None:
        failed = [r for r in results if r["verdict"] == "FAIL"]
        verdict, code = ("FAIL", 1) if failed else ("PASS", 0)
    elif control["verdict"] != "PASS":
        verdict, code = "UNDECIDED", 2
    else:
        failed = [r for r in results if r["verdict"] == "FAIL"]
        verdict, code = ("FAIL", 1) if failed else ("PASS", 0)

    if args.json:
        print(json.dumps({"verdict": verdict, "control_run": control is not None,
                          "sdk_resolved_in_control":
                              control.get("resolved") if control else None,
                          "cases": results}))
    else:
        for r in results:
            print("RESULT %-18s %-8s %s" % (r["case"], r["verdict"], r["detail"]))
            if r.get("resolved"):
                print("       %-18s        resolved -> %s" % ("", r["resolved"]))
        if control is None:
            print("SDK control: SKIPPED (--skip-control)")
        else:
            print("SDK control resolved to: %s" % control.get("resolved"))
        print("VERDICT %s" % verdict)
    return code


if __name__ == "__main__":
    sys.exit(main())
