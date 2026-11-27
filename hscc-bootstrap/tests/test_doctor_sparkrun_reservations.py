"""Doctor coverage for stale sparkrun reservations (t_7dd19947).

The defect: a killed sparkrun client leaves a reservation row whose owner pid
is dead, and the next launch fails "Execution resources are busy or require
reconciliation after an interrupted operation". These tests pin, with fakes
ONLY:

* a dead owner pid is NAMED with its node (the card's acceptance);
* an alive owner is reported live and left alone;
* nothing is deleted — no reconcile command is ever issued — without the
  explicit ``--fix`` half (``release_stale``);
* an unanswered probe is an ERROR, never "no reservations" (the false-empty
  that would clear the way for a launch that then fails);
* the release path speaks ONLY through sparkrun's two documented
  coordination calls — never the sqlite file — and per owner.

Every transport here is a fake keyed by host; no test opens a socket, imports
real sparkrun, or touches any ledger. Fixture hosts are the documented
placeholders (192.0.2.x).
"""

import base64
import json
import re

import pytest

import doctor
import doctor_sparkrun_reservations as dsr

_B64_RE = re.compile(r"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d \| bash -s")


def _decode(command: str) -> str:
    """The bash a fake node 'receives' — decode the base64 payload."""
    m = _B64_RE.search(command)
    assert m, f"unexpected remote command shape: {command[:80]!r}"
    return base64.b64decode(m.group(1)).decode()


def _argv(command: str):
    """The positional argv baked onto the remote bash: (mode, owner).

    Regex over the TAIL after the ``"$py" -c`` call: four trailing
    single-quoted literals (mode, owner, '', base64 payload). The template
    body has its own quotes, hence the tail anchor.
    """
    src = _decode(command)
    tail = src[src.rindex('"$py" -c'):]
    q = chr(39)
    pat = (q + "([^" + q + "]*)" + q + " " + q + "([^" + q + "]*)" + q
           + " " + q + q + " " + q + "([A-Za-z0-9+/=]+)" + q + "[ ]*$")
    m = re.search(pat, tail)
    assert m, f"no four-literal argv tail: {tail[-60:]!r}"
    return m.group(1), m.group(2)


def _payload_of(command: str) -> str:
    """The embedded remote python, decoded off the command."""
    src = _decode(command)
    tail = src[src.rindex('"$py" -c'):]
    b64 = re.search(r"[A-Za-z0-9+/=]{40,}", tail).group(0)
    return base64.b64decode(b64).decode()


def _mode_of(command: str) -> str:
    return _argv(command)[0]


def _owner_of(command: str) -> str:
    """The owner argv baked into the remote bash (empty for a probe)."""
    return _argv(command)[1]


def _row(owner, resource, pid, alive):
    """One reservation row exactly as sparkrun's ``reservations()`` answers."""
    return {"owner": owner, "resource": resource,
            "process": {"pid": pid, "boot": "fake-boot", "created": "123"},
            "alive": alive}


def _probe_answer(rows):
    return json.dumps({"mode": "probe", "reservations": rows})


class FakeNode:
    """A fake cluster node: answers probe/reconcile, records every command."""

    def __init__(self, rows=None, fail=False, reconcile_refuse=None):
        self.rows = rows if rows is not None else []
        self.fail = fail
        self.reconcile_refuse = reconcile_refuse  # owner -> error string
        self.commands = []

    def run(self, command, timeout):
        mode = _mode_of(command)
        owner = _owner_of(command)
        self.commands.append((mode, owner, timeout))
        if self.fail:
            return {"ok": False, "stdout": "", "code": 255,
                    "stderr": "ssh: connect to host 192.0.2.12 port 22: "
                              "Operation timed out"}
        if mode == "probe":
            return {"ok": True, "stdout": _probe_answer(self.rows),
                    "stderr": "", "code": 0}
        # Mirror sparkrun's own guard: a LIVE owner is never released.
        live = {r["owner"] for r in self.rows if r.get("alive")}
        if owner in live:
            refuse = "The reservation owner is still alive"
        else:
            refuse = (self.reconcile_refuse or {}).get(owner)
        if refuse:
            return {"ok": True, "stdout": json.dumps(
                {"mode": mode, "owner": owner, "released": False,
                 "error": refuse}), "stderr": "", "code": 0}
        self.rows = [r for r in self.rows if r["owner"] != owner]
        return {"ok": True, "stdout": json.dumps(
            {"mode": mode, "owner": owner, "released": True}),
            "stderr": "", "code": 0}


def _table(**nodes):
    return {host: node.run for host, node in nodes.items()}


# ── the remote contract ────────────────────────────────────────────────────

def test_probe_command_names_only_the_documented_sparkrun_calls():
    """The ledger is sparkrun's: the shipped remote python may name ONLY
    ``reservations`` / ``reconcile_reservation`` (+ config plumbing). No
    sqlite, no ledger file path, no SQL — hscc never opens the DB."""
    probe = _payload_of(dsr.remote_command(dsr.PROBE_MODE))
    assert "from sparkrun.core.coordination import" in probe
    assert "reservations(root)" in probe
    for banned in ("sqlite", "operations", "DELETE", "PRAGMA", "SELECT"):
        assert banned not in probe, f"the probe reaches for {banned!r}"


def test_reconcile_command_carries_one_owner_and_acknowledges():
    cmd = dsr.remote_command(dsr.RECONCILE_MODE, "a1b2c3")
    assert "acknowledge_effects=True" in _payload_of(cmd)
    assert _owner_of(cmd) == "a1b2c3"


def test_probe_carries_no_owner_and_reconcile_mode_branch_is_guarded():
    assert _owner_of(dsr.remote_command(dsr.PROBE_MODE)) == ""
    src = _payload_of(dsr.remote_command(dsr.PROBE_MODE))
    assert "if mode=='probe'" in src


def test_non_hex_owner_is_refused_before_any_interpolation():
    with pytest.raises(ValueError):
        dsr.remote_command(dsr.RECONCILE_MODE, "x'; rm -rf /; #")


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        dsr.remote_command("drop-table", "")


# ── one node ───────────────────────────────────────────────────────────────

def test_dead_owner_is_stale_and_named():
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    rep = dsr.check_host("192.0.2.10", run=node.run)
    assert rep["verdict"] == dsr.V_STALE
    assert rep["stale"] == [{"owner": "de11", "resource": "host:192.0.2.10",
                             "pid": 4242}]


def test_alive_owner_is_live_and_left_alone():
    node = FakeNode(rows=[_row("beef", "host:192.0.2.10", 7, True)])
    rep = dsr.check_host("192.0.2.10", run=node.run)
    assert rep["verdict"] == dsr.V_LIVE
    assert not rep["stale"]
    # the probe issues exactly one command and it is a probe
    assert [m for (m, _o, _t) in node.commands] == ["probe"]


def test_empty_ledger_answers_clean_and_is_not_an_error():
    node = FakeNode(rows=[])
    rep = dsr.check_host("192.0.2.10", run=node.run)
    assert rep["verdict"] == dsr.V_OK


def test_unreachable_node_is_error_not_empty():
    node = FakeNode(fail=True)
    rep = dsr.check_host("192.0.2.12", run=node.run)
    assert rep["verdict"] == dsr.V_ERROR
    assert "timed out" in rep["error"]


def test_unparseable_answer_is_error_not_empty():
    def junk(command, timeout):
        return {"ok": True, "stdout": "Welcome to DGX Spark\n", "code": 0,
                "stderr": ""}
    rep = dsr.check_host("192.0.2.10", run=junk)
    assert rep["verdict"] == dsr.V_ERROR


def test_unreadable_liveness_is_error_never_deletable():
    row = {"owner": "cafe", "resource": "host:192.0.2.10", "process": {}}
    node = FakeNode(rows=[row])
    rep = dsr.check_host("192.0.2.10", run=node.run)
    assert rep["verdict"] == dsr.V_ERROR
    assert not rep["stale"]


def test_reservations_answer_under_banner_lines_is_read():
    """Remote motd lines ride along; the script's JSON still parses."""
    def motd(command, timeout):
        return {"ok": True, "stderr": "", "code": 0,
                "stdout": "Welcome to Ubuntu\n" + _probe_answer([])}
    rep = dsr.check_host("192.0.2.10", run=motd)
    assert rep["verdict"] == dsr.V_OK


# ── the doctor row ─────────────────────────────────────────────────────────

def test_row_names_the_dead_owner_with_its_node():
    """Card acceptance, first bullet: 'a dead owner pid is named with its
    node'."""
    a = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    b = FakeNode(rows=[_row("f00d", "host:192.0.2.11", 99, False)])
    res = dsr.check(["192.0.2.10", "192.0.2.11"], ssh_factory=_table(
        **{"192.0.2.10": a, "192.0.2.11": b}))
    assert res["ok"] is False and res["fatal"] is False
    assert "host:192.0.2.10 owner de11 pid 4242 on 192.0.2.10" in res["detail"]
    assert "host:192.0.2.11 owner f00d pid 99 on 192.0.2.11" in res["detail"]
    assert "--fix" in res["fix"]


def test_row_green_when_only_live_owners():
    node = FakeNode(rows=[_row("beef", "host:192.0.2.10", 7, True)])
    res = dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}))
    assert res["ok"] is True
    assert "live" in res["detail"]


def test_row_error_when_one_node_unreachable():
    good = FakeNode(rows=[])
    bad = FakeNode(fail=True)
    res = dsr.check(["192.0.2.10", "192.0.2.12"], ssh_factory=_table(
        **{"192.0.2.10": good, "192.0.2.12": bad}))
    assert res["ok"] is False
    assert "UNVERIFIABLE" in res["detail"] and "NOT verified" in res["detail"]


def test_row_never_fatal_even_when_stale():
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 1, False)])
    res = dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}))
    assert res["fatal"] is False


def test_unresolvable_hosts_is_honest_error_not_pass():
    res = dsr.check([], ssh_factory=_table())
    assert res["ok"] is False
    assert "NOT verified" in res["detail"]


def test_probe_row_issues_no_reconcile_anywhere():
    """Card acceptance, third bullet: nothing is deleted without the explicit
    flag. A plain check — even one that FINDS stale rows — never sends a
    reconcile command."""
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}))
    assert [m for (m, _o, _t) in node.commands] == ["probe"]


# ── the --fix half ─────────────────────────────────────────────────────────

def test_fix_releases_only_dead_owner_rows():
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False),
                          _row("beef", "destination:ab", 7, True)])
    lines = dsr.release_stale(["192.0.2.10"],
                              ssh_factory=_table(**{"192.0.2.10": node}))
    modes_owners = [(m, o) for (m, o, _t) in node.commands]
    assert modes_owners == [("probe", ""), ("reconcile", "de11")], \
        "one probe, then exactly the DEAD owner reconciled — never 'beef'"
    assert any("released owner de11" in ln for ln in lines)
    # the fake's own rows prove it: dead gone, live still there
    assert [r["owner"] for r in node.rows] == ["beef"]


def test_fix_lets_sparkrun_refuse_a_live_owner():
    """Even a wrongly-passed owner cannot be force-deleted: sparkrun's own
    guard answers, and we report LEFT rather than forcing."""
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 1, False)],
                    reconcile_refuse={"de11": "The reservation owner is "
                                              "still alive"})
    lines = dsr.release_stale(["192.0.2.10"],
                              ssh_factory=_table(**{"192.0.2.10": node}))
    assert any("LEFT" in ln and "still alive" in ln for ln in lines)
    assert [r["owner"] for r in node.rows] == ["de11"]  # still there


def test_fix_never_touches_a_node_that_did_not_answer():
    node = FakeNode(fail=True)
    lines = dsr.release_stale(["192.0.2.12"],
                              ssh_factory=_table(**{"192.0.2.12": node}))
    assert [m for (m, _o, _t) in node.commands] == ["probe"], \
        "a failed probe must not be followed by a reconcile"
    assert any("NOT touched" in ln for ln in lines)


def test_fix_on_clean_fleet_writes_nothing():
    node = FakeNode(rows=[_row("beef", "host:192.0.2.10", 7, True)])
    lines = dsr.release_stale(["192.0.2.10"],
                              ssh_factory=_table(**{"192.0.2.10": node}))
    assert lines == []
    assert [m for (m, _o, _t) in node.commands] == ["probe"]


def test_fix_releases_each_stale_owner_with_its_own_call():
    node = FakeNode(rows=[_row("aa11", "host:192.0.2.10", 1, False),
                          _row("bb22", "host:192.0.2.10", 2, False)])
    dsr.release_stale(["192.0.2.10"],
                      ssh_factory=_table(**{"192.0.2.10": node}))
    assert [(m, o) for (m, o, _t) in node.commands] == [
        ("probe", ""), ("reconcile", "aa11"), ("reconcile", "bb22")]


# ── the fence ──────────────────────────────────────────────────────────────

def test_production_transport_refuses_inside_pytest():
    """The suite can never reach the live fleet by forgetting to inject: the
    default factory answers an error for every command, for every host."""
    run = dsr._default_ssh_factory()("192.0.2.10")
    res = run(dsr.remote_command(dsr.PROBE_MODE), 5)
    assert res["ok"] is False
    assert "refused" in res["stderr"]


def test_check_without_injection_reports_unverifiable_in_suite():
    res = dsr.check(["192.0.2.10"])  # no ssh_factory: the fence must hold
    assert res["ok"] is False
    assert "UNVERIFIABLE" in res["detail"]


# ── run_doctor wiring ──────────────────────────────────────────────────────

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.10"]}])


def _fleet_stubs():
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.10"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/hscc-fixture/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _patch_transport(monkeypatch, table):
    """Production default in run_doctor's wiring = a host-keyed factory."""
    monkeypatch.setattr(
        dsr, "_default_ssh_factory",
        lambda: (lambda host: table.get(host, dsr._refused_transport(
            "no fake transport for host"))))


def _base_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stub"))
    return home


def test_run_doctor_renders_the_row_once(tmp_path, monkeypatch):
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    _patch_transport(monkeypatch, {"192.0.2.10": node.run})
    home = _base_env(tmp_path, monkeypatch)
    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent", **_fleet_stubs())
    names = [c["name"] for c in res["checks"]]
    assert names.count(dsr.CHECK_NAME) == 1
    row = next(c for c in res["checks"] if c["name"] == dsr.CHECK_NAME)
    assert row["ok"] is False and row["fatal"] is False
    assert "4242" in row["detail"] and "192.0.2.10" in row["detail"]
    # a stale reservation is a WARN: the preflight verdict does not sink
    assert dsr.CHECK_NAME not in res["fatal_failures"]


def test_run_doctor_fleet_off_reports_skipped(tmp_path, monkeypatch):
    home = _base_env(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER,
                            fleet_checks=False)
    row = next(c for c in res["checks"] if c["name"] == dsr.CHECK_NAME)
    assert row["ok"] is True and "skipped" in row["detail"]


def test_fix_dispatch_releases_with_injected_transport(tmp_path):
    """`--fix` is the ONLY path that releases: run_doctor_fix forwards its
    transport to release_stale and the reconcile command actually goes."""
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    home = tmp_path / "home"
    home.mkdir()
    (home / "hermes-agent").mkdir()
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [test]\n")
    res = doctor.run_doctor_fix(
        config_path=str(config), hermes_home=str(home),
        _cluster_runner=lambda: FAKE_CLUSTER,
        ssh_factory={"192.0.2.10": node.run})
    assert ("reconcile", "de11") in [(m, o) for (m, o, _t) in node.commands]
    assert any("released owner de11" in ln for ln in res["fixes_applied"])


def test_fix_without_injected_transport_cannot_reach_a_node(monkeypatch):
    """What production under pytest gets is a REFUSED transport: the release
    path reports NOT touched and issues no reconcile."""
    seen = []

    def refusing(host):
        def run(command, timeout):
            seen.append(_mode_of(command))
            return {"ok": False, "stdout": "", "code": None,
                    "stderr": "refused"}
        return run

    lines = dsr.release_stale(["192.0.2.10"], ssh_factory=refusing)
    assert seen == ["probe"]
    assert any("NOT touched" in ln for ln in lines)
