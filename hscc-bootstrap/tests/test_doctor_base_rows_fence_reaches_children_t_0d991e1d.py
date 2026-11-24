"""t_0d991e1d — the BASE (non-feature) doctor rows must refuse in a suite-spawned
door too, and each refusal must name the seam it refused to cross.

``doctor_rows.build_row`` fences every FEATURE row (t_eb768c2a, PR #761). The rows in
``run_doctor()``'s opening ``checks`` list are plain ``Check`` calls and never pass
through that fence, so they carried NO in-suite fence at all. Measured with the door
spawned as a child of a pytest item (``python3 hscc-bootstrap/doctor.py --json``,
HOME sealed, fake fleet binaries first on PATH): the ``gateway`` row printed the
launchd pid of the machine's real gateway job, ``sparkrun cluster`` and ``nas`` shelled
out to the live ``sparkrun cluster list``, ``models served`` opened its configured
endpoints, the two skill rows walked the profile trees, and ``sparkrun`` exec'd uv.
Reading the free disk space or ``sys.version_info`` from such a child is fine — the
ruling is per row and is written next to each row in ``doctor.py``; this file pins the
half of that ruling that says NO.

Both directions, same planted HOME, same interpreter, ``PYTEST_CURRENT_TEST`` the only
difference (the same shape #732/#761 established, because a control is what stops a
refusal from guarding a branch nothing can take):

* marked child — each fenced row renders its refusal, every live seam below was
  reached ZERO times, and no fake fleet binary was exec'd;
* unmarked child — the same door takes the ambient path: every fenced row reads, every
  seam is reached, and the fake ``sparkrun``/``launchctl``/``uv`` really were exec'd.

The child reports WHICH BRANCH IT TOOK — a call count on each live seam plus the argv
its fake binaries recorded — rather than whether anything was logged. Nothing here
reads ``.hscc/daemon.log``: #716 (``78be7fe3``) muted these rows' borrowed WARN through
``_silent_read``, so a file-diffing assertion on that line is another lane's ruling.

Hermetic by construction: planted HOME/HERMES_HOME only (no agent tree), a fake ``bin``
first on PATH whose ``sparkrun``/``ssh``/``curl``/``launchctl``/``systemctl``/``uv``
only append their argv to a log inside the sandbox, and state naming TEST-NET-1
(192.0.2.1) plus a closed loopback port. The child echoes HOME/HERMES_HOME back so a
broken seal fails here instead of reading live state in silence.
"""

import json
import os
import subprocess
import sys

import doctor_main_only as dmo

#: The door. ``runpy`` runs it as ``__main__`` inside the child rather than exec'ing
#: it, because the guard needs the call counts from the child's own process;
#: ``run_path`` puts the script's directory on ``sys.path[0]``, which is the property
#: that makes doctor.py's flat-sibling imports resolve, so the child exercises exactly
#: the import shape ``doctor_tools.doctor_findings`` exercises.
_DOOR = os.path.join(os.path.dirname(os.path.abspath(dmo.__file__)), "doctor.py")
_BOOT = os.path.dirname(os.path.abspath(dmo.__file__))
_REPO = os.path.dirname(_BOOT)

#: module.attr -> the row(s) that cross it ONLY on their real read. Every entry is a
#: live seam: nothing below is reached when the row refuses.
_SEAMS = (
    # `sparkrun cluster` + `nas`: the live fleet CLI (hscc-cluster/tests/
    # sparkrun_fence.py exists to stop exactly this from a test child).
    ("doctor", "_run_cluster_list"),
    # `gateway`: launchctl / systemctl / ps — the service manager and the process
    # table (the measured leak of this card).
    ("gateway_liveness", "gateway_probe_run"),
    # `models served`: a real socket to every configured endpoint.
    ("doctor", "_http_get_default"),
    # `skill names (duplicates)` + `reviewer readiness`: the profile-tree walk.
    ("doctor", "_skill_findings"),
    # `sparkrun`: `uv python find`, i.e. exec'ing an operator-owned toolchain binary.
    ("doctor", "_uv_default_python"),
)

#: The fenced rows, by the name they print. Each must render a refusal in the marked
#: child and must NOT render one in the unmarked child.
FENCED_ROWS = (
    "sparkrun cluster",
    "nas",
    "gateway",
    "models served",
    "skill names (duplicates)",
    "reviewer readiness (sdlc-review)",
)

#: Rows whose ruling is "a suite-spawned door may read this": no seam above may be
#: reached BY THEM, and they must keep reporting a real value in both halves.
ACCEPTED_ROWS = ("python", "pyyaml", "disk space", "hermes", "sparkrun")

#: Fake binaries: they never do their job, they only record that they were asked to.
#: ``launchctl``/``systemctl``/``uv`` are here so the CONTROL half proves the exec
#: without the suite ever touching the machine's real service manager or uv install.
_FAKE_BIN = {
    "sparkrun": (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "a = sys.argv[1:]\n"
        "print('[]' if a[:2] == ['cluster', 'list'] else '')\n"
    ),
    "ssh": "#!/usr/bin/env python3\nimport sys\nsys.exit(255)\n",
    "curl": "#!/usr/bin/env python3\nimport sys\nsys.exit(22)\n",
    "launchctl": "#!/usr/bin/env python3\nprint('')\nsys_exit = __import__('sys').exit\nsys_exit(1)\n",
    "systemctl": "#!/usr/bin/env python3\nprint('inactive')\n",
    "uv": "#!/usr/bin/env python3\nprint('3.12.0')\n",
}

_CHILD = (
    "import json, os, runpy, sys\n"
    "boot, repo, door, log = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]\n"
    "seams = json.loads(sys.argv[5])\n"
    "sys.path.insert(0, repo)   # so `hscc_daemon` resolves\n"
    "sys.path.insert(0, boot)   # the deployed flat-sibling layout\n"
    "def record(kind, argv):\n"
    "    with open(log, 'a', encoding='utf-8') as fh:\n"
    "        fh.write(json.dumps([kind, list(map(str, argv))]) + '\\n')\n"
    "counts, roots, unwrapped = {}, [], []\n"
    "for mod_name, attr in seams:\n"
    "    try:\n"
    "        mod = __import__(mod_name, fromlist=[attr])\n"
    "        real = getattr(mod, attr)\n"
    "    except Exception as exc:      # a seam this interpreter lacks is loud\n"
    "        unwrapped.append(f'{mod_name}.{attr}: {type(exc).__name__}: {exc}')\n"
    "        continue\n"
    "    def spy(*a, _r=real, _k=mod_name + '.' + attr, **k):\n"
    "        counts[_k] = counts.get(_k, 0) + 1\n"
    "        if _k.endswith('_skill_findings') and a:\n"
    "            roots.append(str(a[0]))\n"
    "        return _r(*a, **k)\n"
    "    setattr(mod, attr, spy)\n"
    "    counts[mod_name + '.' + attr] = 0\n"
    "out = {'seams': counts, 'roots': roots, 'unwrapped': unwrapped,\n"
    "       'pytest_imported': 'pytest' in sys.modules,\n"
    "       'home': os.environ.get('HOME'), 'hermes_home': os.environ.get('HERMES_HOME')}\n"
    "sys.argv = [door, '--json']\n"
    "buf = __import__('io').StringIO()\n"
    "_real_stdout, sys.stdout = sys.stdout, buf\n"
    "try:\n"
    "    runpy.run_path(door, run_name='__main__')\n"
    "except SystemExit as exc:\n"
    "    out['rc'] = exc.code\n"
    "finally:\n"
    "    sys.stdout = _real_stdout\n"
    "text = buf.getvalue()\n"
    "try:\n"
    "    out['checks'] = [(c.get('name'), bool(c.get('ok')), bool(c.get('fatal')),\n"
    "                      (c.get('detail') or '')[:200])\n"
    "                     for c in json.loads(text).get('checks', [])]\n"
    "except Exception as exc:\n"
    "    out['error'] = f'{type(exc).__name__}: {exc}: {text[-400:]}'\n"
    "print(json.dumps(out))\n"
)


def _spawn(tmp_path, *, under_test: bool, profile_home: bool = False) -> dict:
    """Run the door as a suite-spawned child would, and report which branch it took.

    ``profile_home`` plants HERMES_HOME as a PROFILE-scoped home
    (``<root>/profiles/<name>``) — the normal worker mode — which is the shape
    ``_skills_home()`` walks UP and out of, so it is the shape the skill rows' ruling
    is about.
    """
    root = tmp_path / "hermes"
    home = tmp_path / "home"
    hermes_home = root
    (home / ".hscc").mkdir(parents=True)
    if profile_home:
        hermes_home = root / "profiles" / "sandbox-worker"
        (hermes_home / "skills").mkdir(parents=True)
        sibling = root / "profiles" / "sibling-orch"
        (sibling / "skills").mkdir(parents=True)
        root.mkdir(parents=True, exist_ok=True)
    else:
        hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: sandbox-model\n"
        "  base_url: http://127.0.0.1:1/v1\n", encoding="utf-8")
    (home / ".hscc" / "cluster.json").write_text(
        json.dumps({"clusters": [{"name": "sandbox", "hosts": ["192.0.2.1"]}]}),
        encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "exec.log"
    log.touch()
    for name, body in _FAKE_BIN.items():
        header = (
            "#!/usr/bin/env python3\n"
            "# Records that this suite child asked it to run; does nothing else\n"
            "# (t_0d991e1d).\n"
            "import json, sys\n"
            f"with open({str(log)!r}, 'a', encoding='utf-8') as fh:\n"
            f"    fh.write(json.dumps([{name!r}] + sys.argv[1:]) + '\\n')\n")
        p = bin_dir / name
        p.write_text(header + body, encoding="utf-8")
        p.chmod(0o755)

    probe = tmp_path / "probe_base_rows.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), HERMES_HOME=str(hermes_home),
               HSCC_DIR=str(home / ".hscc"),
               XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    # Nothing ambient may point at real state: no agent tree, no operator HSCC_HOME,
    # and no operator uv pin (that pin would legitimately skip the uv probe and make
    # the control half prove nothing). PYTEST_CURRENT_TEST is the variable under test.
    for var in ("HERMES_AGENT_PATH", "HSCC_HOME", "HERMES_REAL_HOME",
                "PYTEST_CURRENT_TEST", "HSCC_TESTS_ENFORCE", "UV_PYTHON",
                "HERMES_PROFILE"):
        env.pop(var, None)
    if under_test:
        # What pytest itself exports for the duration of every item, and which every
        # subprocess of that item therefore inherits.
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"
    proc = subprocess.run(
        [sys.executable, str(probe), _BOOT, _REPO, _DOOR, str(log),
         json.dumps([list(s) for s in _SEAMS])],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report["home"] == str(home) and report["hermes_home"] == str(hermes_home), (
        f"the probe ran under {report['home']!r}/{report['hermes_home']!r}, not the "
        "planted pair — its ambient path could have reached the operator's state")
    # The whole defect is that an in-process fence is invisible across a spawn, so
    # prove the child is such a child before believing anything below.
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself: it is not the spawned-door shape and every "
        "assertion below would pass for the wrong reason")
    assert "error" not in report, f"the door produced no payload: {report['error']}"
    # A seam the child could not wrap is a hole in the guard, not a clean run.
    assert report["unwrapped"] == [], (
        f"live seams the probe could not instrument: {report['unwrapped']}")
    report["execs"] = [json.loads(line) for line in
                       log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return report


def _row(report, name):
    rows = [c for c in report["checks"] if c[0] == name]
    assert len(rows) == 1, (
        f"{name!r} rendered {len(rows)} times — a row that silently disappeared would "
        f"also satisfy 'reached nothing'. Got: {[c[0] for c in report['checks']]}")
    return rows[0]


def _execs(report, kind):
    return [e for e in report["execs"] if e[0] == kind]


def test_a_spawned_door_refuses_every_base_row_that_crosses_a_live_seam(tmp_path):
    """The marked child must refuse, and must reach NONE of the live seams."""
    r = _spawn(tmp_path, under_test=True)
    for name in FENCED_ROWS:
        _ok, _fatal, detail = _row(r, name)[1], _row(r, name)[2], _row(r, name)[3]
        assert "refused" in detail, (
            f"the base row {name!r} took the ambient read path in a child of the "
            f"suite: {detail[:150]}")
        assert "inject" in detail, (
            f"{name!r} refused without naming the seam a caller must inject — a "
            f"refusal nobody can satisfy is a permanent 'unknown': {detail[:150]}")
    for key, count in r["seams"].items():
        assert count == 0, (
            f"{key} was reached {count} time(s) by a suite-spawned door even though "
            "every row that crosses it reported a refusal")
    for kind in ("sparkrun", "launchctl", "systemctl", "uv", "ssh", "curl"):
        assert _execs(r, kind) == [], (
            f"a suite-spawned door exec'd {kind}: {_execs(r, kind)}")
    # A refusal is "cannot verify", not "broken": it must not be fatal, or the door
    # would report the operator's install as failed on account of the suite.
    for name in FENCED_ROWS:
        assert _row(r, name)[2] is False, f"{name!r} refused FATALLY"


def test_a_spawned_door_still_reads_what_is_a_property_of_the_machine(tmp_path):
    """The rows ruled ACCEPTABLE keep answering, in the marked child as well.

    `python`/`pyyaml` read the interpreter, `disk space` a statvfs, `hermes` a stat
    under the caller's root, and `sparkrun`'s PATH lookup is the row's own subject.
    Pinning them here is what keeps the fence above from becoming a blanket skip that
    quietly empties the payload.
    """
    r = _spawn(tmp_path, under_test=True)
    for name in ACCEPTED_ROWS:
        row = _row(r, name)
        assert "refused" not in row[3], f"{name!r} refused though its ruling is accept"
        assert row[3].strip(), f"{name!r} rendered an empty detail"
    assert _execs(r, "uv") == [], "the `sparkrun` row exec'd uv from a suite child"


def test_without_the_marker_the_same_child_takes_the_ambient_path(tmp_path):
    """Control: the refusals above bite, and the marker is what makes them.

    Same probe, same planted HOME, only ``PYTEST_CURRENT_TEST`` removed — every fenced
    row must then read (it cannot refuse), every seam must be reached, and the fake
    binaries must have been exec'd. If this ever reports a refusal, the fence has
    started denying the operator's own doctor run, which is the door's only real use.
    """
    r = _spawn(tmp_path, under_test=False)
    for name in FENCED_ROWS:
        detail = _row(r, name)[3]
        assert "refused" not in detail, (
            f"{name!r} refused with no pytest in sight — the fence now denies a human "
            f"CLI run: {detail[:150]}")
    for key, count in r["seams"].items():
        assert count >= 1, (
            f"{key} was never reached on the ambient path, so the zero in the marked "
            f"child guards a branch nothing can take: {r['seams']}")
    for kind in ("sparkrun", "launchctl", "uv"):
        assert _execs(r, kind), (
            f"the unmarked child never exec'd {kind}, so that row's zero is not "
            f"evidence of a fence: {r['execs']}")


def test_the_skill_rows_walk_out_of_a_profile_home_only_outside_the_marker(tmp_path):
    """Why the skill rows are fenced at all, measured rather than asserted.

    ``_skills_home()`` deliberately walks a PROFILE-scoped HERMES_HOME up one level so
    the audit sees every sibling profile. That is correct for the operator's run and
    wrong for a suite child: the child then walks every profile tree of the machine.
    The planted tree here carries a sibling profile, so the two halves differ on ONE
    fact — which root the walk started from — and nothing else.
    """
    r = _spawn(tmp_path, under_test=True, profile_home=True)
    assert r["roots"] == [], (
        f"a suite-spawned door walked the profile trees: {r['roots']}")
    c = _spawn(tmp_path, under_test=False, profile_home=True)
    walked = {os.path.realpath(x) for x in c["roots"]}
    shared = os.path.realpath(str(tmp_path / "hermes"))
    assert walked == {shared}, (
        "the control did not take the walk this ruling is about, so the empty roots "
        f"above prove nothing: walked={sorted(walked)} shared={shared}")
