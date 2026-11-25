"""Doctor wiring coverage for the ``node_pause_expiry`` row (card t_005e87fd).

Two layers, the same shape as ``test_doctor_kanban_enforce.py`` and
``test_doctor_node_pauses.py``:

1. **The seam inside ``run_doctor()``.** The row's real targets are
   ``~/.hscc/state/node_pause_expiry.json`` and ``~/.hscc/node_pauses.json`` —
   the OPERATOR's live stream stamp and pause overlay when this suite runs from
   a dispatched worker. Inside a pytest process the verdict must be injected
   (``_node_pause_expiry_check``) or the row renders as a refusal naming the
   seam; a canary trio planted under the redirected ``HOME`` proves a leak would
   be visible (and stays untouched by the refusal).

2. **The registry entry as the bridge.** One entry, five Check fields, an
   UNVERIFIABLE row when the stream's module is not deployed, and — the part
   this class of row has repeatedly got wrong — NO root from the environment.
   Both artefacts this row reads are written by HOME-bound writers with no
   ``HSCC_DIR`` override (``state.STATE_DIR`` and ``node_pauses``' own default
   root), so a root lifted from the environment is a directory no pause can
   ever reach, and the row would call a live stream "never stamped" — a FATAL,
   once a pid file happened to sit there (t_adb66608, t_be690204).

The verdict postures themselves are pinned where they live, against the real
writers, in ``hscc_daemon/tests/test_node_pause_expiry_doctor.py``. Hosts here
are TEST-NET-1 (192.0.2.0/24) and every read is of a temp dir.
"""

import json
import types

import doctor
import doctor_rows

CHECK_NAME = "node pause expiry (durable-until stream)"
STREAM = "node_pause_expiry"
FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])

#: The host a canary pause names. If it ever appears in a row built inside this
#: suite, the fence has leaked.
CANARY_HOST = "192.0.2.99"


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/hscc-fixture/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _home(tmp_path, monkeypatch):
    """A throwaway HOME / HERMES_HOME / HSCC_DIR, and no sparkrun probe."""
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Set on purpose: the row must be indifferent to it. A row that read it
    # would be reading a directory no pause writer can reach.
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
    return home


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    kw.setdefault("_orch_waker_check",
                  lambda: {"name": "orch-waker (orchestrator waker)",
                           "ok": True, "fatal": False, "detail": "ok",
                           "fix": ""})
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent", **_fleet_health(), **kw)


def _seam(**verdict):
    base = {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": "expiry stream healthy: last pass 12s ago (budget 210s); "
                      "no node pauses parked",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def _row(res, name=CHECK_NAME):
    return {c["name"]: c for c in res["checks"]}[name]


# ── the seam inside run_doctor ─────────────────────────────────────────────
def test_run_doctor_reports_the_expiry_row_when_injected(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _node_pause_expiry_check=_seam())

    rows = [c for c in res["checks"] if c["name"] == CHECK_NAME]
    assert len(rows) == 1, "the registry must render this row exactly once"
    assert rows[0]["ok"] is True and rows[0]["fatal"] is False


def test_a_stamped_red_is_a_warning_and_does_not_sink_the_doctor(tmp_path,
                                                                monkeypatch):
    """The row's headline case — a pause past due and still parked — is
    ``ok=False / fatal=False``: printed, acted on, but doctor is bootstrap's
    Stage-1 gate and the daemon IS retrying."""
    res = _doctor(tmp_path, monkeypatch, _node_pause_expiry_check=_seam(
        ok=False,
        detail="PAST DUE, STILL PARKED: restart FAILED on 192.0.2.12 "
               "(checked_at 2026-11-22T12:00:00+00:00)"))

    row = _row(res)
    assert row["ok"] is False and row["fatal"] is False
    assert "192.0.2.12" in row["detail"]
    assert res["ok"] is True, "a warn must not fail the preflight"


def test_the_never_stamped_verdict_can_sink_the_preflight(tmp_path, monkeypatch):
    """The one fatal the row may raise (daemon running, stream never stamped):
    doctor's exit code is the install gate, so the fatal flag has to reach it."""
    res = _doctor(tmp_path, monkeypatch, _node_pause_expiry_check=_seam(
        ok=False, fatal=True,
        detail="never stamped while the daemon runs"))

    assert res["ok"] is False
    assert CHECK_NAME in res["fatal_failures"]


def test_run_doctor_refuses_an_uninjected_check(tmp_path, monkeypatch):
    """THE fence: ``fleet_checks=True`` with no seam must not read the
    operator's ``~/.hscc`` — the row comes back a refusal naming the seam."""
    res = _doctor(tmp_path, monkeypatch)

    row = _row(res)
    assert row["ok"] is False and row["fatal"] is False
    assert "refused" in row["detail"] and "pytest" in row["detail"]
    assert "_node_pause_expiry_check" in row["detail"], "the refusal names its seam"


def test_the_refusal_leaves_the_canary_state_untouched(tmp_path, monkeypatch):
    """The tripwire for the fence above: a full plausible ``~/.hscc`` — a RED
    stamp naming a host, an overlay holding it, a live-looking pid file. If the
    pytest branch ever regressed into the real check, the detail would carry
    this text instead of ``refused``."""
    root = tmp_path / ".hscc"
    state = root / "state"
    state.mkdir(parents=True)
    canary_stamp = state / f"{STREAM}.json"
    canary_stamp.write_text(json.dumps({
        "timestamp": "2026-11-22T12:00:00+00:00", "stream": STREAM,
        "ok": False, "kept": [CANARY_HOST], "skipped": [], "deferred": [],
        "resumed": [], "checked_at": "2026-11-22T12:00:00+00:00",
        "message": "1 pause(s) could not be resumed (192.0.2.99)"}))
    canary_overlay = root / "node_pauses.json"
    canary_overlay.write_text(json.dumps({"pauses": {
        CANARY_HOST: {"reason": "canary", "until": "2026-11-22T11:00:00+00:00",
                      "since": "2026-11-22T09:00:00+00:00", "units": [],
                      "span_nodes": [CANARY_HOST], "stopped": True}}}))
    canary_pid = root / "daemon.pid"
    canary_pid.write_text("999999")

    res = _doctor(tmp_path, monkeypatch)

    row = _row(res)
    assert "refused" in row["detail"], (
        f"the row read live daemon state instead of refusing: {row['detail']}")
    assert CANARY_HOST not in row["detail"]
    # Read-only by contract: nothing was written back either.
    assert json.loads(canary_stamp.read_text())["kept"] == [CANARY_HOST]
    assert json.loads(canary_overlay.read_text())["pauses"][CANARY_HOST]["reason"] \
        == "canary"
    assert canary_pid.read_text() == "999999"


def test_an_injected_seam_never_touches_the_feature_module(tmp_path, monkeypatch):
    """Trust order, not just trust: with a seam present the loader must not run
    at all, so a suite cannot reach the real module's reads by accident."""
    calls = []
    monkeypatch.setattr(doctor, "_load_node_pause_exec",
                        lambda: calls.append("loader") or None)

    _doctor(tmp_path, monkeypatch, _node_pause_expiry_check=_seam())

    assert calls == [], "the seam short-circuits the real check entirely"


# ── the registry entry as the bridge ───────────────────────────────────────
def _spec():
    return {s.key: s for s in doctor.FEATURE_CHECK_ROWS}[
        "node_pause_expiry"]


def _outside(monkeypatch, **patches):
    """A module VIEW of doctor whose own ``sys.modules`` has no pytest.

    ``build_row``'s loader branch is the non-pytest branch; a global
    ``delitem(sys.modules, "pytest")`` would take the fence off the running
    session, which is why the view is built this way (the
    ``test_doctor_node_pauses.py`` precedent). Patches are applied to the real
    module FIRST so the copied ``__dict__`` carries them.
    """
    for name, value in patches.items():
        monkeypatch.setattr(doctor, name, value)
    outside = types.SimpleNamespace()
    outside.__dict__.update(vars(doctor))
    outside.sys = types.SimpleNamespace(modules={})
    import sys  # the shared fence reads the PROCESS module table
    # The fence is the shared, child-visible predicate now, not a view of
    # this module's ``sys.modules``: close BOTH process channels, or the
    # refusal branch is taken and the CLI path under test never runs.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return outside


def test_the_row_declares_no_root_context():
    """The wiring half of t_be690204 for this row: both writers resolve from
    HOME (``state.STATE_DIR`` for the stamp, ``node_pauses``' default root for
    the overlay) and neither sees ``HSCC_DIR``, so a handed root is a directory
    no pause can reach. ``row_kwargs=()`` makes that un-wireable rather than
    merely unwired."""
    assert _spec().row_kwargs == (), (
        "the node_pause_expiry row must declare no per-row context: a handed "
        "root splits the reader from the writers that resolve from HOME")


def test_the_row_receives_no_root_even_when_the_environment_names_one(
        monkeypatch):
    """The call-site half, at the render boundary: with ``HSCC_DIR`` exported,
    ``hscc_dir=`` passed to ``run_doctor`` and the full context available,
    ``render_rows`` must hand this row's check NOTHING. The stub proves the
    delegation happened and that the roots were dropped, not ignored."""
    seen = []

    class Feature:
        CHECK_NAME = CHECK_NAME

        @staticmethod
        def doctor_check(**kw):
            seen.append(kw)
            return {"name": CHECK_NAME, "ok": True, "fatal": False,
                    "detail": "from the real check", "fix": ""}

    outside = _outside(monkeypatch, _load_node_pause_exec=lambda: Feature)
    rows = doctor_rows.render_rows(outside, [_spec()], {},
                                   hscc_dir="/sandbox/.hscc",
                                   hermes_home="/sandbox/.hermes")

    assert seen == [{}], f"the row was handed context: {seen}"
    assert rows[0].detail == "from the real check"
    assert rows[0].name == CHECK_NAME and rows[0].fatal is False


def test_the_row_is_unverifiable_when_its_module_is_missing(monkeypatch):
    """A deployment without ``hscc_daemon/node_pause_exec.py`` next to bootstrap
    must print ITS OWN row — the entry's ``missing_detail`` / ``missing_fix`` —
    never vanish and never claim ok."""
    outside = _outside(monkeypatch, _load_node_pause_exec=lambda: None)
    spec = _spec()

    row = doctor_rows.build_row(outside, spec, None)

    assert row.name == CHECK_NAME
    assert row.ok is False and row.fatal is False
    assert row.detail == spec.missing_detail, "the entry's own text must be used"
    assert row.fix == spec.missing_fix
    assert "node_pause_exec.py" in row.detail and "not importable" in row.detail


def test_the_fallback_name_matches_the_feature_module():
    """A registry ``fallback_name`` is a STRING naming a module constant, so
    only a comparison pins the two spellings of one check name."""
    core = doctor._load_node_pause_exec()
    assert core is not None, "the row's module must be importable here"
    assert doctor._NODE_PAUSE_EXPIRY_CHECK_NAME == core.CHECK_NAME == CHECK_NAME


def test_the_loader_resolves_the_repo_sibling():
    """The loader's fallback (repo root on ``sys.path``) is what makes the row
    work from a plain checkout, where hscc_daemon is not 'installed'."""
    core = doctor._load_node_pause_exec()

    assert core is not None
    assert core.STREAM_NAME == STREAM
    assert callable(core.doctor_check)


def test_the_refusal_names_the_seam_and_the_real_call():
    """doctor's refusal and the feature module's own refusal must point at the
    same two doors, or a caller that misses one cannot find the other."""
    res = doctor._refused_node_pause_expiry_check()

    assert res["name"] == CHECK_NAME
    assert res["ok"] is False and res["fatal"] is False
    assert "_node_pause_expiry_check" in res["detail"]
    assert "doctor_check(root=" in res["fix"]
