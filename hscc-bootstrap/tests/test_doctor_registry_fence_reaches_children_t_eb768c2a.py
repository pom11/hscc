"""t_eb768c2a — the registry fence must reach the door the suite SPAWNS, for
every feature row, not just the one row that fences inside its own module.

``doctor_rows.build_row`` refuses an uninjected feature row "inside pytest", and
``doctor_tools.doctor_findings`` runs the doctor in a SEPARATE interpreter
(``python3 hscc-bootstrap/doctor.py --json`` — the flat sibling imports only
resolve when the script runs as a script). ``"pytest" in sys.modules`` is
process-local, so in a suite-spawned door the refusal never fired for ANY row and
each row fell through to its own real check. The roster-wide measurement for this
card ran the door under the marker pytest exports per item and without it: the
two payloads were identical except for ``orch_wake_subs``, the one row whose
module already honoured ``PYTEST_CURRENT_TEST`` (PR #732). Measured, not guessed:
a spawned door read the live service manager, the deployed plugin tree, the
profile trees, the node-pause overlay and the fleet CLI — and shelling out to the
live ``sparkrun`` CLI from a test child is the class of thing
``hscc-cluster/tests/sparkrun_fence.py`` exists to stop, except that fence cannot
see across a spawn.

What is pinned here, in BOTH directions (the control is what stops this file
guarding a branch nothing can take):

* marked child — every row that reads operator state renders its refusal, the
  live seams were reached ZERO times, no production ssh transport was produced,
  and the fleet block reported itself skipped instead of probing;
* unmarked child — the same door takes the ambient path: the rows read, the
  seams are reached, the fleet block runs. The difference between the two runs is
  the marker and nothing else, same planted HOME, same interpreter.

The child reports WHICH BRANCH IT TOOK — a call count on each live seam — rather
than whether anything was logged. Nothing here reads ``.hscc/daemon.log``: #716
muted these rows' borrowed WARN through ``_silent_read``, so a file-diffing
assertion on that line is another lane's ruling.

Hermetic: the child gets a planted HOME/HERMES_HOME with no agent tree, a fake
``bin`` first on PATH whose ``sparkrun``/``ssh``/``curl`` only record that they
were called, and cluster/serving state naming TEST-NET-1 (192.0.2.1) and a closed
loopback port. Nothing in it can reach the fleet, the operator's boards or their
``~/.hscc``, and the child echoes its own HOME back so a broken seal fails here
rather than reading live state in silence.
"""

import json
import os
import subprocess
import sys

import doctor_main_only as dmo
import doctor_node_pauses as dnp
import doctor_orch_subs as dos

#: The door. ``runpy`` runs THIS file (below, in the child) rather than exec'ing
#: it, because the guard needs call counts from inside the child; ``run_path``
#: puts the script's own directory on ``sys.path[0]``, which is the property that
#: makes doctor.py's flat sibling imports resolve, so the child exercises the same
#: import shape the door does.
_DOOR = os.path.join(os.path.dirname(os.path.abspath(dmo.__file__)),
                     "doctor.py")

#: module.attr -> what reaching it proves. Every entry is a seam a row crosses
#: ONLY when it takes its real read, never when it refuses:
#:  * ``board_db_paths``      — the repo's ONE kanban board enumeration;
#:  * ``overlay_path``        — the operator's ``~/.hscc`` pause overlay;
#:  * ``home_findings``       — the sweep of every profile's plugin config;
#:  * ``detect_cluster``      — the live ``sparkrun cluster list`` topology read;
#:  * ``_default_ssh_factory``— producing the PRODUCTION remote-exec transport
#:    (the fleet rows' most harmful failure mode: a test child that can ssh).
_SEAMS = (
    ("hscc_daemon.autodown", "board_db_paths"),
    ("hscc_daemon.node_pauses", "overlay_path"),
    ("doctor_main_only", "home_findings"),
    ("detect", "detect_cluster"),
    ("doctor_node_recovery", "_default_ssh_factory"),
    ("doctor_notifier_mask", "_default_ssh_factory"),
    ("install_gpu_clock_cap", "_default_ssh_factory"),
)

_CHILD = (
    "import json, os, runpy, sys\n"
    "boot, repo_root, door = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "seams = json.loads(sys.argv[4])\n"
    "sys.path.insert(0, repo_root)   # so `hscc_daemon` resolves\n"
    "sys.path.insert(0, boot)        # the deployed flat-sibling layout\n"
    "counts = {}\n"
    "unwrapped = []\n"
    "for mod_name, attr in seams:\n"
    "    try:\n"
    "        mod = __import__(mod_name, fromlist=[attr])\n"
    "        real = getattr(mod, attr)\n"
    "    except Exception as exc:      # a seam this interpreter lacks is loud\n"
    "        unwrapped.append(f'{mod_name}.{attr}: {type(exc).__name__}: {exc}')\n"
    "        continue\n"
    "    def spy(*a, _r=real, _k=mod_name + '.' + attr, **k):\n"
    "        counts[_k] = counts.get(_k, 0) + 1\n"
    "        return _r(*a, **k)\n"
    "    setattr(mod, attr, spy)\n"
    "    counts[mod_name + '.' + attr] = 0\n"
    "out = {'seams': counts, 'unwrapped': unwrapped,\n"
    "       'pytest_imported': 'pytest' in sys.modules, 'home': os.environ.get('HOME')}\n"
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
    "    out['checks'] = [(c.get('name'), bool(c.get('ok')),\n"
    "                      (c.get('detail') or '')[:160])\n"
    "                     for c in json.loads(text).get('checks', [])]\n"
    "except Exception as exc:\n"
    "    out['error'] = f'{type(exc).__name__}: {exc}: {text[-400:]}'\n"
    "print(json.dumps(out))\n"
)

_FAKE_BIN = {
    "sparkrun": (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['cluster', 'list']:\n"
        "    print('[]')\n"
        "elif a[:2] == ['cluster', 'default']:\n"
        "    print('null')\n"
        "else:\n"
        "    print('{}')\n"
    ),
    "ssh": "#!/usr/bin/env python3\nimport sys\nsys.exit(255)\n",
    "curl": "#!/usr/bin/env python3\nimport sys\nsys.exit(22)\n",
}


def _spawn(tmp_path, *, under_test: bool) -> dict:
    """Run the door as a suite-spawned child would, and report its branches."""
    home = tmp_path / "home"
    (home / ".hermes" / "plugins").mkdir(parents=True)
    hscc = home / ".hscc"
    hscc.mkdir()
    (hscc / "cluster.json").write_text(
        json.dumps({"clusters": [{"name": "sandbox", "hosts": ["192.0.2.1"]}]}),
        encoding="utf-8")
    (hscc / "serving.json").write_text(
        json.dumps({"units": [{"name": "sandbox-unit", "host": "127.0.0.1",
                              "port": 1, "model": "sandbox-model"}]}),
        encoding="utf-8")
    (home / ".hermes" / "config.yaml").write_text(
        "model:\n  default: worker-model\n"
        "  base_url: http://127.0.0.1:1/v1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in _FAKE_BIN.items():
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    probe = tmp_path / "probe_door_branches.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"),
               XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    # The child must resolve inside the sandbox only: an agent-tree pointer would
    # let its ambient path find real boards, and HSCC_DIR would let it find a
    # real ~/.hscc. PYTEST_CURRENT_TEST is the variable under test.
    for var in ("HERMES_AGENT_PATH", "HSCC_DIR", "HSCC_HOME",
                "HERMES_REAL_HOME", "PYTEST_CURRENT_TEST", "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        # What pytest itself exports for the duration of every item, and what
        # every subprocess of that item therefore inherits.
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"
    proc = subprocess.run(
        [sys.executable, str(probe), os.path.dirname(os.path.abspath(dmo.__file__)),
         os.path.dirname(os.path.dirname(os.path.abspath(dmo.__file__))), _DOOR,
         json.dumps([list(s) for s in _SEAMS])],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report["home"] == str(home), (
        f"the probe ran under {report['home']!r}, not the planted home — its "
        "ambient path could have reached the operator's state")
    # The whole defect is that the fence's own test is invisible across a spawn,
    # so prove the child is such a child before believing anything below.
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself: it is not the spawned-door shape and "
        "every assertion below would pass for the wrong reason")
    assert "error" not in report, f"the door produced no payload: {report['error']}"
    # A seam the child could not wrap is a hole in the guard, not a clean run.
    assert report["unwrapped"] == [], (
        f"live seams the probe could not instrument: {report['unwrapped']}")
    return report


def _detail(report, name):
    rows = [c for c in report["checks"] if c[0] == name]
    assert len(rows) == 1, f"{name!r} rendered {len(rows)} times: " \
                           f"{[c[0] for c in report['checks']]}"
    return rows[0][2]


#: The three registry rows this file names by their own module's constant: each
#: is a loader/refusal row, so each is fenced by the REGISTRY — the fence this
#: card is about — and each reads something of the operator's.
_OPERATOR_STATE_ROWS = (
    (dnp.CHECK_NAME, "hscc_daemon.node_pauses.overlay_path"),
    (dmo.CHECK_NAME, "doctor_main_only.home_findings"),
    (dos.CHECK_NAME, "hscc_daemon.autodown.board_db_paths"),
)

#: The fleet block is gated separately (``run_doctor``'s own auto-detect) and is
#: the severe class: it ssh's nodes and shells out to the sparkrun CLI.
_FLEET_GATE = "detect.detect_cluster"
_TRANSPORT_GATE = "doctor_node_recovery._default_ssh_factory"


def test_a_spawned_door_refuses_every_row_that_reads_operator_state(tmp_path):
    """The marked child must refuse, and must reach NONE of the live seams."""
    r = _spawn(tmp_path, under_test=True)
    for name, seam in _OPERATOR_STATE_ROWS:
        detail = _detail(r, name)
        assert "refused" in detail, (
            f"the registry fence did not reach the child for {name!r}: it took "
            f"the ambient read path — {detail[:140]}")
        assert r["seams"][seam] == 0, (
            f"{name!r} reported a refusal but still reached {seam} "
            f"{r['seams'][seam]} time(s)")
    assert r["seams"][_FLEET_GATE] == 0, (
        "a suite-spawned door resolved the live cluster topology — the fleet "
        "block's own auto-detect did not reach the child")
    for key, count in r["seams"].items():
        if key.endswith("_default_ssh_factory"):
            assert count == 0, (
                f"a suite-spawned door produced the PRODUCTION ssh transport "
                f"({key}) {count} time(s) — that transport remote-execs onto "
                f"fleet nodes")
    skipped = [n for n, _ok, d in r["checks"] if "skipped (fleet checks off)" in d]
    assert skipped, (
        "nothing reported itself skipped, so the fleet block ran in a child of "
        "the suite: " + str([n for n, _o, _d in r["checks"]]))


def test_without_the_marker_the_same_child_takes_the_ambient_path(tmp_path):
    """Control: the assertions above bite, and the marker is what makes them.

    Same probe, same planted HOME, only ``PYTEST_CURRENT_TEST`` removed — the
    child must then read (it cannot refuse), reach the seams, and run the fleet
    block. If this ever reports a refusal, the fence has started denying the
    operator's own doctor run, which is the door's only real use.
    """
    r = _spawn(tmp_path, under_test=False)
    for name, seam in _OPERATOR_STATE_ROWS:
        detail = _detail(r, name)
        assert "refused" not in detail, (
            f"{name!r} refused with no pytest in sight — the fence now denies a "
            f"human CLI run: {detail[:140]}")
    reached = [s for _n, s in _OPERATOR_STATE_ROWS if r["seams"][s] >= 1]
    assert reached, (
        "no row reached a live seam on the ambient path, so the refusal counts "
        f"above are not evidence of a fence — seams: {r['seams']}")
    transports = [k for k, c in r["seams"].items()
                  if k.endswith("_default_ssh_factory") and c >= 1]
    assert transports, (
        "the fleet block never produced its production transport outside the "
        "marker either, so the zero above guards a branch nothing can take — "
        f"the fence would pass even if the fleet block never ran: {r['seams']}")
