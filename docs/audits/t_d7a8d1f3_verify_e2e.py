#!/usr/bin/env python
"""t_d7a8d1f3 e2e proof — no-ANSI in a real non-tty pipe on the converted CLI.

These commands read a LIVE board / registry / network, so a real subprocess
cannot cleanly inject stubs for the board-dependent surfaces (doctor, lint,
legacy, decompose). Their --json byte-identity + no-ANSI proofs therefore live
in the pytest no-ANSI suite (built exactly to the machine contract).

`verify`, however, reads ONLY the registry (an argument) + runs the verify
command (a subprocess), and writes state to ~/.flightdeck. By scrubbing HOME to
a tmp dir and pointing the registry at a scratch file we CAN run it as a real
subprocess with a non-tty (piped) stdout and prove its themed human view emits
ZERO ANSI escape bytes — the daemon/scripts/iOS contract. That is the strongest
clean real-process proof this card's command set permits.

Run: python3 hscc-project/../docs/audits/t_d7a8d1f3_verify_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2] / "hscc-project"   # <repo>/hscc-project
PY = sys.executable


def _write_registry(tmp: Path) -> Path:
    """A scratch registry: one passing project, one failing, one without verify.

    The repo paths are real subdirs under ``tmp`` so verify's shell runner has
    a valid cwd (a non-existent cwd would make the _dispatch fail before the
    command runs). The whole home is scrubbed, so these never touch the host.
    """
    for name in ("good", "broken", "plain"):
        (tmp / name).mkdir(exist_ok=True)
    p = tmp / "registry.yaml"
    p.write_text(
        "projects:\n"
        "  - name: good\n"
        "    repo: " + str(tmp / "good") + "\n"
        "    verify: 'true'\n"
        "  - name: broken\n"
        "    repo: " + str(tmp / "broken") + "\n"
        "    verify: 'false'\n"
        "  - name: plain\n"
        "    repo: " + str(tmp / "plain") + "\n",
        encoding="utf-8",
    )
    return p


def _run(argv: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run flightdeck as a subprocess with stdout piped (NON-tty)."""
    return subprocess.run(
        [PY, "-m", "flightdeck.cli", *argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="t_d7a8d1f3_e2e_"))
    reg = _write_registry(tmp)
    env = dict(os.environ)
    # Scrub HOME so ~/.flightdeck and ~/.hermes never touch the operator host.
    env["HOME"] = str(tmp)
    env["PYTHONPATH"] = str(REPO)

    print(f"[REPO] {REPO}")
    print(f"[REGISTRY] {reg}")
    print()

    # 1. Human view --all in a non-tty pipe: NO ANSI, content preserved.
    r = _run(["--registry", str(reg), "verify", "--all"], cwd=tmp, env=env)
    out = r.stdout
    print("=== `flightdeck verify --all` (human) — real piped subprocess ===")
    print("exit code :", r.returncode)
    print("has ANSI  :", ("\x1b[" in out) or ("\x1b" in out))
    print("--- captured stdout ---")
    print(out)
    print("--- captured stderr ---")
    print(r.stderr)
    assert len(out) > 0
    assert "\x1b[" not in out and "\x1b" not in out, "ANSI leaked into a pipe"
    assert "good" in out and "PASS" in out
    assert "broken" in out and "FAIL" in out
    assert "no verify configured" in out
    print("[PASS] human view is plain with content preserved.\n")

    # 2. Byte-identity for --json is proven in the pytest no-ANSI suite (the
    #    duration is wall-clock in a real subprocess, so it can't be a clean
    #    byte-equal demo there). Still show the JSON path is valid + clean:
    rj = _run(["--registry", str(reg), "--json", "verify", "--all"], cwd=tmp, env=env)
    print("=== `flightdeck verify --all --json` — real piped subprocess ===")
    print("has ANSI  :", ("\x1b[" in rj.stdout) or ("\x1b" in rj.stdout))
    payload = json.loads(rj.stdout)  # valid JSON, the machine contract
    statuses = {o["project"]: o["status"] for o in payload}
    print("parsed    :", statuses)
    assert "\x1b" not in rj.stdout
    assert statuses == {"good": "PASS", "broken": "FAIL", "plain": "NO_VERIFY"}
    print("[PASS] --json path is valid JSON + no ANSI.\n")

    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
