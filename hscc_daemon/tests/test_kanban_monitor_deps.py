"""Regression guard: the brew libexec venv must be able to import the kanban
board-read chain — the exact failure that made ``hscc kanban monitor`` blind
under ``/opt/homebrew/bin/hscc`` (fixed by pinning ``httpx==0.28.1`` in
requirements.txt, PR #113 / card t_a491b05d).

Two independent legs:

(a) ALWAYS-RUN, machine-independent: parse the repo's ``requirements.txt`` (the
    dependency SOT the brew formula builds its libexec venv from — the tap
    declares its resource blocks "mirror exactly requirements.txt of the pinned
    release") and assert ``httpx`` is present there as an exact ``==`` pin.
    A silent dep drop — the class of change that caused the incident — fails
    here on every machine, CI included, with no interpreter involved.

(b) INTEGRATION: when ``/opt/homebrew/Cellar/hscc/*/libexec/venv/bin/python``
    exists, run the monitor's board-read entry (``kanban_cli`` module import +
    ``kanban_live.collect()`` — the same chain ``hscc kanban monitor`` walks
    first) under THAT interpreter and fail if ``No module named`` shows up in
    the collect errors array. Skips cleanly when the Cellar path is absent
    (CI), and additionally skips a keg whose shipped payload manifest
    (``<keg>/libexec/hscc/requirements.txt``) predates the httpx pin — that keg
    was built before the fix, and enforcing against it would pin the repo suite
    red on the operator machine until the tap + ``brew reinstall`` land
    (orchestrator-owned), which is not a repo regression. The leg ARMS
    automatically on the first keg whose manifest declares httpx:
    manifest-declares-X-but-venv-lacks-X is exactly the tap-vs-SOT drift this
    guard exists to catch.

The integration probe is read-only and hermetic: it points ``HERMES_KANBAN_HOME``
at a tmp dir (the one seam that isolates the board root — measured:
``kanban_home()`` reads ``HERMES_KANBAN_HOME``, whereas redirecting
``HERMES_HOME`` still enumerates the operator's real boards), and asserts the DB
path Hermes actually opens is inside that tmp dir BEFORE connecting. If
isolation cannot be proven it refuses to run rather than touch a real board.
``collect()`` never writes a board and never touches the orchestrator ledger
(only ``attach_tokens`` / ``attach_orchestrators`` do, and they are deliberately
NOT called here).

Non-vacuity: a negative-control leg runs the SAME probe with a PYTHONPATH stub
that makes ``import httpx`` fail, and asserts the violation detector FIRES.
Without it, a probe that silently never reaches the chain (blocked by the
delegated-worker fence, a missing ``~/.hermes/hermes-agent``, …) would pass
vacuously.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO / "requirements.txt"
KEG_PY_GLOB = "/opt/homebrew/Cellar/hscc/*/libexec/venv/bin/python"
NEEDLE = "No module named"

# The board-read pass must finish well inside this; a timeout is a failure,
# never a skip (a hung chain is the same operator-visible blindness).
PROBE_TIMEOUT_S = 180

# Worker/dispatcher pins that must NOT leak into the probe: they would redirect
# the board resolution away from the hermetic tmp root.
# HERMES_DELEGATED_CHILD_CONTEXT additionally opens the kanban DB read-only and
# refuses to create a missing fixture board (the same reason scripts/run_tests.sh
# strips it for every suite leg) — the probe only ever touches a throwaway tmp
# board, so the fence protects nothing here.
STRIP_ENV = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_HOME",
)


# ── (a) requirements.txt pin parsing (always-run) ───────────────────────────

_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXACT_PIN_RE = re.compile(r"^==[ \t]*[A-Za-z0-9][A-Za-z0-9._+!-]*$")


def _canonical(name: str) -> str:
    """PEP 503 name normalisation (httpx_, HTTPX and h.t.t.p.x all mean httpx)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> dict:
    """``{canonical_name: specifier_string}`` for a requirements.txt body.

    Comments (``#``), blank lines and environment markers (``;`` suffix) are
    stripped; ``name[extras]==ver`` keeps its spec. A line with no specifier
    maps to ``""``. Deliberately minimal — the repo's requirements.txt is a flat
    pin list, not a resolver input.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        m = _NAME_RE.match(line)
        if not m:
            continue
        rest = line[m.end():].strip()
        if rest.startswith("["):  # drop an [extras] block before the specifier
            close = rest.find("]")
            rest = rest[close + 1:].strip() if close != -1 else ""
        out[_canonical(m.group(1))] = rest
    return out


def test_requirements_txt_pins_httpx():
    """httpx must stay an exact ``==`` pin in the dependency SOT.

    Why this file and not a formula cross-check: the brew libexec venv is built
    by ``venv.pip_install resources`` from tap resource blocks that mirror this
    file, and ``hermes_cli``'s board-read chain reaches httpx at IMPORT time
    (``kanban_db_connect.connect`` -> ``hermes_state`` ->
    ``agent.credential_pool`` -> ``hermes_cli.auth`` ->
    ``class _CappedByteStream(httpx.SyncByteStream)``), so a dropped pin ships a
    venv where every ``hscc kanban monitor`` board read dies with
    ``No module named 'httpx'``.
    """
    reqs = parse_requirements(REQUIREMENTS.read_text())
    assert "httpx" in reqs, (
        "httpx vanished from requirements.txt — the brew libexec venv is built "
        "from this file, and the kanban board-read chain imports httpx eagerly "
        "via hermes_cli.auth (PR #113). Restore the pin, e.g. 'httpx==0.28.1'. "
        "Parsed: " + json.dumps(reqs))
    spec = reqs["httpx"]
    assert _EXACT_PIN_RE.match(spec), (
        f"httpx must be an exact '==' pin (tracks Hermes' own "
        f"httpx[socks]==0.28.1); found {spec!r} — a range lets the keg venv "
        f"float into a version the board-read chain has never been run under.")


def test_requirements_txt_keeps_the_other_venv_deps():
    """The three pre-existing pins must not be dropped while httpx is added:
    the same silent-drop accident would blind the monitor in a different way
    (rich = the renderer, yaml = hermes_cli.config, psutil = cluster_live)."""
    reqs = parse_requirements(REQUIREMENTS.read_text())
    for name in ("pyyaml", "rich", "psutil", "httpx"):
        assert name in reqs, f"{name} dropped from requirements.txt: {json.dumps(reqs)}"


def test_requirements_parser_shape():
    """Pin the parser the guard depends on. A parser that ignored comments
    would make the httpx assertion vacuously green, and one that mis-split
    extras/markers would fail it spuriously — both are pinned here rather than
    assumed."""
    cases = [
        ("httpx==0.28.1\n", {"httpx": "==0.28.1"}),
        ("httpx[socks]==0.28.1\n", {"httpx": "==0.28.1"}),
        ("  httpx == 0.28.1  # pin\n", {"httpx": "== 0.28.1"}),
        ("httpx==0.28.1; python_version>='3.9'\n", {"httpx": "==0.28.1"}),
        ("# httpx==0.28.1\n", {}),                      # comment is NOT a pin
        ("Http_X==1.2\n", {"http-x": "==1.2"}),         # PEP 503 canonical form
        ("rich>=13,<16\n", {"rich": ">=13,<16"}),       # a range, not a pin
        ("\n\n", {}),
    ]
    for text, expected in cases:
        assert parse_requirements(text) == expected, text


# ── (b) keg-venv integration probe ──────────────────────────────────────────

# Stdlib-only script executed under the TARGET interpreter; prints one JSON
# object. It never lets an exception escape: a blind chain must come back as
# DATA so the assertions live in the test rather than in the probe.
PROBE_SRC = '''
import json, os, sys
from pathlib import Path

out = {"python": sys.executable, "ran": False, "errors": [], "boards": [],
       "db_path": "missing-api", "detail": ""}
try:
    if os.environ.get("HSCC_PROBE_VIA_WRAPPER") == "1":
        # What /opt/homebrew/bin/hscc does: the shipped wrapper puts the
        # payload root on sys.path (candidate #1 = site-packages/plugins).
        import hscc_cli  # noqa: F401
    from hscc_daemon import kanban_cli  # noqa: F401  (the monitor verb module)
    from hscc_daemon import kanban_live
    from hscc_daemon import autodown

    kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        out["detail"] += "kanban lib unreachable: " + str(
            autodown._KANBAN_LOAD.get("reason"))
    else:
        try:
            out["db_path"] = str(
                kanban_db.kanban_db_path(board="default").resolve())
        except AttributeError:
            pass
        guard_root = os.environ.get("HSCC_PROBE_TMP", "")
        if out["db_path"] == "missing-api":
            # Cannot prove where Hermes would open its DB -> refuse to connect.
            out["detail"] += " | kanban_db_path API absent: refusing to connect"
        elif not Path(out["db_path"]).is_relative_to(guard_root):
            out["detail"] += " | ISOLATION FAILED, db=" + out["db_path"]
        else:
            data = kanban_live.collect()
            out["errors"] = list(data["errors"])
            out["boards"] = [b["name"] for b in data["boards"]]
            out["ran"] = True
except Exception as e:
    out["detail"] += " | probe raised: {}: {}".format(type(e).__name__, e)
print(json.dumps(out))
'''


def _probe_env(tmp: Path, extra=None) -> dict:
    env = dict(os.environ)
    for k in STRIP_ENV:
        env.pop(k, None)
    # kanban_home() reads HERMES_KANBAN_HOME — measured to be the seam that
    # isolates the board root. HERMES_HOME is redirected too, only so the
    # chain's scratch dirs land under tmp instead of ~/.hermes.
    env["HERMES_KANBAN_HOME"] = str(tmp)
    env["HERMES_HOME"] = str(tmp / "home")
    env["HSCC_PROBE_TMP"] = str(tmp)
    env.update(extra or {})
    return env


def _run_probe(python, tmp: Path, extra=None, via_wrapper=False) -> dict:
    """Run the board-read probe under ``python``. Returns the probe payload.

    Raises AssertionError on a non-zero exit / unparsable output — an interpreter
    that cannot even start the probe is a failure, not a skip.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    script = tmp / "_board_read_probe.py"
    script.write_text(PROBE_SRC)
    env = _probe_env(tmp, extra)
    if via_wrapper:
        env["HSCC_PROBE_VIA_WRAPPER"] = "1"
    proc = subprocess.run(
        [str(python), str(script)], capture_output=True, text=True,
        timeout=PROBE_TIMEOUT_S, env=env, cwd=str(tmp))
    if proc.returncode != 0:
        raise AssertionError(
            f"board-read probe under {python} exited {proc.returncode}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-4000:]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError(
            f"board-read probe under {python} printed no JSON: {proc.stdout[-2000:]!r}")


def chain_violations(payload: dict) -> list:
    """Every ``No module named`` surface in a probe result.

    Scans the collect errors array (the card's wording) AND the probe-level
    detail: a wrapper/payload import failure happens BEFORE collect runs and
    would otherwise slip through.
    """
    hay = list(payload.get("errors") or []) + [payload.get("detail") or ""]
    return [h for h in hay if NEEDLE in h]


def _keg_venv_pys() -> list:
    root = Path("/opt/homebrew/Cellar/hscc")
    if not root.is_dir():
        return []
    return sorted(root.glob("*/libexec/venv/bin/python"))


def _keg_manifest(keg_py: Path):
    """The requirements.txt the keg ships (the tap installs the whole repo
    payload under ``libexec/hscc/``), or None if absent.

    ``<prefix>/Cellar/hscc/<ver>/libexec/venv/bin/python`` -> ``<ver>`` is
    ``parents[3]``.
    """
    keg_root = keg_py.parents[3]
    cand = keg_root / "libexec" / "hscc" / "requirements.txt"
    return cand if cand.is_file() else None


def test_brew_keg_venv_imports_the_board_read_chain(tmp_path):
    """Run the monitor's board-read entry under every installed brew keg venv
    python and fail on ``No module named`` in the collect errors array."""
    kegs = _keg_venv_pys()
    if not kegs:
        pytest.skip(f"no brew keg venv under {KEG_PY_GLOB} (CI / non-brew install)")
    verdicts = []
    for py in kegs:
        manifest = _keg_manifest(py)
        if manifest is not None and "httpx" not in parse_requirements(
                manifest.read_text()):
            # This keg was built from a pre-pin requirements.txt (fix = PR #113;
            # tap resource blocks + `brew reinstall hscc` are separate,
            # orchestrator-owned work). Nothing in this repo can retro-fix an
            # installed keg, so the leg skips it and stays honest about why.
            verdicts.append(f"{py}: skip (keg manifest predates the httpx pin)")
            continue
        payload = _run_probe(py, tmp_path / py.parents[3].name, via_wrapper=True)
        bad = chain_violations(payload)
        assert not bad, (
            f"kanban board-read chain is BLIND under keg venv {py}: {bad}\n"
            f"full probe: {json.dumps(payload, indent=2)}\n"
            "Fix: the pin must be in requirements.txt AND installed — "
            "tap resource blocks mirror it, then `brew reinstall hscc`.")
        assert payload["ran"], (
            f"board-read pass never ran under {py}: "
            f"{json.dumps(payload, indent=2)}")
        # Non-vacuity: a green pass must have actually read a board.
        assert payload["boards"], (
            f"board-read pass read ZERO boards under {py} with no errors — "
            f"vacuous probe, investigate before trusting this guard: "
            f"{json.dumps(payload, indent=2)}")
        verdicts.append(f"{py}: ok boards={payload['boards']}")
    assert verdicts


def test_guard_fires_on_a_blind_venv(tmp_path):
    """Negative control: the same probe with a PYTHONPATH stub that makes
    ``import httpx`` raise MUST report violations — otherwise the green leg
    above proves nothing.

    Runs under the test interpreter against the repo tree (available wherever
    the suite runs). Skips when even the CLEAN run cannot reach the chain on
    this machine (e.g. CI with no ``~/.hermes/hermes-agent``): the control's
    premise is that a healthy chain reads a board.
    """
    clean = _run_probe(sys.executable, tmp_path / "clean",
                       extra={"PYTHONPATH": str(REPO)})
    if chain_violations(clean) or not clean["ran"] or not clean["boards"]:
        pytest.skip(
            "clean board-read pass is not healthy on this machine "
            f"(errors={clean['errors']!r} detail={clean['detail']!r} "
            f"boards={clean['boards']!r}) — nothing to blind; live blindness "
            "must be judged on the keg leg")
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "httpx.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'httpx'\")\n")
    blinded = _run_probe(sys.executable, tmp_path / "blinded",
                         extra={"PYTHONPATH": f"{stub}{os.pathsep}{REPO}"})
    bad = chain_violations(blinded)
    assert bad, (
        "guard is VACUOUS: stubbing out httpx did NOT surface 'No module named' "
        f"in the errors array: {json.dumps(blinded, indent=2)}")
