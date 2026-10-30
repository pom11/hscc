"""Doctor coverage for the snap firmware-notifier mask (card t_61850b24).

The check is a thin mapper over ``install_notifier_mask.probe_node``; what is
worth testing is the *mapping*, because that is where the honest-signal rule can
silently break:

  * a node that answered "not masked"  -> V_FAIL (a data fact, the card's red);
  * a node we could not read at all    -> V_ERROR (an environment fault), and
    the two must never share a rendering — an operator who cannot tell a broken
    node from an unmasked one has no actionable output;
  * the no-bus failure — the card's named pitfall, whose text
    ``Failed to connect to bus: No medium found`` looks exactly like "state !=
    masked" to a naive parser — must land in V_ERROR, not V_FAIL;
  * opt-out -> V_OPTED_OUT, never green.

Plus the layout/transport guards that would otherwise pass silently: a missing
installer module must name itself, and the production transport must be
unreachable from a pytest process.

Fabricated transport only. No socket, no host, no node; addresses are the
documented 10.0.0.x placeholders.
"""

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doctor_notifier_mask as dnm  # noqa: E402
import install_notifier_mask as nm  # noqa: E402

NODE = "10.0.0.1"
OTHER = "10.0.0.2"
SVC = nm.NOTIFIER_SERVICE
TIMER = nm.NOTIFIER_TIMER


class _Recorder:
    """Callable canned transport that records what it was asked to run.

    A class rather than a function with an attached attribute so the recorded
    calls are a declared field, not a dynamic attribute.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, command, timeout):
        kind = "state" if 'MODE="state"' in _decode(command) else "other"
        self.calls.append({"kind": kind, "command": command})
        res = self.responses.get(kind, {"ok": False, "stdout": "", "code": 127,
                                        "stderr": f"unscripted {kind}"})
        if isinstance(res, str):
            res = {"ok": True, "stdout": res}
        out = {"ok": True, "stdout": "", "stderr": "", "code": 0}
        out.update(res)
        return out


def transport(**responses):
    """Per-command-kind canned answers; ``state`` is what a probe sends.

    A bare string is treated as the stdout of a successful run.
    """
    return _Recorder(responses)


def _decode(command: str) -> str:
    import base64
    import re
    m = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", command)
    return base64.b64decode(m.group(1)).decode() if m else ""


def _string_literals(module, needle):
    """String constants in ``module`` containing ``needle``, docstrings excluded.

    Used instead of ``needle in inspect.getsource(module)`` so that prose
    explaining the bus pitfall does not fail a test that is really asking
    "does this module compose this command?" — a blunt source scan would
    pressure the next reader into deleting the explanation. Docstrings are
    ``ast.Constant`` nodes too, so they are dropped explicitly; f-string text
    pieces are kept, since those are real composed strings.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = list(getattr(node, "body", []))
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and needle in node.value
    ]


def state(service="masked", timer="masked", *, svc_active="inactive",
          timer_active="inactive", rc=0, code_name=None, msg=None):
    out = ["HSCC_STATE_BEGIN: 1",
           "HSCC_MASK_DIR: $HOME/.config/systemd/user",
           "HSCC_UID: 1000",
           "HSCC_XDG_RUNTIME_DIR: /run/user/1000",
           f"HSCC_UNIT_0: {SVC}", f"HSCC_UNIT_0_STATE: {service}",
           f"HSCC_UNIT_0_ACTIVE: {svc_active}", "HSCC_UNIT_0_LINK: /dev/null",
           f"HSCC_UNIT_1: {TIMER}", f"HSCC_UNIT_1_STATE: {timer}",
           f"HSCC_UNIT_1_ACTIVE: {timer_active}", "HSCC_UNIT_1_LINK: /dev/null",
           "HSCC_UNIT_COUNT: 2", "HSCC_STATE_END: 1"]
    if code_name:
        out = [l for l in out if not l.startswith("HSCC_UNIT")]
        out += [f"HSCC_CODE: {code_name}", f"HSCC_MSG: {msg or ''}",
                "HSCC_BUS_TEXT: Failed to connect to bus: No medium found"]
    out.append(f"HSCC_RC: {rc}")
    return "\n".join(out) + "\n"


def factory(**responses):
    return lambda host: transport(**responses)


def provider(**responses):
    """Stand-in for ``_default_ssh_factory`` — a zero-arg callable that hands
    back a per-host transport, which is that function's actual signature.
    Patching the attribute with ``factory(...)`` directly would call the
    per-host lambda with no host and raise TypeError."""
    return lambda: factory(**responses)


def cfg_dir(tmp_path, block):
    """An HSCC_DIR carrying a cluster.json with the given node_recovery block."""
    hscc = tmp_path / ".hscc"
    hscc.mkdir(parents=True, exist_ok=True)
    data = {"name": "fabricated", "hosts": [NODE, OTHER]}
    if block is not None:
        data["node_recovery"] = block
    (hscc / "cluster.json").write_text(json.dumps(data))
    return str(hscc)


# ── verdict mapping: the honest-signal contract ─────────────────────────────

def test_masked_node_is_ok():
    r = dnm.layer(NODE, ssh_cmd=transport(state=state()), enabled=True)
    assert r["verdict"] == dnm.V_OK
    assert any("masked" in n for n in r["notes"])
    assert r["problems"] == []


def test_unmasked_node_is_the_cards_red_not_an_error():
    """The card: doctor goes RED when the notifier is not masked. A node that
    ANSWERED with `static` is a data fact, so V_FAIL — not V_ERROR."""
    r = dnm.layer(NODE, ssh_cmd=transport(state=state(service="static",
                                                      timer="enabled",
                                                      svc_active="active",
                                                      timer_active="active")),
                  enabled=True)
    assert r["verdict"] == dnm.V_FAIL
    assert "NOT MASKED" in r["problems"][0]
    assert "UNVERIFIABLE" not in r["problems"][0]


def test_no_bus_failure_is_an_error_never_a_data_fact():
    """THE pitfall. `systemctl --user` without the session bus prints
    'Failed to connect to bus: No medium found'; read naively that is
    'state != masked' -> red. It is not a statement about the unit."""
    raw = state(rc=nm.RC_NO_BUS, code_name="NO_BUS",
                msg="cannot reach the user manager bus; unit state UNREADABLE")
    r = dnm.layer(NODE, ssh_cmd=transport(state={"ok": False, "stdout": raw,
                                                 "code": nm.RC_NO_BUS}),
                  enabled=True)
    assert r["verdict"] == dnm.V_ERROR
    text = r["problems"][0]
    assert "UNVERIFIABLE" in text and "NOT verified" in text
    assert "No medium found" in text, "the operator must see what systemctl said"
    assert "NOT MASKED" not in text, "an env fault must not read as a fact"


def test_unreachable_node_is_an_error_not_ok_and_not_not_masked():
    r = dnm.layer(NODE, ssh_cmd=transport(
        state={"ok": False, "stdout": "", "code": 255,
               "stderr": "ssh: connect to host: Connection refused"}),
        enabled=True)
    assert r["verdict"] == dnm.V_ERROR
    assert "UNVERIFIABLE" in r["problems"][0]
    assert "Connection refused" in r["problems"][0]


def test_red_and_error_never_share_a_rendering():
    """A data fact and an environment fault must be tellable apart by an
    operator reading the line — pinned, because collapsing them is the exact
    failure this check exists to prevent."""
    red = dnm.layer(NODE, ssh_cmd=transport(state=state(service="static",
                                                        timer="enabled")),
                    enabled=True)["problems"][0]
    err = dnm.layer(NODE, ssh_cmd=transport(
        state={"ok": False, "stdout": "", "code": 255, "stderr": "unreachable"}),
        enabled=True)["problems"][0]
    assert ("NOT MASKED" in red) != ("NOT MASKED" in err)
    assert ("NOT verified" in red) != ("NOT verified" in err)


def test_partially_masked_node_is_red():
    r = dnm.layer(NODE, ssh_cmd=transport(state=state(service="masked",
                                                      timer="enabled",
                                                      timer_active="active")),
                  enabled=True)
    assert r["verdict"] == dnm.V_FAIL
    assert TIMER in r["problems"][0]


def test_foreign_override_is_a_red_line():
    raw = state(service="masked", timer="masked")
    raw = raw.replace("HSCC_UNIT_0_LINK: /dev/null",
                      "HSCC_UNIT_0_LINK: /home/u/own-unit")
    r = dnm.layer(NODE, ssh_cmd=transport(state=raw), enabled=True)
    assert r["verdict"] == dnm.V_FAIL
    assert "not HSCC" in r["problems"][0] or "override" in r["problems"][0]


def test_opt_out_is_opted_out_not_green():
    r = dnm.layer(NODE, ssh_cmd=transport(state=state()), enabled=False)
    assert r["verdict"] == dnm.V_OPTED_OUT
    assert r["problems"] == []
    assert nm.MASK_SUBKEY in r["notes"][0]
    assert "NOT masked" in r["notes"][0]


def test_missing_installer_names_itself_as_an_error():
    """A layout without the installer must report that, not stay silently
    absent (which would read as 'nothing to check')."""
    real = dnm._installer
    dnm._installer = lambda: None
    try:
        r = dnm.layer(NODE, ssh_cmd=transport(state=state()), enabled=True)
        assert r["verdict"] == dnm.V_ERROR
        assert "install_notifier_mask.py" in r["problems"][0]
    finally:
        dnm._installer = real


# ── check() rendering ───────────────────────────────────────────────────────

def test_check_reports_per_node_rows_and_counts():
    def fac(host):
        return transport(state=state() if host == NODE else
                         state(service="static", timer="enabled"))
    res = dnm.check([NODE, OTHER], ssh_factory=fac)
    assert res["ok"] is False
    assert res["name"] == dnm.CHECK_NAME
    assert res["fatal"] is False, "a broken notifier must never block an install"
    assert len(res["rows"]) == 2
    detail = res["detail"]
    assert "1/2 node(s)" in detail
    assert NODE in detail and OTHER in detail
    # every red line names the node
    assert any(OTHER in ln and "[XX]" in ln for ln in detail.splitlines())


def test_check_all_masked_is_ok():
    res = dnm.check([NODE, OTHER], ssh_factory=factory(state=state()))
    assert res["ok"] is True
    assert "2/2 node(s)" in res["detail"]


def test_empty_host_list_is_a_loud_error_not_an_all_clear():
    res = dnm.check([], ssh_factory=factory(state=state()))
    assert res["ok"] is False
    assert "no cluster nodes resolved" in res["detail"]
    assert "cannot" in res["fix"]


def test_check_reads_the_opt_out_from_config(tmp_path):
    res = dnm.check([NODE], ssh_factory=factory(state=state()),
                    hscc_dir=cfg_dir(tmp_path, {nm.MASK_SUBKEY: False}))
    assert res["ok"] is True
    assert res["rows"][0]["verdict"] == dnm.V_OPTED_OUT
    assert "opted out" in res["detail"]
    assert "--" in res["detail"], "opt-out renders as --, never as ok"


def test_config_error_is_surfaced_and_flips_ok(tmp_path):
    bad = tmp_path / ".hscc"
    bad.mkdir(parents=True)
    (bad / "cluster.json").write_text("{not json")
    res = dnm.check([NODE], ssh_factory=factory(state=state()),
                    hscc_dir=str(bad))
    assert res["ok"] is False
    assert "CONFIG" in res["detail"]


def test_non_boolean_opt_out_stays_on_and_is_reported(tmp_path):
    """A typo'd opt-out must not silently disable the layer — and must not be
    silently ignored either."""
    res = dnm.check([NODE], ssh_factory=factory(state=state()),
                    hscc_dir=cfg_dir(tmp_path, {nm.MASK_SUBKEY: "false"}))
    assert res["ok"] is False
    assert nm.MASK_SUBKEY in res["detail"]
    assert res["rows"][0]["verdict"] == dnm.V_OK, "kept ON, so the mask is still checked"


def test_detail_is_plaintext_no_ansi():
    res = dnm.check([NODE],
                    ssh_factory=factory(state=state(service="static",
                                                    timer="enabled")))
    assert "\x1b" not in res["detail"]


def test_fix_text_names_the_command_and_denies_the_scary_consequence():
    res = dnm.check([NODE], ssh_factory=factory(state=state(service="static",
                                                            timer="enabled")))
    assert "install_notifier_mask.py" in res["fix"]
    assert "fwupd" in res["fix"], "the fix text must say no firmware update is lost"


# ── read-only by construction ───────────────────────────────────────────────

def test_check_sends_only_the_state_mode():
    """The check's own module must contain no systemctl text at all: every
    systemctl string lives in the installer script, which is MODE-gated. That
    is what makes it read-only *by construction* rather than by care."""
    t = transport(state=state())
    dnm.layer(NODE, ssh_cmd=t, enabled=True)
    assert [c["kind"] for c in t.calls] == ["state"], \
        "the check must issue exactly one remote command"
    script = _decode(t.calls[0]["command"])
    assert 'MODE="state"' in script
    # The check never composes a systemctl call of its own; it delegates the
    # whole decision to the installer's script in state mode. Asserted over AST
    # *string constants*, not raw source: the word "systemctl" legitimately
    # appears in this module's docstrings explaining the bus pitfall, and an
    # `in src` test would only teach the next reader to delete the comment.
    # (The bash-backed tests in test_install_notifier_mask.py are what prove the
    # mask/stop arms of the shared script do not execute in state mode; a
    # textual assertion here could not.)
    assert not _string_literals(dnm, "systemctl --user"), \
        "the doctor layer must not hand-roll systemctl commands"
    assert not _string_literals(dnm, "sudo"), \
        "the doctor layer must not be able to escalate"


def test_default_transport_is_refused_inside_pytest():
    """A suite that forgets to inject its transport must fail loudly rather
    than ssh the live fleet."""
    assert "pytest" in sys.modules, "this file is the proof; run it under pytest"
    fac = dnm._default_ssh_factory()
    res = fac(NODE)("anything", 5)
    assert res["ok"] is False
    assert "refused inside a pytest process" in res["stderr"]


def test_topology_probe_cannot_reach_the_live_fleet_from_the_suite(monkeypatch):
    """A bare check() with no hosts must resolve [] under pytest, so it can
    never shell out to the operator's real sparkrun config."""
    called = []
    monkeypatch.setattr(dnm, "_topology_hosts", lambda: called.append("x") or [])
    res = dnm.check(ssh_factory=factory(state=state()))
    assert res["ok"] is False and "no cluster nodes resolved" in res["detail"]


# ── the installer is the single source of truth ─────────────────────────────

def test_verdicts_come_from_the_installers_own_vocabulary():
    """Nothing here re-declares 'masked': the check must consume the
    installer's MASKED_OUTCOMES/is_masked_state, or the two can drift."""
    import inspect
    src = inspect.getsource(dnm.layer)
    assert "MASKED_OUTCOMES" in src
    assert "probe_node" in src
    assert "nm.MASK_SUBKEY" in src
    # and the check must not re-type the unit name as a literal of its own:
    # an AST walk over string constants, because the name legitimately appears
    # in this module's prose and an `in src` test would only teach the next
    # reader to delete the comment.
    assert not _string_literals(dnm, SVC) and not _string_literals(dnm, TIMER), \
        "unit names must come from the installer module, never be re-typed here"


def test_check_name_is_stable_for_the_json_contract():
    assert dnm.CHECK_NAME == "snap firmware-notifier mask (node-recovery)"


def test_no_real_addresses_in_the_doctor_module():
    import re
    src = open(os.path.join(os.path.dirname(dnm.__file__),
                            "doctor_notifier_mask.py")).read()
    bad = re.compile(r"\b(?:192\.168\.\d+\.\d+|100\.6[5-9]\.\d+\.\d+"
                     r"|100\.(?:7\d|8\d|9\d|1[01]\d|12[0-7])\.\d+\.\d+)\b")
    assert not bad.search(src), bad.findall(src)


# ── wired into run_doctor (the shared registry) ─────────────────────────────

class TestWiredIntoDoctor:
    """The check must appear in doctor's own registry and in `doctor --json`,
    and a skipped fleet-check pass must say 'skipped' rather than vanish."""

    @pytest.fixture()
    def doctor(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "hermes-agent").mkdir()
        import yaml
        (hermes / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(hermes))
        monkeypatch.setenv("HSCC_DIR", str(home / ".hscc"))
        (home / ".hscc").mkdir()
        import doctor
        monkeypatch.setattr(doctor, "_run_cluster_list", lambda: (
            '[{"name": "t", "default": true, "hosts": ["10.0.0.1", "10.0.0.2"],'
            ' "cache_dir": ""}]'))
        # Hermetic: the sibling `sparkrun cluster vs serving` check must not
        # reach a real sparkrun either. With HOME redirected it would otherwise
        # read the operator's live cluster and its JSON handling would decide
        # whether this suite passes — a test must not depend on that. "" is the
        # documented "no default cluster" answer (see the sibling-crash note on
        # the card: "null" and "[]" both crash that check on main).
        monkeypatch.setattr(doctor, "_run_cluster_default", lambda: "")
        monkeypatch.setattr(doctor.shutil, "which",
                            lambda n: "/usr/bin/sparkrun" if n == "sparkrun" else None)
        monkeypatch.setattr(doctor, "_gateway_running",
                            lambda: doctor.Check("gateway", True, detail="running",
                                                 fix="", fatal=False))
        return doctor

    def test_fleet_checks_off_reports_skipped(self, doctor):
        res = doctor.run_doctor(fleet_checks=False)
        names = {c["name"]: c for c in res["checks"]}
        assert dnm.CHECK_NAME in names, "the check must be registered"
        assert "skipped" in names[dnm.CHECK_NAME]["detail"]
        assert names[dnm.CHECK_NAME]["fatal"] is False
        assert res["ok"] is True

    def test_fleet_checks_on_reports_per_node_verdicts(self, doctor, monkeypatch):
        monkeypatch.setattr(dnm, "_default_ssh_factory",
                            provider(state=state()))
        res = doctor.run_doctor(fleet_checks=True)
        row = next(c for c in res["checks"] if c["name"] == dnm.CHECK_NAME)
        assert row["ok"] is True
        assert "2/2 node(s)" in row["detail"]
        assert set(row) == {"name", "ok", "detail", "fix", "fatal"}, \
            "doctor --json must keep one five-field shape per check"

    def test_json_shape_is_five_fields_and_parses(self, doctor, monkeypatch):
        monkeypatch.setattr(dnm, "_default_ssh_factory",
                            provider(state=state(service="static", timer="enabled")))
        res = doctor.run_doctor(fleet_checks=True)
        payload = json.dumps(res)  # must be serialisable
        assert json.loads(payload)
        row = next(c for c in res["checks"] if c["name"] == dnm.CHECK_NAME)
        assert row["ok"] is False
        assert row["fatal"] is False

    def test_a_red_notifier_never_flips_the_fatal_gate(self, doctor, monkeypatch):
        monkeypatch.setattr(dnm, "_default_ssh_factory",
                            provider(state=state(service="static", timer="enabled")))
        res = doctor.run_doctor(fleet_checks=True)
        names = {c["name"] for c in res["checks"]}
        assert dnm.CHECK_NAME in names, "the check must be present when fleet checks run"
        assert dnm.CHECK_NAME not in res["fatal_failures"], \
            "a red notifier is drift to fix, never a fatal that fails `doctor`"
        row = next(c for c in res["checks"] if c["name"] == dnm.CHECK_NAME)
        assert row["ok"] is False, "and it must actually be red here"
        assert row["fatal"] is False

    def test_missing_installer_is_reported_not_silently_absent(self, doctor,
                                                               monkeypatch):
        real = dnm._installer
        dnm._installer = lambda: None
        try:
            res = doctor.run_doctor(fleet_checks=True)
            row = next(c for c in res["checks"] if c["name"] == dnm.CHECK_NAME)
            assert row["ok"] is False
            assert row["fatal"] is False
        finally:
            dnm._installer = real
