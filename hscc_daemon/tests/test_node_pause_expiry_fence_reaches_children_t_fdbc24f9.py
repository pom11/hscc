"""t_fdbc24f9 — the ``node pause expiry`` row's in-suite fence must bite in a child
the suite SPAWNS, not only in the process that imported pytest.

``hscc_daemon/node_pause_exec.py::_doctor_check`` fenced its default-root read with
``"pytest" in sys.modules`` — a process-local test. The door that row is reached from
is not that process: the agent tool and ``hscc doctor`` run in a fresh interpreter a
suite item spawns, and pytest was never imported there. Measured with the row called
directly from such a child (its own planted ``HOME``, so "live" here means the planted
files that stand in for the operator's): the row read the planted stamp, the planted
pause overlay and the planted pid file, and reported a verdict about them, while the
fence that was supposed to stop it had fired for a process that was never involved.

The door path is already covered — the ``node_pause_expiry`` registry row is refused
by ``doctor_rows.build_row`` before its module is consulted (PR #761/#763). This file
pins the half that was still open: a DIRECT call to ``doctor_check()`` /
``_doctor_check()`` with no root.

Both directions, same planted home, same interpreter, ``PYTEST_CURRENT_TEST`` the only
difference (the #732/#761 shape, because a control is what stops a refusal from
guarding a branch nothing can take):

* marked child — both entry points refuse, and all three live seams (the stamp read,
  the overlay read, the daemon-liveness probe) were reached ZERO times;
* unmarked child — the same call takes the ambient path: every seam is reached, and
  the verdict it prints names the planted stamp's resumed host and the planted pause
  count, which is the proof the reads were real rather than an empty-file no-op.

The child reports WHICH BRANCH IT TOOK — a call count per live seam plus the path each
read used — rather than whether anything was logged. It loads the row by ordinary
``import`` (not ``runpy.run_path``, which executes into a throwaway namespace and would
make the counts meaningless — t_0d991e1d measured exactly that) because the seams are
names on the modules that OWN them (``state.read_state``,
``node_pauses.list_pauses``) plus the row's own liveness probe, and an ordinary import
is also exactly how ``hscc doctor`` and the daemon load this module.

Hermetic by construction: planted ``HOME`` only (so the row's default root resolves
inside the sandbox — asserted from the child's own module constants, not assumed), a
fixture fleet of RFC 5737 documentation addresses (192.0.2.x), a pid no process can
own, and a probe that signals 0 (a probe never sends a signal). Nothing outside
``tmp_path`` is read or written.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

#: ``<repo>/hscc_daemon/tests/<this file>`` -> the repo root, which is what the child
#: puts on ``sys.path`` so ``import hscc_daemon`` resolves the way it does for
#: ``hscc doctor`` and for the daemon itself.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The phrase the repo's refusal convention always carries (``doctor_rows``,
#: ``_refused_transport``, ``api_cli._refused_launchd_status``).
REFUSAL = "refused inside a pytest process"

#: The fixture fleet. RFC 5737 documentation addresses only.
PARKED = "192.0.2.11"
RESUMED = "192.0.2.12"

#: A pid no process can own on this platform (and ``os.kill(pid, 0)`` probes, it does
#: not signal, so even a colliding pid would be harmless).
_IMPOSSIBLE_PID = "4194304"

#: The three live reads ``_doctor_check`` performs when no root was supplied and
#: nothing was injected. Every one of them is a read of the operator's ``~/.hscc``.
_SEAMS = (
    ("hscc_daemon.state", "read_state"),
    ("hscc_daemon.node_pauses", "list_pauses"),
    ("hscc_daemon.node_pause_exec", "_daemon_pid_alive"),
)

#: The child: load the row, wrap every live seam with a counting spy that also records
#: the path it was asked about, call the row the way a spawned door calls it, and print
#: one JSON report. ``mode`` picks the call; the caller's environment picks the fence.
_CHILD = r'''
import importlib, io, json, os, sys
from datetime import datetime, timezone

repo, mode, root_arg = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, repo)

out = {"home": os.environ.get("HOME"),
       "pytest_imported": "pytest" in sys.modules,
       "seams": {}, "paths": {}, "verdicts": {}, "unwrapped": []}

import hscc_daemon.node_pause_exec as npe
from hscc_daemon import node_pauses, state

# The child's DEFAULT root, from the constants the row itself reads. If these are not
# under the planted home, every "refused nothing leaked" assertion below is vacuous.
out["hscc_dir"] = node_pauses.HSCC_DIR
out["state_dir"] = state.STATE_DIR

counts, paths = {}, {}
for mod_name, attr in json.loads(sys.argv[4]):
    try:
        target = importlib.import_module(mod_name)
        real = getattr(target, attr)
    except Exception as exc:            # a seam this interpreter lacks is loud
        out["unwrapped"].append("%s.%s: %s: %s" % (mod_name, attr,
                                                   type(exc).__name__, exc))
        continue
    key = mod_name + "." + attr

    def spy(*a, _r=real, _k=key, **k):
        counts[_k] = counts.get(_k, 0) + 1
        if a:
            paths.setdefault(_k, []).append(str(a[0]))
        return _r(*a, **k)

    setattr(target, attr, spy)
    counts[key] = 0
out["seams"], out["paths"] = counts, paths


def verdict(label, fn):
    try:
        res = fn()
    except Exception as exc:
        out["verdicts"][label] = {"raised": "%s: %s" % (type(exc).__name__, exc)}
        return
    out["verdicts"][label] = {
        "name": res.get("name"), "ok": res.get("ok"),
        "fatal": res.get("fatal"), "detail": (res.get("detail") or "")[:400]}


now_iso = datetime.now(timezone.utc).isoformat()
if mode == "default":
    # Both entry points, because the registry calls the wrapper and a suite child may
    # call the private one; the fence sits inside `_doctor_check` and must cover both.
    verdict("doctor_check", lambda: npe.doctor_check())
    verdict("_doctor_check", lambda: npe._doctor_check())
elif mode == "root":
    verdict("doctor_check", lambda: npe.doctor_check(root=root_arg))
elif mode == "injected":
    # A stamp handed in, but still no root: the overlay and pid reads would still be
    # default-root reads, so the fence must not treat an injection as a named root.
    stamp = {"ok": True, "timestamp": now_iso, "checked_at": now_iso,
             "message": "injected stamp", "resumed": [], "kept": [], "skipped": [],
             "deferred": []}
    verdict("doctor_check", lambda: npe.doctor_check(
        stream_state=stamp, _daemon_running=lambda: True,
        _predates_build=lambda: False))
else:
    out["bad_mode"] = mode

print(json.dumps(out))
'''


def _plant(tmp_path):
    """A ``HOME`` whose ``~/.hscc`` carries this row's three artefacts.

    Written through the shapes the real writers produce (a stream stamp with the
    ``timestamp`` field ``state.write_state`` adds, and a normalised overlay entry) so
    the control half reads a HEALTHY stream rather than a file the reader would reject
    and quietly treat as empty.
    """
    home = tmp_path / "home"
    hscc = home / ".hscc"
    (hscc / "state").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (hscc / "state" / "node_pause_expiry.json").write_text(json.dumps({
        "ok": True,
        "timestamp": now.isoformat(),
        "checked_at": now.isoformat(),
        "message": f"auto-resumed 1 expired pause(s): {RESUMED}",
        "resumed": [RESUMED], "kept": [], "skipped": [], "deferred": [],
    }), encoding="utf-8")
    (hscc / "node_pauses.json").write_text(json.dumps({"pauses": {PARKED: {
        "reason": "sandbox maintenance",
        "since": (now - timedelta(hours=1)).isoformat(),
        "until": (now + timedelta(hours=1)).isoformat(),
        "units": ["sandbox-unit"], "span_nodes": [], "stopped": True,
    }}}), encoding="utf-8")
    (hscc / "daemon.pid").write_text(_IMPOSSIBLE_PID + "\n", encoding="utf-8")
    return home, hscc


def _spawn(tmp_path, *, mode: str, under_test: bool) -> dict:
    """Call the row from a child of a pytest item and report which branch it took.

    The CI shape is what is being reproduced: ``scripts/run_tests.sh`` seals ``HOME``,
    and every child a leg spawns inherits that seal — so a row whose fence reads only
    ``sys.modules`` is answering about a process the child never was. Here the seal is
    per-test and the marker is the single variable.
    """
    home, hscc = _plant(tmp_path)
    probe = tmp_path / "probe_node_pause_expiry_row.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join(["/usr/bin", "/bin"]))
    # Nothing ambient may point at real state, and the variable under test must be
    # the ONLY pytest signal the child can see.
    for var in ("PYTEST_CURRENT_TEST", "HSCC_DIR", "HSCC_HOME", "HERMES_HOME",
                "HERMES_AGENT_PATH", "HERMES_REAL_HOME", "HERMES_PROFILE",
                "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        # What pytest itself exports for the duration of every item, and which every
        # subprocess of that item therefore inherits.
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"

    proc = subprocess.run(
        [sys.executable, str(probe), _REPO, mode, str(hscc),
         json.dumps([list(s) for s in _SEAMS])],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report["home"] == str(home), (
        f"the probe ran under {report['home']!r}, not the planted home — its default "
        "root could have reached the operator's state")
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself: it is not the spawned-child shape and every "
        "assertion below would pass for the wrong reason")
    # The seal, checked where it matters: the constants the row reads, not just HOME.
    for key in ("hscc_dir", "state_dir"):
        assert os.path.realpath(report[key]).startswith(os.path.realpath(str(hscc))), (
            f"the child's {key}={report[key]!r} is not under the planted {hscc} — the "
            "default-root read this guard measures would not have been a sandbox read")
    # A seam the child could not wrap is a hole in the guard, not a clean run.
    assert report["unwrapped"] == [], (
        f"live seams the probe could not instrument: {report['unwrapped']}")
    assert "bad_mode" not in report, report.get("bad_mode")
    return report


def _verdict(report, label):
    got = report["verdicts"].get(label)
    assert got is not None, f"the child never produced a {label} verdict: {report}"
    assert "raised" not in got, f"{label} raised instead of answering: {got['raised']}"
    return got


def _assert_refused(got, label):
    assert REFUSAL in got["detail"], (
        f"{label} took the ambient read path in a child of the suite: "
        f"{got['detail'][:150]}")
    assert "inject" in got["detail"] or "root=" in got["detail"], (
        f"{label} refused without naming what a caller must supply — a refusal nobody "
        f"can satisfy is a permanent 'unknown': {got['detail'][:150]}")
    assert got["ok"] is False and got["fatal"] is False, (
        f"{label} is not an honest non-fatal refusal: {got}")


def test_a_spawned_child_refuses_the_default_root_and_reaches_no_live_seam(tmp_path):
    """The marked child must refuse from BOTH entry points and read nothing live.

    This is the hole the card names: the registry refuses the ``node_pause_expiry``
    row before its module is consulted, so the door path was covered, but nothing
    stopped a suite-spawned caller of ``doctor_check()`` / ``_doctor_check()`` from
    reading the operator's stamp, overlay and pid file.
    """
    r = _spawn(tmp_path, mode="default", under_test=True)
    for label in ("doctor_check", "_doctor_check"):
        _assert_refused(_verdict(r, label), label)
    for key, count in r["seams"].items():
        assert count == 0, (
            f"{key} was reached {count} time(s) by a suite-spawned caller even though "
            f"the row reported a refusal: {r['seams']}")
    assert r["paths"] == {}, f"a refused row still named a path it read: {r['paths']}"


def test_a_spawned_child_that_named_a_root_still_answers_from_it(tmp_path):
    """The fence must not become a blanket skip: a caller-named root is the caller's
    own tree and answers in the marked child as well.

    The paths each read used are captured by the spy, so this pins not just "a verdict
    came back" but that every read stayed inside the planted tree.
    """
    hscc = tmp_path / "home" / ".hscc"
    r = _spawn(tmp_path, mode="root", under_test=True)
    got = _verdict(r, "doctor_check")
    assert REFUSAL not in got["detail"], (
        f"a caller-named root was refused — the fence now denies the row's real use: "
        f"{got['detail'][:150]}")
    assert got["name"] == "node pause expiry (durable-until stream)"
    assert RESUMED in got["detail"], (
        f"the row did not read the stamp it was pointed at: {got['detail'][:150]}")
    assert "1 pause(s) parked" in got["detail"], (
        f"the row did not read the overlay it was pointed at: {got['detail'][:150]}")
    assert r["seams"]["hscc_daemon.node_pauses.list_pauses"] >= 1
    assert r["seams"]["hscc_daemon.node_pause_exec._daemon_pid_alive"] >= 1
    for key, seen in r["paths"].items():
        if key.endswith("read_state"):
            continue          # the root branch reads the file directly, by design
        for path in seen:
            if path in ("None", "sandbox-unit"):
                continue
            assert os.path.realpath(path).startswith(os.path.realpath(str(hscc))), (
                f"{key} read {path!r}, outside the caller's named root {hscc}")


def test_an_injected_stamp_does_not_unlock_a_default_root_in_a_child(tmp_path):
    """``stream_state=`` answers the STAMP question only; the overlay and pid reads
    would still be default-root reads of the operator's tree, so the fence must hold.

    The refusal's own fix text says so ("inject ... or pass root=") — this pins that
    the two are not interchangeable, and that the conservative answer costs zero live
    reads.
    """
    r = _spawn(tmp_path, mode="injected", under_test=True)
    _assert_refused(_verdict(r, "doctor_check"), "doctor_check(stream_state=...)")
    for key, count in r["seams"].items():
        assert count == 0, (
            f"{key} was reached {count} time(s) with only the stamp injected: "
            f"{r['seams']}")


def test_without_the_marker_the_same_child_takes_the_ambient_path(tmp_path):
    """Control: the refusals above bite, and the marker is what makes them.

    Same probe, same planted home, only ``PYTEST_CURRENT_TEST`` removed — the row
    cannot refuse, every seam must be reached, and the verdict must carry what the
    planted files actually say. If this ever reports a refusal, the fence has started
    denying the operator's own ``hscc doctor``, which is the row's only real use.
    """
    r = _spawn(tmp_path, mode="default", under_test=False)
    for label in ("doctor_check", "_doctor_check"):
        got = _verdict(r, label)
        assert REFUSAL not in got["detail"], (
            f"{label} refused with no pytest in sight — the fence now denies a human "
            f"CLI run: {got['detail'][:150]}")
        assert RESUMED in got["detail"], (
            f"{label} never read the planted stamp, so the zero in the marked child "
            f"is not evidence of a fence: {got['detail'][:150]}")
        assert PARKED not in got["detail"] or "1 pause(s) parked" in got["detail"]
    for key, count in r["seams"].items():
        assert count >= 1, (
            f"{key} was never reached on the ambient path, so the zero in the marked "
            f"child guards a branch nothing can take: {r['seams']}")
