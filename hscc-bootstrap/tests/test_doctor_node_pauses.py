"""Doctor coverage for the node-pause overlay (feature 2.17.3, t_ba1fec3e).

Three postures are the deliverable, and each is pinned against a REAL overlay
file under a temp root (the overlay module's own ``directory=`` seam), not
against a stubbed reader:

* an active pause is an INFO row — ``ok=True``, ``fatal=False``, host + reason +
  since + until in the detail. A pause is the operator's own decision; nothing
  about it may read as WARN (○) or FAIL (✗).
* nothing parked is SILENT — no host, no reason, no window, no warning. (The
  row itself stays, per doctor's own rule that a check never renders as
  absence-of-output; what goes quiet is the pause information.)
* a malformed overlay is a WARN fail-loud row — ``ok=False``, ``fatal=False``,
  naming the file and quoting what the reader could not use, because
  ``node_pauses.load()`` answers "no pauses" for a corrupt file and an operator
  must never be told "nothing is parked" about a node they parked.

Then the wiring, which is where this class of row has repeatedly gone wrong:

* the seam routes through ``run_doctor()`` and the row appears exactly once;
* inside a pytest process the row REFUSES the default root (it would read the
  operator's ``~/.hscc/node_pauses.json`` and let a live pause decide what the
  suite asserts) — pinned by making the reader explode if it is called at all;
* the registry hands this row NO root (t_adb66608 / t_be690204: node_pauses has
  no HSCC_DIR override, so an environment root is a directory no pause writer
  can reach and every parked node would read as "nothing is parked").

Hosts are TEST-NET-1 (192.0.2.0/24) and every read is of a temp dir: nothing
here can touch the fleet or the operator's state.
"""

import json
import os
import types
from datetime import datetime, timezone

import doctor
import doctor_node_pauses as dnp
import doctor_rows

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])

#: Fixed clock: `since` must be the value seeded, not the wall clock.
NOW = datetime(2026, 11, 21, 18, 0, 0, tzinfo=timezone.utc)


def _core():
    """The overlay module the row reads — the same loader the row uses.

    A real import, not a stub: these tests pin the row's reading of a real
    overlay, so a missing hscc_daemon would make every assertion vacuous.
    """
    core = dnp._load_core()
    assert core is not None, "hscc_daemon/node_pauses.py must be importable"
    return core


def _root(tmp_path):
    root = tmp_path / "hscc-root"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _seed(root, host, **kw):
    """Record a pause through the overlay's own writer, on the fixed clock."""
    return _core().pause(host, directory=root, now=NOW, **kw)


def _write_raw(root, text):
    """Put bytes straight into the overlay file (a writer would never do this)."""
    path = _core().overlay_path(root)
    with open(path, "w") as f:
        f.write(text)
    return path


# ── the info posture: an active pause is reported, never flagged ────────────

def test_active_pause_is_an_info_row(tmp_path):
    root = _root(tmp_path)
    _seed(root, "192.0.2.5", reason="gpu training run", units=["vllm-a"])

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is True, (
        f"a pause is an operator decision, never a doctor failure: {res}")
    assert res["fatal"] is False, (
        "a FATAL here would hard-stop bootstrap over a parked worker")
    # The four fields the card asks for, and nothing that reads as a fault.
    assert res["name"] == dnp.CHECK_NAME
    detail = res["detail"]
    assert "192.0.2.5" in detail
    assert "gpu training run" in detail
    assert "since 2026-11-21T18:00:00+00:00" in detail
    assert "until open-ended" in detail
    assert res["fix"] == "", "an info row has nothing to fix"


def test_until_window_is_shown_when_the_pause_has_a_deadline(tmp_path):
    root = _root(tmp_path)
    _seed(root, "192.0.2.6", reason="maintenance", until="2h")

    detail = dnp.doctor_check(directory=root)["detail"]

    assert "192.0.2.6" in detail and "maintenance" in detail
    assert "until 2026-11-21T20:00:00+00:00" in detail


def test_every_parked_node_gets_its_own_phrase(tmp_path):
    """Both hosts, sorted, one row — an operator scanning doctor must see all
    of them; the absent row is the failure this feature exists to remove."""
    root = _root(tmp_path)
    _seed(root, "192.0.2.9", reason="second")
    _seed(root, "192.0.2.5", reason="first")

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is True and res["fatal"] is False
    assert res["detail"].startswith("2 nodes paused: ")
    assert res["detail"].index("192.0.2.5") < res["detail"].index("192.0.2.9"), \
        "the listing is sorted, so two runs of doctor print the same text"


def test_a_pause_whose_deadline_passed_is_still_info(tmp_path):
    """Truth-accurate, per node_pauses: a pause whose `until` has passed is
    still a pause (the resume has not run), so doctor must not call it a fault
    — that would page the operator for their own forgotten resume."""
    root = _root(tmp_path)
    _seed(root, "192.0.2.5", reason="training", until="1h")

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is True and res["fatal"] is False
    assert "192.0.2.5" in res["detail"]


def test_since_keeps_its_offset_and_loses_its_microseconds(tmp_path):
    """Fractional seconds are trimmed with a REGEX, never split("."), which
    would eat the +00:00 and turn a UTC stamp into something that looks local."""
    root = _root(tmp_path)
    _write_raw(root, json.dumps({"pauses": {
        "192.0.2.5": {"reason": "training", "since": "2026-11-21T18:00:00.123456+00:00"}}}))

    detail = dnp.doctor_check(directory=root)["detail"]

    assert "since 2026-11-21T18:00:00+00:00" in detail
    assert ".123456" not in detail


# ── the silent posture: nothing parked says nothing ─────────────────────────

def test_silent_when_no_pauses(tmp_path):
    """No overlay at all: a green row that names no host and warns about
    nothing — the operator has nothing to act on."""
    root = _root(tmp_path)

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is True and res["fatal"] is False
    assert res["detail"] == dnp.SILENT_DETAIL
    assert "192.0.2." not in res["detail"]
    assert res["fix"] == ""


def test_a_valid_empty_overlay_is_silent_not_malformed(tmp_path):
    """`{"pauses": {}}` is a readable file holding nothing. Conflating it with
    a corrupt file would WARN every fleet that has ever paused and resumed a
    node, which is precisely the noise that makes a WARN row worthless."""
    root = _root(tmp_path)
    _write_raw(root, json.dumps({"pauses": {}}))

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is True and res["fatal"] is False
    assert res["detail"] == dnp.SILENT_DETAIL


def test_a_pause_parked_elsewhere_does_not_leak_into_the_row(tmp_path):
    """The row reads the root it was given — one root per call, no ambient
    second read (the root-split class: two roots in one verdict)."""
    here = _root(tmp_path / "a")
    elsewhere = _root(tmp_path / "b")
    _seed(elsewhere, "192.0.2.77", reason="parked somewhere else")

    res = dnp.doctor_check(directory=here)

    assert res["detail"] == dnp.SILENT_DETAIL
    assert "192.0.2.77" not in res["detail"]


# ── the WARN posture: a file that cannot be trusted is fail-loud ────────────

def test_malformed_overlay_is_a_warn_row_not_a_fail(tmp_path):
    root = _root(tmp_path)
    path = _write_raw(root, '{"pauses": {"192.0.2.5": ')  # truncated write

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is False, (
        "a corrupt overlay must not read as 'nothing is parked'")
    assert res["fatal"] is False, (
        "WARN (○), never FAIL (✗): doctor is Stage 1 of bootstrap and a broken "
        "overlay is work to steer, not a reason to hard-stop an install")
    assert path in res["detail"], "the row must name the file it could not read"
    assert "OVERLAY UNREADABLE" in res["detail"]
    # The reader's OWN complaint is quoted (node_pauses.load warns; the row
    # listens instead of re-parsing), so there is one definition of malformed.
    assert "unreadable" in res["detail"]
    assert "hscc node pause" in res["fix"], "the fix must say how to re-park"


def test_malformed_entry_warns_and_still_names_the_readable_pause(tmp_path):
    """Half a broken overlay is still a broken overlay — and the pause the
    reader COULD use must not be hidden behind the warning."""
    root = _root(tmp_path)
    good = _seed(root, "192.0.2.5", reason="training")
    path = _core().overlay_path(root)
    with open(path) as f:
        doc = json.load(f)
    doc["pauses"]["192.0.2.8"] = "not an object"
    with open(path, "w") as f:
        json.dump(doc, f)

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is False and res["fatal"] is False
    assert "192.0.2.8" in res["detail"], "the unusable entry is named"
    assert "Still readable" in res["detail"]
    assert "192.0.2.5" in res["detail"] and good["reason"] in res["detail"]


def test_non_object_pauses_block_warns(tmp_path):
    root = _root(tmp_path)
    _write_raw(root, json.dumps({"pauses": ["192.0.2.5"]}))

    res = dnp.doctor_check(directory=root)

    assert res["ok"] is False and res["fatal"] is False
    assert "OVERLAY UNREADABLE" in res["detail"]


def test_the_row_is_never_fatal_for_any_posture(tmp_path, monkeypatch):
    """Stated once, across every posture, because `fatal=True` is the field
    that turns this row into a bootstrap abort."""
    root = _root(tmp_path)
    for posture in ("missing", "empty", "paused", "corrupt"):
        for name in os.listdir(root):
            os.remove(os.path.join(root, name))
        if posture == "empty":
            _write_raw(root, json.dumps({"pauses": {}}))
        elif posture == "paused":
            _seed(root, "192.0.2.5", reason="training")
        elif posture == "corrupt":
            _write_raw(root, "{ not json")
        res = dnp.doctor_check(directory=root)
        assert res["fatal"] is False, f"{posture} must never be fatal: {res}"
    # A core that raises where it is documented not to also lands on WARN.
    boom = _core()
    monkeypatch.setattr(boom, "load", lambda directory=None: (_ for _ in ()).throw(
        RuntimeError("simulated reader blow-up")))
    res = dnp.doctor_check(directory=root)
    assert res["ok"] is False and res["fatal"] is False


# ── the fence: a suite must never read the operator's overlay ───────────────

def test_default_root_is_refused_inside_pytest(tmp_path, monkeypatch):
    """`directory=None` means the operator's ~/.hscc — inside a suite that is
    refused, and pinned by making a default-root read impossible."""
    core = _core()

    def _never(*a, **kw):
        raise AssertionError("the row read the default root inside pytest")

    monkeypatch.setattr(core, "load", _never)
    monkeypatch.setattr(core, "overlay_path", _never)

    res = dnp.doctor_check()

    assert res["name"] == dnp.CHECK_NAME
    assert res["ok"] is False and res["fatal"] is False
    assert "refused inside a pytest process" in res["detail"]
    assert "192.0.2." not in res["detail"], "a refusal reports no pause"


# ── the wiring: registry, seam, and no root from the environment ────────────

def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _doctor(tmp_path, monkeypatch, **kw):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent", **_fleet_health(), **kw)


def _seam(**verdict):
    base = {"name": dnp.CHECK_NAME, "ok": True, "fatal": False,
            "detail": "1 node paused: 192.0.2.5 \"training\" "
                      "(since 2026-11-21T18:00:00+00:00, until open-ended)",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def test_the_row_renders_once_from_its_seam(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _node_pauses_check=_seam())

    rows = [c for c in res["checks"] if c["name"] == dnp.CHECK_NAME]
    assert len(rows) == 1, "the registry must render this row exactly once"
    assert rows[0]["ok"] is True and rows[0]["fatal"] is False
    assert "192.0.2.5" in rows[0]["detail"]
    assert dnp.CHECK_NAME not in res["fatal_failures"], \
        "an info row must not sink the preflight"


def test_the_row_refuses_without_its_seam(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch)

    row = {c["name"]: c for c in res["checks"]}[dnp.CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "refused inside a pytest process" in row["detail"], \
        "uninjected inside a suite must be an honest refusal, never a pass"


def test_the_row_declares_no_root_context():
    """The wiring half of t_be690204, restated for this row.

    node_pauses resolves its default root from HOME at import and has no
    HSCC_DIR override, so HSCC_DIR is not a relocation of the overlay — it is a
    directory no pause writer can ever reach, and a row handed it would answer
    "nothing is parked" about a fleet with a parked node. ``row_kwargs=()``
    makes that un-wireable rather than merely unwired.
    """
    by_key = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}
    assert by_key["node_pauses"].row_kwargs == (), (
        "the node_pauses row must declare no per-row context: a handed root "
        "splits the reader from the writers that resolve from HOME")


def test_the_fallback_name_matches_the_feature_module():
    """A registry `fallback_name` is a STRING naming a module constant, so the
    two spellings of one check name can only be pinned by comparing them."""
    assert doctor._NODE_PAUSES_CHECK_NAME == dnp.CHECK_NAME


def test_the_row_is_unverifiable_when_its_module_is_missing(monkeypatch):
    """A registry entry whose loader returns None must print ITS OWN
    UNVERIFIABLE row: the `missing_detail` / `missing_fix` strings on the entry
    are otherwise decoration nobody ever reaches, and a missing feature module
    must never render as absence-of-output.

    ``build_row``'s loader branch is the non-pytest branch, so this reaches it
    the way test_doctor_row_registry.py does — a module view whose OWN
    ``sys.modules`` has no pytest, not a global delitem that would break the
    running session.
    """
    spec = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}["node_pauses"]
    monkeypatch.setattr(doctor, "_load_doctor_node_pauses", lambda: None)
    outside = types.SimpleNamespace()
    outside.__dict__.update(vars(doctor))          # AFTER the patch, not before
    outside.sys = types.SimpleNamespace(modules={})
    import sys  # the shared fence reads the PROCESS module table
    # The fence is the shared, child-visible predicate now, not a view of
    # this module's ``sys.modules``: close BOTH process channels, or the
    # refusal branch is taken and the CLI path under test never runs.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    row = doctor_rows.build_row(outside, spec, None)

    assert row.name == dnp.CHECK_NAME
    assert row.ok is False and row.fatal is False
    assert row.detail == spec.missing_detail, "the entry's own text must be used"
    assert row.fix == spec.missing_fix


def test_the_row_names_the_core_when_it_cannot_be_read(tmp_path, monkeypatch):
    """A missing daemon module is an honest 'cannot verify', never absence."""
    monkeypatch.setattr(dnp, "_load_core", lambda: None)

    res = dnp.doctor_check(directory=_root(tmp_path))

    assert res["ok"] is False and res["fatal"] is False
    assert "not importable" in res["detail"]
    assert res["detail"], "an unverifiable row must still print something"


def test_the_warning_capture_is_restored_even_when_the_reader_blows_up(
        tmp_path, monkeypatch):
    """The row borrows node_pauses' logger to hear what `load()` complains
    about; it must give it back. A leaked capture would silence the daemon's
    own WARNs for the rest of the process."""
    core = _core()
    before = core._warn

    def _boom(directory=None):
        raise RuntimeError("simulated blow-up")

    monkeypatch.setattr(core, "load", _boom)
    dnp._read(core, _root(tmp_path))
    assert core._warn is before, "the swap leaked — every later WARN is lost"
