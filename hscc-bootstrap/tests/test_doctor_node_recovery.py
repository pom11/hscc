"""The node-recovery doctor check (card t_94598b3a).

Fabricated per-node fixtures only: a scripted ``ssh_cmd`` answers the two probe
commands (the watchdog ``state`` script and ``install_autoreboot.probe_command``)
and records everything it was asked. Nothing here opens a socket, resolves a
host, or touches /etc — the addresses are the documented 10.0.0.x placeholders,
and the write-never-read rule is pinned by asserting on the recorded commands.

The honest-signal rule (fleet rule: an environment fault must never present as
a data fact) is the centre of gravity of this file: every unreachable/refused/
unreadable shape must land on ``error`` or ``fail``, never on ``ok``.
"""

import json
import os
import sys

import pytest

import doctor
import doctor_node_recovery as dnr

NODE = "10.0.0.1"
OTHER = "10.0.0.2"


# ── fabricated node probes ──────────────────────────────────────────────────

def state_lines(*, present=True, usec="30s", watchdog_class="present",
                device_state="armed", device_timeout="30", wdctl="present",
                wdctl_timeout="30seconds", fuser="unavailable",
                identity="SBSA Generic Watchdog"):
    """The watchdog ``state`` script's stdout, as a node would print it.

    Field names mirror what the real GB10 probe emits (measured by the parent
    card: identity "SBSA Generic Watchdog", sysfs state "inactive" while
    unarmed, RuntimeWatchdogUSec "0" while unarmed).
    """
    out = ["HSCC_STATE_BEGIN: 1",
           "HSCC_PATH: /etc/systemd/system.conf.d/90-hscc-watchdog.conf",
           f"HSCC_PRESENT: {'true' if present else 'false'}",
           "HSCC_SHA256: deadbeef",
           f"HSCC_RUNTIME_WATCHDOG_USEC: {usec}",
           "HSCC_REBOOT_WATCHDOG_USEC: 2min",
           f"HSCC_WATCHDOG_CLASS: {watchdog_class}",
           "HSCC_WATCHDOG_DEVICES: watchdog0",
           f"HSCC_WATCHDOG0_IDENTITY: {identity}",
           f"HSCC_WATCHDOG0_STATE: {device_state}",
           f"HSCC_WATCHDOG0_TIMEOUT: {device_timeout}",
           "HSCC_WDCTL: " + wdctl,
           f"HSCC_WDCTL_TIMEOUT: {wdctl_timeout}",
           f"HSCC_FUSER: {fuser}",
           "HSCC_STATE_END: 1",
           "HSCC_RC: 0"]
    if wdctl == "absent":
        out = [ln for ln in out if not ln.startswith("HSCC_WDCTL_TIMEOUT")]
    return "\n".join(out) + "\n"


def sysctl_lines(knobs, file_content, *, omit_val_for=()):
    """``install_autoreboot.probe_command()`` stdout for a fabricated node.

    ``knobs`` maps key -> live value (every entry gets a KNOB line, and a VAL
    line unless the key is in ``omit_val_for`` — that models the kernel naming
    a knob whose value the node could not read). Keys absent from the dict are
    knobs this kernel does not expose.
    """
    lines = []
    for key, value in knobs.items():
        lines.append(f"KNOB {key}")
        if key not in omit_val_for:
            lines.append(f"VAL {key} {value}")
    lines.append("---FILE---")
    if file_content:
        lines.append(file_content.rstrip("\n"))
    return "\n".join(lines) + "\n"


HEALTHY_KNOBS = {"kernel.panic": "10", "kernel.panic_on_oops": "1",
                 "kernel.softlockup_panic": "1", "kernel.hung_task_panic": "1",
                 "kernel.hung_task_timeout_secs": "600",
                 "kernel.hardlockup_panic": "1"}


def healthy_file():
    """The canonical drop-in for a node that exposes every knob, incl. hardlockup."""
    import install_autoreboot as ar
    return ar.build_dropin(True)


class FakeNode:
    """Answers the two probe commands; records every command it was given.

    ``state``/``sysctl`` are either the stdout a node prints (a str) or a whole
    transport response (a dict) — the dict form is how a test fabricates a
    half-run probe, a refused sudo, or an unreachable node.
    """

    def __init__(self, *, state=None, sysctl=None, state_fail=None,
                 sysctl_fail=None):
        self.state = state if state is not None else state_lines()
        self.sysctl = sysctl if sysctl is not None else sysctl_lines(
            HEALTHY_KNOBS, healthy_file())
        self.state_fail = state_fail
        self.sysctl_fail = sysctl_fail
        self.commands = []

    def _respond(self, payload, fail):
        if fail is not None:
            return fail
        if isinstance(payload, dict):
            return payload
        return {"ok": True, "stdout": payload, "stderr": "", "code": 0}

    def __call__(self, command, timeout):
        self.commands.append({"command": command, "timeout": timeout})
        if "HSCC_STATE_BEGIN" in command or 'MODE="state"' in command:
            return self._respond(self.state, self.state_fail)
        if "---FILE---" in command or "kernel.panic" in command:
            return self._respond(self.sysctl, self.sysctl_fail)
        raise AssertionError(f"doctor sent an unexpected command: {command[:120]}")


def one(node):
    """A factory that answers every host with this one fabricated node."""
    return lambda host: node.__call__


def two(a, b):
    nodes = [a, b]

    def make(host):
        idx = 0 if host == NODE else 1
        return nodes[idx].__call__
    return make


# ── the happy path ──────────────────────────────────────────────────────────

def test_healthy_node_is_ok_on_both_layers():
    node = FakeNode()
    res = dnr.check([NODE], ssh_factory=one(node),
                    hscc_dir="/nonexistent-so-defaults-on")
    assert res["ok"] is True, res["detail"]
    row = res["rows"][0]
    assert row["verdict"] == dnr.V_OK
    assert row["watchdog"]["verdict"] == dnr.V_OK
    assert row["sysctl"]["verdict"] == dnr.V_OK
    assert "1/1 node(s) fully protected" in res["detail"]


def test_doctor_sends_only_the_two_probe_commands_and_never_writes():
    """Read-only contract: the bytes sent ARE the parents' own probe commands.

    Byte-identity beats a token blacklist here. The watchdog probe is the
    parent's ``state``-mode script — which legitimately contains the words
    ``sudo tee`` and ``daemon-reexec`` inside its install branches and refusal
    messages — so substring bans would be noise. What matters is that doctor
    sends exactly ``install_script("state")`` (the parent's read-only mode) and
    ``install_autoreboot.probe_command()``, and nothing else.
    """
    import install_autoreboot as ar
    import node_recovery as nr

    node = FakeNode()
    dnr.check([NODE], ssh_factory=one(node), hscc_dir="/nonexistent")
    cmds = [c["command"] for c in node.commands]
    assert len(cmds) == 2, [c[:80] for c in cmds]
    # The watchdog leg is the parent's state-mode script (env prefix included,
    # exactly as `_remote_state` sends it).
    assert cmds[0] == nr.wanted_env_prefix() + nr.install_script("state")
    # The sysctl leg is the parent's probe command, byte for byte.
    assert cmds[1] == ar.probe_command()
    # And the state script is the parent's READ-ONLY mode: no write branch runs
    # in it (the MODE variable is what gates them).
    assert 'MODE="state"' in cmds[0]


def test_every_node_of_the_fleet_gets_its_own_row():
    a, b = FakeNode(), FakeNode(state=state_lines(usec="0", device_state="inactive"))
    res = dnr.check([NODE, OTHER], ssh_factory=two(a, b), hscc_dir="/nonexistent")
    assert [r["node"] for r in res["rows"]] == [NODE, OTHER]
    assert res["ok"] is False
    assert "1/2 node(s) fully protected" in res["detail"]


# ── the watchdog layer ──────────────────────────────────────────────────────

def watchdog_verdict(**state_kw):
    node = FakeNode(state=state_lines(**state_kw))
    layer = dnr.watchdog_layer(NODE, ssh_cmd=node, enabled=True)
    return layer


def test_watchdog_unarmed_is_red_and_names_the_target():
    layer = watchdog_verdict(usec="0", device_state="inactive")
    assert layer["verdict"] == dnr.V_FAIL
    problem = layer["problems"][0]
    assert NODE in problem and "RuntimeWatchdogSec=30s" in problem
    assert "0" in problem


def test_watchdog_foreign_holder_is_red_with_the_holder():
    layer = watchdog_verdict(usec="0", device_state="active", fuser="1234")
    assert layer["verdict"] == dnr.V_FAIL
    assert "1234" in layer["problems"][0]


def test_watchdog_configured_but_nothing_petting_is_not_ok():
    """The parent's own precedent: adopted config + sysfs inactive is a fault.

    Asserted as *not ok* rather than as one specific verdict: the parent maps
    this to ``error`` (it cannot tell whether a hang would reset the node), and
    either fault rendering is acceptable — a green one is not.
    """
    layer = watchdog_verdict(usec="30s", device_state="inactive")
    assert layer["verdict"] != dnr.V_OK
    assert layer["problems"], "a false green needs a stated reason"


def test_watchdog_no_watchdog_core_is_red_not_silent():
    layer = watchdog_verdict(watchdog_class="missing", usec="0",
                             device_state="", device_timeout="")
    assert layer["verdict"] in (dnr.V_FAIL, dnr.V_ERROR)
    assert "no watchdog core" in layer["problems"][0]


def test_watchdog_missing_wdctl_timeout_is_not_green():
    """A device with no readable timeout anywhere cannot be protection."""
    node = FakeNode(state=state_lines(usec="30s", device_state="armed",
                                      wdctl="present", wdctl_timeout=""))
    layer = dnr.watchdog_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "no Timeout" in layer["problems"][0]


def test_watchdog_transport_failure_is_error_never_ok():
    node = FakeNode(state_fail={"ok": False, "stdout": "", "code": 255,
                                "stderr": "ssh: connect to host timed out"})
    layer = dnr.watchdog_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "NOT verified" in layer["problems"][0]


def test_watchdog_sudo_refusal_is_error():
    """The parent reports RC_NEED_ROOT as an ERROR; the doctor keeps it a fault."""
    node = FakeNode(state={"ok": True, "stdout": "HSCC_CODE: 4\n"
                            "HSCC_MSG: sudo -n refused on this node\nHSCC_RC: 4\n",
                            "stderr": "", "code": 4})
    layer = dnr.watchdog_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "sudo -n refused" in layer["problems"][0]


def test_watchdog_probe_without_rc_is_error():
    """A half-run probe proves nothing — never read it as 'not armed, fine'."""
    node = FakeNode(state={"ok": True, "stdout": "HSCC_STATE_BEGIN: 1\n",
                           "stderr": "", "code": 0})
    layer = dnr.watchdog_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR


def test_data_fact_and_env_fault_never_share_a_rendering():
    """The card's core rule, pinned at the wording level.

    "read a state that is not armed" is a DATA FACT (fail / NOT ARMED);
    "the probe never returned state" is an ENVIRONMENT FAULT (error /
    UNVERIFIABLE ... NOT verified). If those two ever print the same way, the
    operator cannot tell a broken node from an unreadable one.
    """
    fact = watchdog_verdict(usec="0", device_state="inactive")
    fault = dnr.watchdog_layer(
        NODE, ssh_cmd=FakeNode(state_fail={"ok": False, "stdout": "", "code": 255,
                                            "stderr": "ssh: connection refused"}),
        enabled=True)
    assert (fact["verdict"], fault["verdict"]) == (dnr.V_FAIL, dnr.V_ERROR)
    assert "NOT ARMED" in fact["problems"][0]
    assert "UNVERIFIABLE" not in fact["problems"][0]
    assert "UNVERIFIABLE" in fault["problems"][0]
    assert "NOT ARMED" not in fault["problems"][0]


# ── the sysctl layer ────────────────────────────────────────────────────────

def sysctl_verdict(knobs, file_content, *, omit_val_for=(), enabled=True):
    node = FakeNode(sysctl=sysctl_lines(knobs, file_content,
                                        omit_val_for=omit_val_for))
    return dnr.sysctl_layer(NODE, ssh_cmd=node, enabled=enabled)


def test_sysctl_in_sync_is_ok():
    layer = sysctl_verdict(HEALTHY_KNOBS, healthy_file())
    assert layer["verdict"] == dnr.V_OK


def test_sysctl_wrong_value_names_node_key_actual_and_expected():
    knobs = dict(HEALTHY_KNOBS, **{"kernel.softlockup_panic": "0"})
    layer = sysctl_verdict(knobs, healthy_file())
    assert layer["verdict"] == dnr.V_FAIL
    line = [p for p in layer["problems"] if "softlockup" in p][0]
    assert line.startswith(NODE + "/kernel.softlockup_panic")
    assert "live '0'" in line and "expected '1'" in line


def test_sysctl_missing_dropin_file_is_red():
    layer = sysctl_verdict(HEALTHY_KNOBS, None)
    assert layer["verdict"] == dnr.V_FAIL
    assert any("absent" in p for p in layer["problems"])


def test_sysctl_knob_absent_but_file_declares_it_is_red():
    """hardlockup_panic in the file, knob gone from the kernel: not green.

    The installer calls this an upgradeable stale key set; until it re-runs the
    node is NOT enforcing what the operator thinks they configured, so the
    doctor keeps printing it.
    """
    knobs = {k: v for k, v in HEALTHY_KNOBS.items() if k != "kernel.hardlockup_panic"}
    layer = sysctl_verdict(knobs, healthy_file())   # file HAS hardlockup
    assert layer["verdict"] == dnr.V_FAIL
    assert any("stale managed key set" in p for p in layer["problems"])


def test_sysctl_knob_present_and_wrong_is_red():
    knobs = dict(HEALTHY_KNOBS, **{"kernel.hardlockup_panic": "0"})
    layer = sysctl_verdict(knobs, healthy_file())
    assert layer["verdict"] == dnr.V_FAIL
    assert any("kernel.hardlockup_panic" in p and "live '0'" in p
               for p in layer["problems"])


def test_sysctl_hardlockup_absent_everywhere_is_ok_with_a_note():
    """The parent's conditional rule: knob not exposed => skipped, node not failed.

    The note stays visible so an operator can see the layer is not enforced for
    that knob, rather than the row silently reading as a full pass.
    """
    knobs = {k: v for k, v in HEALTHY_KNOBS.items() if k != "kernel.hardlockup_panic"}
    import install_autoreboot as ar
    layer = sysctl_verdict(knobs, ar.build_dropin(False))
    assert layer["verdict"] == dnr.V_OK
    assert any("hardlockup" in n and "not enforced" in n for n in layer["notes"])


def test_sysctl_hung_task_timeout_drift_is_red_at_the_parents_value():
    """600 is the parent's decision; a node reading the 120s default is drift."""
    knobs = dict(HEALTHY_KNOBS, **{"kernel.hung_task_timeout_secs": "120"})
    layer = sysctl_verdict(knobs, healthy_file())
    assert layer["verdict"] == dnr.V_FAIL
    assert any("hung_task_timeout_secs" in p and "'120'" in p and "'600'" in p
               for p in layer["problems"])


def test_sysctl_unreadable_value_is_a_fault_not_a_match():
    layer = sysctl_verdict(HEALTHY_KNOBS, healthy_file(),
                           omit_val_for=("kernel.panic",))
    assert layer["verdict"] == dnr.V_FAIL
    assert any("kernel.panic" in p and "unreadable" in p for p in layer["problems"])


def test_sysctl_operator_edited_file_is_red_and_names_drift():
    edited = healthy_file().replace("kernel.panic = 10", "kernel.panic = 0")
    layer = sysctl_verdict(HEALTHY_KNOBS, edited)
    assert layer["verdict"] == dnr.V_FAIL
    assert any("operator edit" in p for p in layer["problems"])


def test_sysctl_probe_failure_is_error_not_ok():
    node = FakeNode(sysctl_fail={"ok": False, "stdout": "", "code": 255,
                                 "stderr": "ssh: Permission denied (publickey)"})
    layer = dnr.sysctl_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "NOT verified" in layer["problems"][0]


def test_sysctl_zero_knobs_is_error_not_vacuous_ok():
    """/proc/sys unreadable prints nothing: that is a fault, not 'nothing to check'."""
    layer = sysctl_verdict({}, healthy_file())
    assert layer["verdict"] == dnr.V_ERROR
    assert "/proc/sys unreadable" in layer["problems"][0]


def test_sysctl_unreachable_node_is_error_never_ok():
    node = FakeNode(sysctl_fail={"ok": False, "stdout": "", "code": None,
                                 "stderr": "no route to host"})
    layer = dnr.sysctl_layer(NODE, ssh_cmd=node, enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "no route to host" in layer["problems"][0]


# ── opt-outs (a deferred layer must not read as green-but-unenforced) ───────

def write_cluster_json(tmp_path, block):
    d = tmp_path / "hscc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cluster.json").write_text(json.dumps({"node_recovery": block}))
    return str(d)


def test_watchdog_opt_out_is_reported_not_green(tmp_path):
    hscc = write_cluster_json(tmp_path, {"watchdog": False})
    res = dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir=hscc)
    row = res["rows"][0]
    assert row["watchdog"]["verdict"] == dnr.V_OPTED_OUT
    assert "NOT armed" in row["watchdog"]["notes"][0]
    assert "opted out" in res["detail"]
    # The sysctl layer is subkey-independent: it must still be verified.
    assert row["sysctl"]["verdict"] == dnr.V_OK


def test_autoreboot_opt_out_is_reported_not_green(tmp_path):
    hscc = write_cluster_json(tmp_path, {"autoreboot": False})
    res = dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir=hscc)
    row = res["rows"][0]
    assert row["sysctl"]["verdict"] == dnr.V_OPTED_OUT
    assert "nothing enforces" in row["sysctl"]["notes"][0]
    assert row["watchdog"]["verdict"] == dnr.V_OK


def test_non_boolean_opt_out_is_a_config_fault(tmp_path):
    """A typo cannot silently disable a recovery layer and still read as green."""
    hscc = write_cluster_json(tmp_path, {"watchdog": "false"})
    res = dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir=hscc)
    assert res["ok"] is False
    assert "CONFIG" in res["detail"]


def test_opt_out_never_renders_as_ok_mark(tmp_path):
    hscc = write_cluster_json(tmp_path, {"watchdog": False, "autoreboot": False})
    res = dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir=hscc)
    assert "[ok]" not in res["detail"], res["detail"]
    assert "opted_out" in res["detail"]


# ── the check as wired into doctor ──────────────────────────────────────────

FAKE_CLUSTER = ('[{"name": "test-cluster", "default": true, '
                '"hosts": ["10.0.0.1", "10.0.0.2"], "cache_dir": ""}]')


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["10.0.0.1", "10.0.0.2"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def hermetic_home(tmp_path, monkeypatch):
    """Private HOME/HERMES_HOME tree, same convention as test_doctor.py.

    Doctor resolves homes from the ambient env, and a dispatched worker carries
    HERMES_HOME=<root>/profiles/<name>; without this every CLI/summary test
    below would read the operator's live home. A plain helper (not a fixture)
    because each test also needs its own tmp_path-driven stubs.
    """
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    return home


def test_run_doctor_reports_the_check_when_fleet_checks_are_on(tmp_path, monkeypatch):
    home = hermetic_home(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER,
                            fleet_checks=True, _ssh_factory=one(FakeNode()),
                            hscc_dir="/nonexistent", **_fleet_health())
    names = {c["name"]: c for c in res["checks"]}
    assert dnr.CHECK_NAME in names
    assert names[dnr.CHECK_NAME]["ok"] is True
    assert names[dnr.CHECK_NAME]["fatal"] is False


def test_run_doctor_json_includes_the_check(tmp_path, monkeypatch):
    home = hermetic_home(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER,
                            fleet_checks=True, _ssh_factory=one(FakeNode()),
                            hscc_dir="/nonexistent", **_fleet_health())
    payload = json.loads(json.dumps(res))     # must be JSON-serialisable
    assert any(c["name"] == dnr.CHECK_NAME for c in payload["checks"])
    assert "\n" in [c for c in payload["checks"]
                    if c["name"] == dnr.CHECK_NAME][0]["detail"]


def test_run_doctor_fleet_off_marks_the_check_skipped_not_ok(tmp_path, monkeypatch):
    """A skipped check must say skipped; absence would read as an all-clear."""
    home = hermetic_home(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER)
    names = {c["name"]: c for c in res["checks"]}
    assert names[dnr.CHECK_NAME]["detail"] == "skipped (fleet checks off)"


def test_run_doctor_inside_pytest_never_gets_a_real_transport(tmp_path, monkeypatch):
    """fleet_checks=True without an injected ssh_factory must not ssh anywhere."""
    home = hermetic_home(tmp_path, monkeypatch)
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER,
                            fleet_checks=True, hscc_dir="/nonexistent",
                            **_fleet_health())
    names = {c["name"]: c for c in res["checks"]}
    c = names[dnr.CHECK_NAME]
    assert c["ok"] is False
    assert "NOT verified" in c["detail"]
    assert "pytest" in c["detail"] or "refused" in c["detail"]


def test_run_doctor_red_node_is_not_fatal(tmp_path, monkeypatch):
    """A node without a watchdog must not hard-stop an install (parents are
    non-fatal too) — but it must still be loud."""
    home = hermetic_home(tmp_path, monkeypatch)
    bad = FakeNode(state=state_lines(usec="0", device_state="inactive"))
    res = doctor.run_doctor(str(home), _cluster_runner=lambda: FAKE_CLUSTER,
                            fleet_checks=True, _ssh_factory=one(bad),
                            hscc_dir="/nonexistent", **_fleet_health())
    assert res["ok"] is True                      # nothing fatal failed
    names = {c["name"]: c for c in res["checks"]}
    assert names[dnr.CHECK_NAME]["ok"] is False
    assert "NOT protected" in names[dnr.CHECK_NAME]["detail"]


def test_no_hosts_resolved_is_an_error_not_an_all_clear():
    res = dnr.check([], ssh_factory=one(FakeNode()), hscc_dir="/nonexistent")
    assert res["ok"] is False
    assert "cannot verify node recovery" in res["detail"]


def test_node_hosts_reads_the_default_cluster():
    assert doctor._node_hosts(lambda: FAKE_CLUSTER) == [NODE, OTHER]
    assert doctor._node_hosts(lambda: "") == []
    assert doctor._node_hosts(lambda: "not json") == []


def test_sample_render_green_and_red():
    """The card asks for one sample per-node red/green render, verbatim."""
    good = FakeNode()
    bad = FakeNode(state=state_lines(usec="0", device_state="inactive"),
                   sysctl=sysctl_lines(dict(HEALTHY_KNOBS, **{"kernel.panic": "0"}),
                                       healthy_file()))
    res = dnr.check([NODE, OTHER], ssh_factory=two(good, bad),
                    hscc_dir="/nonexistent")
    print(res["detail"])
    lines = res["detail"].splitlines()
    assert any(f"[ok] {NODE}" in ln for ln in lines)
    assert any(f"[XX] {OTHER}" in ln and "kernel.panic" in ln for ln in lines)
    assert any("watchdog" in ln for ln in lines if f"[XX] {OTHER}" in ln)


# ── layout + transport guards (the silent-failure classes) ──────────────────

def test_cluster_dir_resolves_the_sibling_plugin_in_both_layouts():
    """A wrong sibling path would make the watchdog layer silently unavailability-error.

    True in the work repo (hscc-bootstrap beside hscc-cluster) and in the
    deployed ~/.hermes/plugins layout — the same assumption
    install_autoreboot._default_ssh already makes for clusterlib.
    """
    d = dnr._cluster_dir()
    assert os.path.isdir(d), d
    assert os.path.isfile(os.path.join(d, "node_recovery.py")), d
    assert os.path.isfile(os.path.join(d, "clusterlib.py")), d
    assert dnr._nr() is not None, "the watchdog module must resolve from bootstrap"


def test_sibling_path_is_appended_so_bootstrap_theme_still_wins():
    """hscc-bootstrap and hscc-cluster BOTH ship a `_theme.py`.

    The sibling dir is appended to sys.path precisely so importing the cluster
    modules can never hijack a same-named bootstrap module. Pin the outcome:
    `import _theme` must still be THIS plugin's file.
    """
    import _theme
    dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir="/nonexistent")
    assert os.path.realpath(_theme.__file__) == os.path.realpath(
        os.path.join(os.path.dirname(dnr.__file__), "_theme.py")), _theme.__file__


def test_default_transport_refuses_inside_pytest():
    """The production transport must be unreachable from a test process."""
    run = dnr._default_ssh_factory()(NODE)
    res = run("echo hi", 5)
    assert res["ok"] is False and "pytest" in res["stderr"]


def test_watchdog_layer_reports_unavailable_module_as_error(monkeypatch):
    """A broken layout is an ERROR that names itself, never a silent skip."""
    monkeypatch.setattr(dnr, "_nr", lambda: None)
    layer = dnr.watchdog_layer(NODE, ssh_cmd=FakeNode(), enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "unavailable" in layer["problems"][0]


def test_sysctl_layer_reports_unavailable_module_as_error(monkeypatch):
    monkeypatch.setattr(dnr, "_ar", lambda: None)
    layer = dnr.sysctl_layer(NODE, ssh_cmd=FakeNode(), enabled=True)
    assert layer["verdict"] == dnr.V_ERROR
    assert "install_autoreboot" in layer["problems"][0]


# ── the standalone CLI (also the doctor --json contract holder) ─────────────

def test_cli_json_includes_every_node(tmp_path, capsys):
    rc = dnr.main(["--hosts", f"{NODE},{OTHER}", "--json"],
                  ssh_factory=one(FakeNode()))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [r["node"] for r in payload["rows"]] == [NODE, OTHER]
    assert payload["name"] == dnr.CHECK_NAME


def test_cli_exits_nonzero_when_a_node_is_not_protected(capsys):
    bad = FakeNode(sysctl=sysctl_lines(HEALTHY_KNOBS, None))
    rc = dnr.main(["--hosts", NODE, "--json"], ssh_factory=one(bad))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["ok"] is False


def test_doctor_json_output_carries_the_check(tmp_path, monkeypatch, capsys):
    """The existing `doctor --json` contract: every check is one dict of the
    same five fields, so a consumer can scan names without special-casing.

    rc is NOT asserted: in a throwaway home the preflight legitimately fails
    other fatal checks (no sparkrun cluster configured), and this test is about
    the node-recovery row's presence and shape, not the preflight verdict.
    """
    home = hermetic_home(tmp_path, monkeypatch)
    doctor.main(["--json", "--home", str(home)])
    res = json.loads(capsys.readouterr().out)
    match = [c for c in res["checks"] if c["name"] == dnr.CHECK_NAME]
    assert match, [c["name"] for c in res["checks"]]
    assert sorted(match[0]) == sorted(
        ["name", "ok", "detail", "fix", "fatal"]), "check shape drifted"
    # Fleet I/O is off inside pytest, so the row must SAY skipped, not look ok.
    assert "skipped" in match[0]["detail"]


def test_check_shape_has_no_extra_keys_for_the_doctor():
    """run_doctor copies only the five Check fields; prove the rest is opt-in."""
    res = dnr.check([NODE], ssh_factory=one(FakeNode()), hscc_dir="/nonexistent")
    assert set(res) == {"name", "ok", "fatal", "detail", "fix", "rows"}
