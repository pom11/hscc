"""Doctor coverage for stale sparkrun reservations (t_7dd19947, t_5f419e01).

The defect: a killed sparkrun client leaves a reservation row whose owner pid
is dead, and the next launch fails "Execution resources are busy or require
reconciliation after an interrupted operation". These tests pin, with fakes
ONLY:

* a dead owner pid is NAMED with the machine whose ledger holds it (the card's
  acceptance);
* an alive owner is reported live and left alone;
* nothing is deleted — no reconcile command is ever issued — without the
  explicit ``--fix`` half (``release_stale``);
* an unanswered probe is an ERROR, never "no reservations" (the false-empty
  that would clear the way for a launch that then fails);
* the release path speaks ONLY through sparkrun's two documented
  coordination calls — never the sqlite file — and per owner.

t_5f419e01 adds the half that was missing, and it is pinned in BOTH
directions: a probe that reports a ``platform: "darwin"`` dead-owner row
seeded in the LOCAL ledger, AND the fact that the node leg alone reports that
same fixture clean. A regression to node-only probing cannot look green,
because the row that started this card is a local row and the node leg is
asserted to miss it.

Every transport here is a fake: a remote one keyed by host, a local one keyed
by nothing (there is one "here"). The two are shape-guarded against each other
— a node transport that receives an argv list, or a local runner that receives
a shell string, fails the test rather than answering. No test opens a socket,
spawns a process, imports real sparkrun, or touches any ledger. Fixture hosts
are the documented placeholders (192.0.2.x).
"""

import ast
import base64
import json
import os
import re
import subprocess

import pytest

import doctor
import doctor_sparkrun_reservations as dsr

_B64_RE = re.compile(r"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d \| bash -s")

#: A fixture interpreter path: absolute, obviously not a real install, and
#: never a bare ``python``/``python3`` a PATH could resolve.
FIXTURE_SPARKRUN_PY = "/fixture/venvs/sparkrun/bin/python"


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


def _row(owner, resource, pid, alive, platform=None):
    """One reservation row exactly as sparkrun's ``reservations()`` answers.

    The identity is the LINUX shape (``boot`` + ``created``) — what a DGX
    node's ``process_identity`` writes. ``platform`` is only present when a
    caller names one, which is what keeps a local (darwin) row a distinct
    fixture rather than the same row relabelled.
    """
    identity = {"pid": pid, "boot": "fake-boot", "created": "123"}
    if platform:
        identity = {"pid": pid, "platform": platform, "created": "123"}
    return {"owner": owner, "resource": resource, "process": identity,
            "alive": alive}


def _local_row(owner, resource, pid, alive):
    """A LOCAL ledger row: the darwin identity shape, ``platform`` first-class.

    This is the incident's row type — a locally-spawned sparkrun client's
    claim, holding a ``host:`` resource but living in the gateway's ledger.
    """
    return _row(owner, resource, pid, alive, platform="darwin")


def _probe_answer(rows):
    return json.dumps({"mode": "probe", "reservations": rows})


def _local_pid_alive(pid) -> bool:
    """The LOCAL process table, read the way a local sparkrun client sees it.

    Used ONLY to make a fake's ``alive`` honest: a row is dead here because
    this machine says so, not because a fixture declared it. (sparkrun's own
    compare is stricter — it also matches the identity — and it is
    sparkrun's, not ours: that is why the probe ships the compare rather
    than re-implementing one.)
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # failed inspection is not proof of death
    except OSError:
        return True
    return True


def _dead_local_pid() -> int:
    """A pid THIS machine can currently prove is not a process.

    Scans down from a high pid rather than spawning-and-reaping, so the
    fixture itself never starts a process.
    """
    for cand in range(4_000_000, 4_000_000 - 500, -1):
        if not _local_pid_alive(cand):
            return cand
    raise AssertionError("no provably-dead local pid found — impossible host")


class FakeNode:
    """A fake cluster node: answers probe/reconcile, records every command."""

    def __init__(self, rows=None, fail=False, reconcile_refuse=None):
        self.rows = rows if rows is not None else []
        self.fail = fail
        self.reconcile_refuse = reconcile_refuse  # owner -> error string
        self.commands = []

    def run(self, command, timeout):
        assert isinstance(command, str), (
            f"a NODE transport got a {type(command).__name__} — the local "
            f"argv door is cross-wired into the ssh door")
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


class FakeLocalLedger:
    """THIS machine's ledger, answered by a fake that never spawns anything.

    Same contract as ``FakeNode`` on purpose — one probe, per-owner reconcile,
    a live owner that refuses — with two differences that ARE the card:

    * it receives an ARGV LIST, not a shell string (a local probe is an exec,
      never a bash pipe);
    * ``alive`` is judged against THIS host's process table, via
      ``_local_pid_alive``, so a seeded pid is dead/alive because this machine
      says so rather than because a fixture typed it.
    """

    def __init__(self, rows=None, fail=False, reconcile_refuse=None):
        self.rows = rows if rows is not None else []
        self.fail = fail
        self.reconcile_refuse = reconcile_refuse
        self.commands = []

    def answer(self, argv, timeout):
        assert isinstance(argv, list), (
            f"the LOCAL runner got a {type(argv).__name__} — a node command "
            f"string is cross-wired into the local door")
        assert argv[0] == "-c" and argv[1] == dsr._PROBE_PY, (
            "the local leg must run the SAME embedded payload the remote "
            "ships, not a second implementation")
        mode, owner = argv[2], argv[3]
        self.commands.append((mode, owner, timeout))
        if self.fail:
            return {"ok": False, "stdout": "", "code": None,
                    "stderr": "hscc's sparkrun venv python is unresolvable"}
        if mode == "probe":
            rows = [{**r, "alive": _local_pid_alive(r["process"]["pid"])}
                    for r in self.rows]
            return {"ok": True, "stdout": _probe_answer(rows), "stderr": "",
                    "code": 0}
        live = {r["owner"] for r in self.rows
                if _local_pid_alive(r["process"]["pid"])}
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


def _clean_local():
    """A local ledger that answered, and answered EMPTY.

    The default every remote-focused case needs — and it is stated at each
    call site rather than autoused, because "this leg was never asked" is
    exactly the state that must never read as clean.
    """
    return FakeLocalLedger(rows=[]).answer


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
        **{"192.0.2.10": a, "192.0.2.11": b}), local_runner=_clean_local())
    assert res["ok"] is False and res["fatal"] is False
    assert "host:192.0.2.10 owner de11 pid 4242 on 192.0.2.10" in res["detail"]
    assert "host:192.0.2.11 owner f00d pid 99 on 192.0.2.11" in res["detail"]
    assert "--fix" in res["fix"]


def test_row_green_when_only_live_owners():
    node = FakeNode(rows=[_row("beef", "host:192.0.2.10", 7, True)])
    res = dsr.check(["192.0.2.10"], ssh_factory=_table(
        **{"192.0.2.10": node}), local_runner=_clean_local())
    assert res["ok"] is True
    assert "live" in res["detail"]


def test_row_error_when_one_node_unreachable():
    good = FakeNode(rows=[])
    bad = FakeNode(fail=True)
    res = dsr.check(["192.0.2.10", "192.0.2.12"], ssh_factory=_table(
        **{"192.0.2.10": good, "192.0.2.12": bad}),
        local_runner=_clean_local())
    assert res["ok"] is False
    assert "UNVERIFIABLE" in res["detail"] and "NOT verified" in res["detail"]


def test_row_never_fatal_even_when_stale():
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 1, False)])
    res = dsr.check(["192.0.2.10"], ssh_factory=_table(
        **{"192.0.2.10": node}), local_runner=_clean_local())
    assert res["fatal"] is False


def test_unresolvable_hosts_is_honest_error_not_pass():
    res = dsr.check([], ssh_factory=_table(), local_runner=_clean_local())
    assert res["ok"] is False
    assert "NOT verified" in res["detail"]


def test_probe_row_issues_no_reconcile_anywhere():
    """Card acceptance, third bullet: nothing is deleted without the explicit
    flag. A plain check — even one that FINDS stale rows — never sends a
    reconcile command, on EITHER leg."""
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    local = FakeLocalLedger(rows=[_local_row("dead", "host:192.0.2.10",
                                             _dead_local_pid(), False)])
    dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}),
               local_runner=local.answer)
    assert [m for (m, _o, _t) in node.commands] == ["probe"]
    assert [m for (m, _o, _t) in local.commands] == ["probe"]


# ── the --fix half ─────────────────────────────────────────────────────────

def test_fix_releases_only_dead_owner_rows():
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False),
                          _row("beef", "destination:ab", 7, True)])
    lines = dsr.release_stale(["192.0.2.10"],
                              ssh_factory=_table(**{"192.0.2.10": node}),
                              local_runner=_clean_local())
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
                              ssh_factory=_table(**{"192.0.2.10": node}),
                              local_runner=_clean_local())
    assert any("LEFT" in ln and "still alive" in ln for ln in lines)
    assert [r["owner"] for r in node.rows] == ["de11"]  # still there


def test_fix_never_touches_a_node_that_did_not_answer():
    node = FakeNode(fail=True)
    lines = dsr.release_stale(["192.0.2.12"],
                              ssh_factory=_table(**{"192.0.2.12": node}),
                              local_runner=_clean_local())
    assert [m for (m, _o, _t) in node.commands] == ["probe"], \
        "a failed probe must not be followed by a reconcile"
    assert any("NOT touched" in ln for ln in lines)


def test_fix_on_clean_fleet_writes_nothing():
    node = FakeNode(rows=[_row("beef", "host:192.0.2.10", 7, True)])
    lines = dsr.release_stale(["192.0.2.10"],
                              ssh_factory=_table(**{"192.0.2.10": node}),
                              local_runner=_clean_local())
    assert lines == []
    assert [m for (m, _o, _t) in node.commands] == ["probe"]


def test_fix_releases_each_stale_owner_with_its_own_call():
    node = FakeNode(rows=[_row("aa11", "host:192.0.2.10", 1, False),
                          _row("bb22", "host:192.0.2.10", 2, False)])
    dsr.release_stale(["192.0.2.10"],
                      ssh_factory=_table(**{"192.0.2.10": node}),
                      local_runner=_clean_local())
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


# ── the LOCAL leg: the same calls, run here (t_5f419e01) ───────────────────

def test_local_argv_runs_the_same_payload_the_remote_ships():
    """"Local" means the SAME calls, run here — not a second implementation.

    The single strongest pin on the card's fix: the embedded python the
    remote leg base64's and ships to a node is BYTE-IDENTICAL to the one the
    local leg passes as an argv element. Two payloads would mean two
    chances to drift into reading the ledger ourselves.
    """
    shipped = _payload_of(dsr.remote_command(dsr.PROBE_MODE))
    local = dsr.local_argv(dsr.PROBE_MODE)
    assert local[0] == "-c", "the local probe is an exec, not a shell string"
    assert local[1] == shipped
    assert dsr.local_argv(dsr.RECONCILE_MODE, "a1b2c3")[2:] == [
        dsr.RECONCILE_MODE, "a1b2c3"]


def test_local_argv_passes_the_same_mode_and_owner_gate():
    with pytest.raises(ValueError):
        dsr.local_argv("drop-table")
    with pytest.raises(ValueError):
        dsr.local_argv(dsr.RECONCILE_MODE, "x'; rm -rf /; #")


def test_local_dead_owner_row_is_reported_stale():
    """Card acceptance, bullet 1a: a ``platform: "darwin"`` dead-owner row
    seeded in the LOCAL ledger is reported stale — named, with its pid."""
    dead = _dead_local_pid()
    assert _local_pid_alive(dead) is False, "fixture: the pid is really dead"
    local = FakeLocalLedger(rows=[_local_row(
        "a5f70df0", "host:192.0.2.10", dead, alive=False)])

    rep = dsr.check_local(run=local.answer)

    assert rep["verdict"] == dsr.V_STALE
    assert rep["stale"] == [{"owner": "a5f70df0", "resource": "host:192.0.2.10",
                             "pid": dead, "platform": "darwin"}]
    assert [m for (m, _o, _t) in local.commands] == ["probe"]


def test_the_node_leg_alone_would_have_missed_this_row():
    """Card acceptance, bullet 1b — the half that makes a regression loud.

    The incident's row is LOCAL; every node's own ledger is clean. So the
    node leg, on its own, answers CLEAN for the very fixture state that
    blocks a launch. Any change that drops the local leg reddens this test,
    because the row it is paired with cannot be found any other way.
    """
    dead = _dead_local_pid()
    node = FakeNode(rows=[])                  # the node ledger: genuinely empty
    local = FakeLocalLedger(rows=[_local_row(
        "a5f70df0", "host:192.0.2.10", dead, alive=False)])

    remote_only = dsr.check_host("192.0.2.10", run=node.run)
    assert remote_only["verdict"] == dsr.V_OK, (
        "the node leg must report CLEAN for this fixture — that is the miss")

    both = dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}),
                     local_runner=local.answer)
    assert both["ok"] is False
    assert f"pid {dead} on {dsr.LOCAL_TARGET}" in both["detail"]
    assert "a5f70df0" in both["detail"]


def test_local_leg_is_answered_with_no_cluster_hosts_at_all():
    """The incident host is not a fleet node, so the local leg cannot depend
    on the cluster being resolvable: with ZERO hosts the local answer still
    stands on its own — and the missing node coverage is still reported."""
    dead = _dead_local_pid()
    local = FakeLocalLedger(rows=[_local_row(
        "a5f70df0", "host:192.0.2.10", dead, alive=False)])

    res = dsr.check([], local_runner=local.answer)

    assert res["ok"] is False
    assert f"on {dsr.LOCAL_TARGET}" in res["detail"]
    assert "NOT verified" in res["detail"], "zero nodes is never a clean"


def test_a_confirmed_local_stale_row_outranks_an_unreachable_node():
    """Naming a dead owner is the row's whole purpose; a node we could not
    reach must not bury it behind an UNVERIFIABLE."""
    dead = _dead_local_pid()
    bad = FakeNode(fail=True)
    local = FakeLocalLedger(rows=[_local_row(
        "a5f70df0", "host:192.0.2.10", dead, alive=False)])

    res = dsr.check(["192.0.2.12"], ssh_factory=_table(**{"192.0.2.12": bad}),
                    local_runner=local.answer)

    assert res["ok"] is False
    assert "stale reservation(s)" in res["detail"]
    assert "UNVERIFIABLE" in res["detail"]


def test_local_and_node_stale_rows_are_named_with_their_own_target():
    """A resource naming a node is not a claim held by that node: the detail
    says WHICH MACHINE owns the ledger, because that is what ``--fix`` has to
    know before it can reach the right process table."""
    dead = _dead_local_pid()
    node = FakeNode(rows=[_row("nodestale", "host:192.0.2.10", 4242, False)])
    local = FakeLocalLedger(rows=[_local_row("localstale", "host:192.0.2.11",
                                             dead, alive=False)])

    res = dsr.check(["192.0.2.10"], ssh_factory=_table(**{"192.0.2.10": node}),
                    local_runner=local.answer)

    assert "owner nodestale pid 4242 on 192.0.2.10" in res["detail"]
    assert f"owner localstale pid {dead} on {dsr.LOCAL_TARGET}" in res["detail"]


def test_alive_local_pid_is_live_and_left_alone():
    """Card acceptance, bullet 2: liveness for a local row is judged against
    the LOCAL pid — this process is running right now, so its row is LIVE and
    nothing is released."""
    alive = os.getpid()
    assert _local_pid_alive(alive) is True, "fixture: we are alive"
    local = FakeLocalLedger(rows=[_local_row(
        "beef0001", "host:192.0.2.10", alive, alive=False)])

    rep = dsr.check_local(run=local.answer)

    assert rep["verdict"] == dsr.V_LIVE, \
        "an ALIVE local pid must never read as stale"
    assert rep["live"] == [{"owner": "beef0001", "resource": "host:192.0.2.10",
                            "pid": alive, "platform": "darwin"}]


def test_local_probe_failure_is_error_never_empty():
    local = FakeLocalLedger(fail=True)
    rep = dsr.check_local(run=local.answer)
    assert rep["verdict"] == dsr.V_ERROR
    assert "unresolvable" in rep["error"]


def test_ledgers_never_answer_the_wrong_door():
    """Shape guard, both directions: an argv list belongs to the local runner
    and a shell string to a node transport. A code path that confuses them
    gets an AssertionError from the fake, not an answer."""
    with pytest.raises(AssertionError, match="cross-wired"):
        FakeLocalLedger().answer(dsr.remote_command(dsr.PROBE_MODE), 5)
    with pytest.raises(AssertionError, match="cross-wired"):
        FakeNode().run(dsr.local_argv(dsr.PROBE_MODE), 5)


def test_fix_releases_a_dead_local_owner_and_never_the_live_one():
    """``--fix`` reaches the local ledger too — one reconcile, and only for
    the owner whose pid this machine can prove is gone."""
    dead, alive = _dead_local_pid(), os.getpid()
    local = FakeLocalLedger(rows=[_local_row("de11", "host:192.0.2.10", dead,
                                             alive=False),
                                  _local_row("beef", "host:192.0.2.11", alive,
                                             alive=True)])

    lines = dsr.release_stale([], local_runner=local.answer)

    assert [(m, o) for (m, o, _t) in local.commands] == [("probe", ""),
                                                         ("reconcile", "de11")]
    assert any(ln.startswith("local: released owner de11") for ln in lines)
    assert [r["owner"] for r in local.rows] == ["beef"]


def test_fix_never_touches_a_local_ledger_that_did_not_answer():
    """Fail-closed is not a remote-only rule: an unread local ledger is
    neither clean nor deletable."""
    local = FakeLocalLedger(rows=[_local_row("de11", "host:192.0.2.10",
                                             _dead_local_pid(), False)],
                            fail=True)

    lines = dsr.release_stale([], local_runner=local.answer)

    assert [m for (m, _o, _t) in local.commands] == ["probe"]
    assert any("local: NOT touched" in ln for ln in lines)
    assert len(local.rows) == 1


def test_fix_lets_sparkrun_refuse_a_live_local_owner():
    """sparkrun's own guard is the last word on the local half too.

    The race this models is real: the probe said dead, and by the time
    ``reconcile_reservation`` re-checks inside its own ``BEGIN IMMEDIATE``
    the owner is back. We report LEFT, never force, and the row stays.
    """
    local = FakeLocalLedger(
        rows=[_local_row("de11", "host:192.0.2.10", _dead_local_pid(), False)],
        reconcile_refuse={"de11": "The reservation owner is still alive"})

    lines = dsr.release_stale([], local_runner=local.answer)

    assert [(m, o) for (m, o, _t) in local.commands] == [("probe", ""),
                                                         ("reconcile", "de11")]
    assert any("LEFT" in ln and "still alive" in ln for ln in lines)
    assert [r["owner"] for r in local.rows] == ["de11"], "the row survives"


def test_a_locally_live_owner_is_never_even_nominated_for_release():
    """The cheaper guarantee, upstream of the race: an owner this machine
    reports alive is never passed to reconcile at all."""
    local = FakeLocalLedger(rows=[_local_row(
        "beef", "host:192.0.2.10", os.getpid(), alive=True)])

    lines = dsr.release_stale([], local_runner=local.answer)

    assert [m for (m, _o, _t) in local.commands] == ["probe"]
    assert lines == []
    assert [r["owner"] for r in local.rows] == ["beef"]


def test_production_local_runner_refuses_inside_pytest():
    """The most dangerous door is the one nearest home: without the fence an
    unforsaken suite would probe — and on --fix, could reach — the OPERATOR'S
    OWN ledger."""
    run = dsr._default_local_runner()
    res = run(dsr.local_argv(dsr.PROBE_MODE), 5)
    assert res["ok"] is False
    assert "refused" in res["stderr"]


def test_check_without_a_local_injection_cannot_read_a_real_ledger():
    """A suite that forgets the new seam gets an honest UNVERIFIABLE naming
    the local target — never a clean row built on a ledger nobody read."""
    node = FakeNode(rows=[])
    res = dsr.check(["192.0.2.10"], ssh_factory=_table(
        **{"192.0.2.10": node}))
    assert res["ok"] is False
    assert dsr.LOCAL_TARGET in res["detail"]


def test_production_local_runner_execs_under_sparkruns_own_python_only(
        monkeypatch):
    """What production DOES spawn, pinned: argv[0] is the RESOLVED sparkrun venv
    interpreter — never a PATH ``python3``, never ``shell=True`` — followed by
    the shared payload, and the timeout is forwarded."""
    monkeypatch.setattr(dsr.doctor_rows, "under_pytest", lambda: False)
    seen = {}

    def fake_spawn(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"mode": "probe", "reservations": []}), "")

    monkeypatch.setattr(dsr.subprocess, "run", fake_spawn)
    run = dsr._default_local_runner(resolve_local=lambda: FIXTURE_SPARKRUN_PY)
    res = run(dsr.local_argv(dsr.PROBE_MODE), 7)

    assert res["ok"] is True
    assert seen["argv"][0] == FIXTURE_SPARKRUN_PY
    assert seen["argv"][1:] == dsr.local_argv(dsr.PROBE_MODE)
    assert "shell" not in seen["kwargs"] or seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 7


def test_unresolvable_local_venv_never_spawns_a_default_interpreter(
        monkeypatch):
    """The failure this pins is the v1.15.0 class in a new costume: a probe
    that cannot find its interpreter falls back to SOME python, gets an empty
    answer, and reports a clean ledger. Here the answer is an error and
    NOTHING is spawned."""
    monkeypatch.setattr(dsr.doctor_rows, "under_pytest", lambda: False)

    def no_spawn(*_a, **_k):
        raise AssertionError("a subprocess must not spawn when the venv is "
                             "unresolvable")

    monkeypatch.setattr(dsr.subprocess, "run", no_spawn)
    run = dsr._default_local_runner(resolve_local=lambda: None)
    res = run(dsr.local_argv(dsr.PROBE_MODE), 5)
    assert res["ok"] is False
    assert "unresolvable" in res["stderr"]
    assert dsr.check_local(run=run)["verdict"] == dsr.V_ERROR


def test_local_probe_never_names_a_ledger_path_or_a_connection():
    """Card acceptance, bullet 4: the local leg changed nothing about the
    boundary. No sqlite import, no ``connect()`` call, anywhere in the module
    — checked structurally, so a reworded comment cannot hide a new path."""
    imports, calls = set(), []
    for node in ast.walk(ast.parse(
            open(dsr.__file__, encoding="utf-8").read())):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                calls.append(fn.attr)
    assert "sqlite3" not in imports
    assert "connect" not in calls


def test_both_legs_share_one_classifier_and_one_release_rule():
    """The two doors are different transports; what an ANSWER means is ONE
    function. Pinned by behaviour: the same fake answer through either door
    yields the same report, so a future fork of one leg's rules reddens here."""
    rows = [_row("de11", "host:192.0.2.10", 4242, False)]
    node = FakeNode(rows=list(rows))
    rep_node = dsr.check_host("192.0.2.10", run=node.run)

    dead = _dead_local_pid()
    local = FakeLocalLedger(rows=[_local_row("de11", "host:192.0.2.10", dead,
                                             False)])
    rep_local = dsr.check_local(run=local.answer)

    assert rep_node["verdict"] == rep_local["verdict"] == dsr.V_STALE
    assert [e["owner"] for e in rep_node["stale"]] == \
        [e["owner"] for e in rep_local["stale"]]


# ── run_doctor wiring ──────────────────────────────────────────────────────

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.10"]}])


def _fleet_stubs():
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.10"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/hscc-fixture/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _patch_transport(monkeypatch, table, local=None):
    """Production default in run_doctor's wiring = a host-keyed factory and,
    for the local leg, the caller's fake ledger (default: an empty one)."""
    monkeypatch.setattr(
        dsr, "_default_ssh_factory",
        lambda: (lambda host: table.get(host, dsr._refused_transport(
            "no fake transport for host"))))
    monkeypatch.setattr(
        dsr, "_default_local_runner", lambda resolve_local=None: (
            local if local is not None else _clean_local()))


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


def test_run_doctor_names_a_local_stale_owner(tmp_path, monkeypatch):
    """The row a real operator saw: nothing on any node, one dead darwin
    owner in the gateway's own ledger. run_doctor renders it, NAMED, and it
    is the incident this card exists for."""
    dead = _dead_local_pid()
    node = FakeNode(rows=[])
    local = FakeLocalLedger(rows=[_local_row(
        "a5f70df0", "host:192.0.2.10", dead, alive=False)])
    _patch_transport(monkeypatch, {"192.0.2.10": node.run}, local=local.answer)
    home = _base_env(tmp_path, monkeypatch)
    res = doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent", **_fleet_stubs())
    row = next(c for c in res["checks"] if c["name"] == dsr.CHECK_NAME)
    assert row["ok"] is False and row["fatal"] is False
    assert "a5f70df0" in row["detail"] and str(dead) in row["detail"]
    assert dsr.LOCAL_TARGET in row["detail"]


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
        ssh_factory={"192.0.2.10": node.run},
        local_runner=_clean_local())
    assert ("reconcile", "de11") in [(m, o) for (m, o, _t) in node.commands]
    assert any("released owner de11" in ln for ln in res["fixes_applied"])


def test_fix_dispatch_releases_a_local_stale_owner(tmp_path):
    """And the same flag reaches the LOCAL half through its own seam — still
    only a dead owner, still one reconcile, still nowhere near a node."""
    dead = _dead_local_pid()
    local = FakeLocalLedger(rows=[_local_row("a5f70df0", "host:192.0.2.10",
                                             dead, alive=False)])
    home = tmp_path / "home"
    home.mkdir()
    (home / "hermes-agent").mkdir()
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [test]\n")
    res = doctor.run_doctor_fix(
        config_path=str(config), hermes_home=str(home),
        _cluster_runner=lambda: FAKE_CLUSTER,
        ssh_factory={"192.0.2.10": FakeNode(rows=[]).run},
        local_runner=local.answer)
    assert [(m, o) for (m, o, _t) in local.commands] == [("probe", ""),
                                                         ("reconcile",
                                                          "a5f70df0")]
    assert any("released owner a5f70df0" in ln for ln in res["fixes_applied"])
    assert not any("192.0.2.10: released" in ln for ln in res["fixes_applied"])


def test_fix_dispatch_never_releases_locally_on_an_unanswered_probe(tmp_path):
    """The dispatch level, unfaked: production under pytest is a REFUSED local
    runner, so --fix says NOT touched and issues no reconcile — the same
    sentence the node leg has always had, now true of the local leg too."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "hermes-agent").mkdir()
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [test]\n")
    node = FakeNode(rows=[_row("de11", "host:192.0.2.10", 4242, False)])
    res = doctor.run_doctor_fix(
        config_path=str(config), hermes_home=str(home),
        _cluster_runner=lambda: FAKE_CLUSTER,
        ssh_factory={"192.0.2.10": node.run})
    assert any("local: NOT touched" in ln for ln in res["fixes_applied"])
    assert [(m, o) for (m, o, _t) in node.commands] == [("probe", ""),
                                                        ("reconcile", "de11")]


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


def test_fix_without_local_injection_cannot_release_locally():
    """The local fence has teeth on the mutating half too: production under
    pytest is a REFUSED runner, so --fix reports NOT touched and never builds
    a reconcile argv against the operator's ledger."""
    lines = dsr.release_stale([])
    assert any("local: NOT touched" in ln and "refused" in ln for ln in lines)
