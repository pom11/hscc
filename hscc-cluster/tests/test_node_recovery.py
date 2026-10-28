"""Node-recovery watchdog install (card t_ebbe38d1).

Two layers on purpose:

  * fabricated-transport tests — inject a dict responder for ``ssh_cmd`` and
    assert the *decisions* (write / no-op / drift / opt-out / dry-run / refuse /
    unreachable) without a shell;
  * a bash-backed fake node (``FakeNode``) — actually runs the generated
    node-side script under a sandbox with shimmed ``sudo`` / ``systemctl`` /
    ``sha256sum``, so the install decisions are proven by bash, not by a Python
    re-implementation of it. The systemd emulation is the real contract: a
    drop-in with RuntimeWatchdogSec=30s only becomes RuntimeWatchdogUSec=30s
    after ``daemon-reexec``, so a bug where the script writes but never re-execs
    fails here rather than looking green.

No test here opens a socket, resolves a host, or touches /etc. Node addresses
are the documented 10.0.0.x placeholders.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List

import pytest

import node_recovery as nr

NODE = "10.0.0.1"
OTHER = "10.0.0.2"


# ── fabricated transport ────────────────────────────────────────────────────

def state_lines(*, present=False, sha="", usec="0", devices="watchdog0",
                fuser="", identity="SBSA Generic Watchdog", systemd="present",
                watchdog_class="present", device_state="", device_timeout="10",
                wdctl="present", wdctl_timeout="10seconds"):
    """A node's `state`-mode stdout as the node would print it.

    Field names/shape mirror what the real GB10 probe emits (measured:
    identity `SBSA Generic Watchdog`, state `inactive` while unarmed, no
    `name` attribute at all).
    """
    out = ["HSCC_STATE_BEGIN: 1",
           f"HSCC_PATH: {nr.DROPIN_PATH}",
           f"HSCC_PRESENT: {'true' if present else 'false'}",
           f"HSCC_SHA256: {sha}",
           f"HSCC_RUNTIME_WATCHDOG_USEC: {usec}",
           "HSCC_REBOOT_WATCHDOG_USEC: 2min",
           f"HSCC_WATCHDOG_CLASS: {watchdog_class}",
           f"HSCC_WATCHDOG_DEVICES: {devices}",
           f"HSCC_WATCHDOG0_IDENTITY: {identity}",
           f"HSCC_WATCHDOG0_STATE: {device_state}",
           f"HSCC_WATCHDOG0_TIMEOUT: {device_timeout}",
           f"HSCC_WATCHDOG0_MIN_TIMEOUT: 1",
           f"HSCC_WATCHDOG0_MAX_TIMEOUT: 0",
           f"HSCC_WDCTL: {wdctl}",
           f"HSCC_WDCTL_TIMEOUT: {wdctl_timeout}",
           f"HSCC_FUSER: {fuser or 'unavailable'}",
           "HSCC_STATE_END: 1",
           "HSCC_RC: 0"]
    if systemd == "missing":
        out = [ln for ln in out if not ln.startswith("HSCC_RUNTIME_WATCHDOG")]
        out.insert(4, "HSCC_SYSTEMD: missing")
        out.insert(5, "HSCC_RUNTIME_WATCHDOG_USEC:")
    return "\n".join(out) + "\n"


class FakeTransport:
    """Scripted node: answers each mode from a queue, records every command.

    ``modes`` maps a script mode ("state"/"install") to a list of responses;
    each response is a dict, or a callable taking the command for inspection.
    A response list is consumed front-to-back and the last entry repeats, so a
    test can say "state says X, install says Y, verify says Z".
    """

    def __init__(self, **modes):
        self.commands: list = []
        self.modes: Dict[str, List[Any]] = {k: list(v) for k, v in modes.items()}

    @staticmethod
    def _mode(command):
        for mode in ("state", "install", "uninstall"):
            if f'MODE="{mode}"' in command:
                return mode
        return "unknown"

    def __call__(self, command, timeout):
        mode = self._mode(command)
        self.commands.append({"mode": mode, "command": command, "timeout": timeout})
        queue = self.modes.get(mode)
        if not queue:
            return {"ok": True, "stdout": "HSCC_RC: 0\n", "stderr": "", "code": 0}
        resp: Any = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(resp):
            resp = resp(command)
        out: Dict[str, Any] = {"ok": True, "stdout": "", "stderr": "", "code": 0}
        out.update(resp)
        return out

    def calls(self, mode):
        return [c for c in self.commands if c["mode"] == mode]


def ok_state(*, device_state="armed", **kw):
    """A fabricated healthy node.

    ``device_state`` defaults to "armed" because the healthy fixture is a node
    whose core PID1 actually holds; tests about the config-only false green pass
    device_state="inactive" explicitly.
    """
    kw.setdefault("device_state", device_state)
    return {"ok": True, "stdout": state_lines(**kw), "stderr": "", "code": 0}


# ── the drop-in content itself ─────────────────────────────────────────────

def test_dropin_content_is_the_exact_manager_section():
    """The bytes we write are the contract with the doctor check — pinned."""
    assert nr.DROPIN_CONTENT == (
        "# Managed by HSCC (node-recovery watchdog; hscc bootstrap).\n"
        "# PID1 (systemd) opens and pets /dev/watchdog0 and closes it on clean\n"
        "# shutdown, so a hard hang resets the SoC instead of wedging until someone\n"
        "# power-cycles the box. To stop HSCC managing this, set\n"
        "#   node_recovery: {watchdog: false}\n"
        "# in ~/.hscc/cluster.json and re-run bootstrap, then remove this file.\n"
        "[Manager]\n"
        "RuntimeWatchdogSec=30s\n"
        "RebootWatchdogSec=2min\n")
    assert nr.RUNTIME_WATCHDOG_SEC == 30
    assert nr.REBOOT_WATCHDOG_SEC == "2min"
    assert nr.DROPIN_PATH == "/etc/systemd/system.conf.d/90-hscc-watchdog.conf"
    assert nr.DROPIN_PATH.endswith("90-hscc-watchdog.conf")


def test_expected_dropin_matches_content():
    assert nr.expected_dropin() == nr.DROPIN_CONTENT


def test_sha_fingerprint_matches_content():
    assert nr.dropin_sha256() == hashlib.sha256(nr.DROPIN_CONTENT.encode()).hexdigest()


# ── the one definition of "armed" (shared with the doctor card) ────────────

@pytest.mark.parametrize("value,expected", [
    ("30s", True),                      # what systemd prints today
    ("30000000", True),                 # microseconds form
    ("30000000us", True),
    ("0min 30s", True),
    ("30 s", True),
    ("0", False),                       # watchdog disabled
    ("", False),                        # unreadable -> NOT armed
    ("1min", False),                    # a divergent timeout is not our target
    ("2min", False),
    ("garbage", False),
    (None, False),
])
def test_is_armed_state(value, expected):
    assert nr.is_armed_state({"runtime_watchdog_usec": value}) is expected


def test_bash_armed_alternation_is_grouped_and_matches_python():
    """The node script's `grep -E` and Python's matcher must agree.

    Ungrouped alternation under ^...$ anchors only the first and last
    alternative — a silent false-positive that would let a node whose timeout is
    `1min` be called armed.
    """
    import re
    script = nr.install_script("install")
    needles = [f"^({nr._ARMED_ALT})$" for _ in [0]]
    assert needles[0].replace("(", "(", 1) in script or "(%s)" % nr._ARMED_ALT in script
    grep_re = re.compile(r"^(" + nr._ARMED_ALT + r")$")
    for value in ("30s", "30000000", "30000000us", "0min 30s", "30 s"):
        assert grep_re.match(value), value
        assert nr._ARMED_RE.match(value), value
    for value in ("1min", "30sx", "x30s", "0", ""):
        assert not grep_re.match(value), value
        assert not nr._ARMED_RE.match(value), value


# ── opt-out config ──────────────────────────────────────────────────────────

def _cfg(tmp_path, block):
    p = tmp_path / "cluster.json"
    data = {"name": "fabricated", "gateway": {"ip": NODE}}
    if block is not None:
        data[nr.NODE_RECOVERY_KEY] = block
    p.write_text(json.dumps(data))
    return str(p)


def test_watchdog_defaults_on_when_block_absent(tmp_path):
    cfg = nr.node_recovery_config(_cfg(tmp_path, None))
    assert cfg["watchdog"] is True and cfg["autoreboot"] is True
    assert cfg["config_error"] == ""


def test_watchdog_defaults_on_when_file_absent(tmp_path):
    assert nr.node_recovery_config(str(tmp_path / "nope.json"))["watchdog"] is True


def test_opt_out_false_is_honoured(tmp_path):
    assert nr.watchdog_enabled(_cfg(tmp_path, {"watchdog": False})) is False


def test_opt_out_does_not_touch_the_sibling_subkey(tmp_path):
    """autoreboot=false (the sysctl card's knob) must not disarm the watchdog."""
    cfg = nr.node_recovery_config(_cfg(tmp_path, {"autoreboot": False}))
    assert cfg["autoreboot"] is False and cfg["watchdog"] is True


def test_watchdog_false_does_not_touch_autoreboot(tmp_path):
    cfg = nr.node_recovery_config(_cfg(tmp_path, {"watchdog": False}))
    assert cfg["watchdog"] is False and cfg["autoreboot"] is True


def test_unknown_subkey_is_ignored_not_fatal(tmp_path):
    """Older consumers must not break on a key they do not know (forward-compat)."""
    cfg = nr.node_recovery_config(_cfg(tmp_path, {"watchdog": True, "future_knob": 7}))
    assert cfg["watchdog"] is True and cfg["config_error"] == ""


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, "", [], {}])
def test_malformed_subkey_stays_on_and_reports(tmp_path, bad):
    """A typo'd opt-out must not silently flip the feature either way."""
    cfg = nr.node_recovery_config(_cfg(tmp_path, {"watchdog": bad}))
    assert cfg["watchdog"] is True
    assert nr.WATCHDOG_SUBKEY in cfg["config_error"] and "not true/false" in cfg["config_error"]


def test_non_mapping_block_reports_and_defaults(tmp_path):
    cfg = nr.node_recovery_config(_cfg(tmp_path, "yes"))
    assert cfg["watchdog"] is True and "must be a mapping" in cfg["config_error"]


def test_corrupt_config_is_an_error_not_a_silent_default(tmp_path):
    p = tmp_path / "cluster.json"
    p.write_text("{not json")
    cfg = nr.node_recovery_config(str(p))
    assert cfg["watchdog"] is True            # fails toward the safe default...
    assert "could not read" in cfg["config_error"]   # ...and says so


def test_config_error_surfaces_in_the_fleet_summary(tmp_path):
    tp = FakeTransport(state=[ok_state()], install=[{"ok": True, "stdout": "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    summary = nr.arm_fleet([NODE], ssh_factory=lambda h: tp,
                           config_path=_cfg(tmp_path, "nope"))
    assert summary["config_error"]


def test_hscc_dir_honours_env_not_a_baked_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HSCC_DIR", str(tmp_path))
    assert nr.hscc_dir() == str(tmp_path)
    assert nr.cluster_config_path() == os.path.join(str(tmp_path), "cluster.json")


# ── install decisions over the fabricated transport ─────────────────────────

def test_install_writes_the_exact_dropin_content_and_reexecs():
    tp = FakeTransport(
        state=[ok_state(), ok_state(usec="30s", present=True,
                                    sha=nr.dropin_sha256())],
        install=[{"ok": True, "stdout": "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.OK_APPLIED and r["armed"] is True and r["ok"] is True
    assert r["action"] == "applied"
    # The desired bytes travel base64-encoded; decode them and compare to the
    # module's canonical content — this is the card's "exact drop-in content".
    cmd = tp.calls("install")[0]["command"]
    b64 = [ln.split("=", 1)[1] for ln in cmd.splitlines()
           if ln.startswith("export HSCC_WANTED_B64=")][0]
    assert base64.b64decode(b64).decode() == nr.DROPIN_CONTENT
    assert "systemctl daemon-reexec" in cmd
    # And no real address leaked into the script.
    assert "10.0.0." not in cmd


def test_second_run_is_a_noop(tmp_path):
    tp = FakeTransport(state=[ok_state(present=True, sha=nr.dropin_sha256(),
                                       usec="30s")])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.OK_UNCHANGED and r["action"] == "unchanged"
    assert r["armed"] is True
    assert tp.calls("install") == [], "unchanged node must not be written again"


def test_operator_edited_file_is_left_alone_and_reported_as_drift():
    tp = FakeTransport(state=[ok_state(present=True, sha="deadbeef" * 8,
                                       usec="300s")])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.DRIFT
    assert r["armed"] is False and r["ok"] is False
    assert "did not write" in r["message"] and NODE in r["message"]
    assert r["code"] == nr.RC_DRIFT
    assert tp.calls("install") == [], "drift must never be clobbered by an install"


def test_node_side_drift_refusal_also_reports_drift():
    """Even if our pre-check missed it, the node's own refusal maps to drift."""
    tp = FakeTransport(
        state=[ok_state(), ok_state(present=True, sha="ff" * 32, usec="300s")],
        install=[{"ok": False, "code": nr.RC_DRIFT, "stderr": "",
                  "stdout": "HSCC_CODE: DRIFT_FOREIGN\n"
                            "HSCC_MSG: /etc/... holds content HSCC did not write\n"
                            f"HSCC_RC: {nr.RC_DRIFT}\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.DRIFT and r["armed"] is False


def test_opt_out_skips_and_reports_without_touching_the_node(tmp_path):
    tp = FakeTransport(state=[ok_state()])
    r = nr.arm_node(NODE, ssh_cmd=tp, enabled=False)
    assert r["outcome"] == nr.SKIP_DISABLED
    assert r["armed"] is False and r["ok"] is False   # not protected...
    assert "opted out" in r["message"]
    assert tp.commands == [], "an opt-out must issue ZERO remote commands"


def test_fleet_opt_out_skips_every_node(tmp_path):
    tp = FakeTransport(state=[ok_state()])
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=lambda h: tp,
                           config_path=_cfg(tmp_path, {"watchdog": False}))
    assert summary["enabled"] is False and summary["ok"] is True
    assert summary["opted_out"] == [NODE, OTHER]
    assert summary["protected"] == []
    assert tp.commands == []


def test_fleet_opt_out_names_the_still_armed_risk(tmp_path):
    """`watchdog: false` does not disarm a node armed by an earlier run — the
    report must say so rather than implying the node is now unmanaged and safe."""
    tp = FakeTransport(state=[ok_state()])
    summary = nr.arm_fleet([NODE], ssh_factory=lambda h: tp,
                           config_path=_cfg(tmp_path, {"watchdog": False}))
    assert "STILL" in summary["nodes"][0]["message"]
    assert "--disarm" in summary["nodes"][0]["message"]


# ── dry-run contract ────────────────────────────────────────────────────────

def test_dry_run_writes_nothing_and_shows_the_exact_diff():
    tp = FakeTransport(state=[ok_state()])
    r = nr.arm_node(NODE, ssh_cmd=tp, dry_run=True)
    assert r["action"] == "dry-run"
    assert tp.calls("install") == [], "dry-run must never run install mode"
    assert "RuntimeWatchdogSec=30s" in r["message"]
    assert "daemon-reexec" in r["message"]
    assert nr.DROPIN_PATH in r["message"]
    assert r["would_write"] is True


def test_dry_run_reports_unchanged_and_drift_without_writing():
    tp = FakeTransport(state=[ok_state(present=True, sha=nr.dropin_sha256(),
                                       usec="30s")])
    r = nr.arm_node(NODE, ssh_cmd=tp, dry_run=True)
    assert r["outcome"] == nr.OK_UNCHANGED and r["would_write"] is False

    tp2 = FakeTransport(state=[ok_state(present=True, sha="ab" * 32, usec="60s")])
    r2 = nr.arm_node(NODE, ssh_cmd=tp2, dry_run=True)
    assert r2["outcome"] == nr.DRIFT and r2["would_write"] is False
    assert "DRIFT" in r2["message"]
    assert not tp2.calls("install") and not tp.calls("install")


def test_fleet_dry_run_flags_itself_and_writes_nothing():
    tp = FakeTransport(state=[ok_state()])
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=lambda h: tp, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["ok"] is True
    assert not tp.calls("install")
    assert "DRY RUN" in "\n".join(nr.format_report(summary))


# ── never steal a device ────────────────────────────────────────────────────

def test_foreign_holder_is_refused_never_stolen():
    tp = FakeTransport(
        state=[ok_state(fuser="1234 5678", usec="0")],
        install=[{"ok": False, "code": nr.RC_FOREIGN_HOLDER, "stderr": "",
                  "stdout": "HSCC_CODE: FOREIGN_HOLDER\n"
                            "HSCC_MSG: /dev/watchdog0 is held by pid(s) [1234 5678]\n"
                            f"HSCC_RC: {nr.RC_FOREIGN_HOLDER}\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.REFUSED_FOREIGN
    assert r["armed"] is False and r["ok"] is False
    assert "refusing to steal" in r["message"] or "held by" in r["message"]
    assert tp.calls("install") and len(tp.calls("install")) == 1, "no retry storm"


def test_foreign_holder_detected_by_the_read_only_probe():
    """The probe surface (doctor) must see a foreign holder without writing."""
    tp = FakeTransport(state=[ok_state(fuser="4242", usec="0")])
    r = nr.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.REFUSED_FOREIGN and r["armed"] is False
    assert "4242" in r["message"]
    assert not tp.calls("install")


def test_no_watchdog_core_refuses_rather_than_arming_a_dead_timeout():
    tp = FakeTransport(state=[ok_state(watchdog_class="missing", devices="")])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["armed"] is False
    assert r["outcome"] == nr.ERROR and tp.calls("install") == []


# ── environment faults must never read as ok ───────────────────────────────

def test_unreachable_node_is_an_error_not_unarmed_ok():
    tp = FakeTransport(state=[{"ok": False, "stdout": "", "stderr":
                               "ssh: connect to host timed out", "code": 255}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["ok"] is False and r["armed"] is False
    assert "timed out" in r["message"] and NODE in r["message"]


def test_probe_of_unreachable_node_is_error():
    tp = FakeTransport(state=[{"ok": False, "stdout": "", "stderr": "no route", "code": 1}])
    r = nr.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["armed"] is False


def test_truncated_probe_output_is_a_fault_not_a_state():
    """A killed script leaves no HSCC_RC: that is an environment fault."""
    tp = FakeTransport(state=[{"ok": True, "stdout": "HSCC_STATE_BEGIN: 1\n",
                               "stderr": "killed", "code": 137}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "rc=" in r["message"] or "probe incomplete" in r["message"]


def test_install_succeeding_but_watchdog_not_taken_is_error():
    """Write succeeded, PID1 never adopted it -> must NOT read as protected."""
    tp = FakeTransport(
        state=[ok_state(), ok_state(usec="0")],
        install=[{"ok": True, "stdout": "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "RuntimeWatchdogUSec" in r["message"]


def test_reexec_failure_is_reported_as_a_fault():
    tp = FakeTransport(
        state=[ok_state(), ok_state(usec="0")],
        install=[{"ok": False, "code": nr.RC_REEXEC_FAILED, "stderr": "",
                  "stdout": "HSCC_ACTION: applied_reexec_failed\n"
                            "HSCC_CODE: REEXEC_FAILED\n"
                            "HSCC_MSG: daemon-reexec failed\n"
                            f"HSCC_RC: {nr.RC_REEXEC_FAILED}\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["armed"] is False


def test_sudo_refusal_is_a_clear_node_failure_not_a_hang():
    tp = FakeTransport(
        state=[ok_state()],
        install=[{"ok": False, "code": nr.RC_NEED_ROOT, "stderr": "",
                  "stdout": "HSCC_CODE: NEED_ROOT\n"
                            "HSCC_MSG: sudo -n refused on this node\n"
                            f"HSCC_RC: {nr.RC_NEED_ROOT}\n"}])
    r = nr.arm_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "sudo" in r["message"].lower()


def test_arm_node_never_raises_on_a_broken_transport():
    def broken(command, timeout):
        raise RuntimeError("ssh blew up")
    r = nr.arm_node(NODE, ssh_cmd=broken)
    assert r["outcome"] == nr.ERROR and r["armed"] is False


def test_non_dict_transport_result_is_a_fault():
    r = nr.arm_node(NODE, ssh_cmd=lambda c, t: "not a dict")
    assert r["outcome"] == nr.ERROR and "expected dict" in r["message"]


# ── fleet summary ───────────────────────────────────────────────────────────

def _armed_transport():
    return FakeTransport(state=[ok_state(present=True, sha=nr.dropin_sha256(),
                                        usec="30s")])


def test_fleet_ok_requires_every_node_protected():
    tps = {NODE: _armed_transport(),
           OTHER: FakeTransport(state=[{"ok": False, "stdout": "",
                                        "stderr": "unreachable", "code": 255}])}
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=lambda h: tps[h])
    assert summary["ok"] is False
    assert summary["protected"] == [NODE]
    assert [a["node"] for a in summary["needing_attention"]] == [OTHER]


def test_fleet_all_armed_is_ok():
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=lambda h: _armed_transport())
    assert summary["ok"] is True and summary["protected"] == [NODE, OTHER]
    assert summary["needing_attention"] == []


def test_fleet_isolates_a_node_that_crashes():
    def factory(host):
        if host == NODE:
            return _armed_transport()

        def boom(command, timeout):
            raise RuntimeError("kaboom")
        return boom
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=factory)
    assert summary["protected"] == [NODE]
    assert summary["needing_attention"][0]["node"] == OTHER
    assert "kaboom" in summary["needing_attention"][0]["message"]


def test_summary_names_the_target_so_doctor_cannot_diverge():
    summary = nr.arm_fleet([NODE], ssh_factory=lambda h: _armed_transport())
    assert summary["target"]["RuntimeWatchdogSec"] == "30s"
    assert summary["target"]["dropin"] == nr.DROPIN_PATH


def test_report_is_plaintext_and_names_each_node():
    summary = nr.arm_fleet([NODE, OTHER], ssh_factory=lambda h: _armed_transport())
    text = "\n".join(nr.format_report(summary))
    assert NODE in text and OTHER in text
    assert "─" not in text and "✓" not in text, "no Rich/box art (bootstrap embeds it)"


# ── probe surface contract for the sibling doctor card ─────────────────────

def test_probe_never_writes():
    tp = FakeTransport(state=[ok_state(present=True, sha=nr.dropin_sha256(),
                                        usec="30s")])
    nr.probe_node(NODE, ssh_cmd=tp)
    assert not tp.calls("install") and not tp.calls("uninstall")


def test_probe_result_carries_what_the_doctor_needs():
    tp = FakeTransport(state=[ok_state(present=True, sha=nr.dropin_sha256(),
                                        usec="30s", devices="watchdog0",
                                        identity="SBSA Generic Watchdog")])
    r = nr.probe_node(NODE, ssh_cmd=tp)
    assert r["armed"] is True and r["outcome"] == nr.OK_UNCHANGED
    assert r["state"]["runtime_watchdog_usec"] == "30s"
    assert r["state"]["present"] is True
    assert r["state"]["watchdog_devices"] == "watchdog0"
    assert r["state"]["watchdog0_identity"] == "SBSA Generic Watchdog"
    assert r["expected"] == {"RuntimeWatchdogSec": "30s", "RebootWatchdogSec": "2min"}


def test_armed_outcomes_membership_is_the_protection_test():
    assert nr.ARMED_OUTCOMES == {nr.OK_APPLIED, nr.OK_UNCHANGED}
    for bad in (nr.DRIFT, nr.REFUSED_FOREIGN, nr.ERROR, nr.SKIP_DISABLED):
        assert bad not in nr.ARMED_OUTCOMES


# ── the generated script itself ────────────────────────────────────────────

def test_generated_scripts_are_valid_bash_for_every_mode():
    for mode in ("state", "install", "uninstall"):
        script = nr.install_script(mode)
        assert script.startswith("#!/usr/bin/env bash")
        assert f'MODE="{mode}"' in script
        assert "__" not in script.replace("__ARMED_RE__", ""), "unreplaced token"
        proc = subprocess.run(["bash", "-n", "-s"], input=script,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{mode}: bash -n failed: {proc.stderr}"


def test_generated_script_ends_with_hsc_rc_on_every_path():
    """A failure that returns no HSCC_RC is indistinguishable from a node that
    is simply fine — so every exit path must emit one."""
    script = nr.install_script("install")
    assert "say \"HSCC_RC:" in script
    assert script.rstrip().endswith("exit 0")
    assert script.count("HSCC_RC:") >= 6


def test_unknown_mode_rejected_client_side():
    with pytest.raises(ValueError):
        nr.install_script("yolo")


def test_no_real_addresses_in_the_module_or_generated_script():
    src = open(os.path.join(os.path.dirname(nr.__file__), "node_recovery.py")).read()
    script = nr.install_script("install")
    import re
    bad = re.compile(r"\b(?:192\.168\.\d+\.\d+|100\.6[5-9]\.\d+\.\d+|100\.(?:7\d|8\d|9\d|1[01]\d|12[0-7])\.\d+\.\d+)\b")
    assert not bad.search(src), bad.findall(src)
    assert not bad.search(script)


# ── bash-backed fake node: runs the REAL node-side script ──────────────────

class FakeNode:
    """Executes the generated script against a sandbox node.

    Shims: ``sudo`` (exec, no-op), ``systemctl`` (emulates PID1 adopting the
    drop-in ONLY on daemon-reexec), ``sha256sum`` (macOS has only shasum).
    ``fuser`` is deliberately absent unless ``holders`` is set — the script must
    not need it.

    macOS has no /sys and no /dev/watchdog0, so the sandbox's stand-ins are
    handed to the module through :class:`node_recovery.NodePaths`. Production
    never overrides it; the defaults stay the real paths.
    """

    def __init__(self, tmp_path, *, usec="0", existing=None, holders=None,
                 env_extra=None):
        self.root = tmp_path / "node"
        self.bin = self.root / "bin"
        self.etc = self.root / "etc" / "systemd" / "system.conf.d"
        self.sysfs = self.root / "sys" / "class" / "watchdog"
        self.dev_dir = self.root / "dev"
        for d in (self.bin, self.etc, self.sysfs / "watchdog0", self.dev_dir):
            d.mkdir(parents=True)
        wd0 = self.sysfs / "watchdog0"
        # Mirrors the real GB10 attributes (measured): identity, not name;
        # state starts `inactive` and only becomes `armed` once the emulator's
        # systemctl adopts the drop-in on daemon-reexec.
        (wd0 / "identity").write_text("SBSA Generic Watchdog\n")
        (wd0 / "state").write_text("inactive\n")
        (wd0 / "timeout").write_text("10\n")
        (wd0 / "min_timeout").write_text("1\n")
        (wd0 / "max_timeout").write_text("0\n")
        self.dev = self.dev_dir / "watchdog0"
        self.dev.write_text("")
        self.dropin = self.etc / "90-hscc-watchdog.conf"
        self.state = self.root / "systemd_state"
        self.state.write_text(usec)
        if existing is not None:
            self.dropin.write_text(existing)
        self.holders = holders or ""
        self.env_extra = env_extra or {}

        self._write("sudo", '#!/bin/sh\nif [ "$1" = "-n" ] && [ "$2" = "true" ]; then '
                            'exit ${HSCC_FAKE_SUDO_RC:-0}; fi\n'
                            'if [ "$1" = "-n" ]; then shift; fi\nexec "$@"\n')
        self._write("sha256sum", "#!/bin/sh\nexec shasum -a 256 \"$@\"\n")
        self._write("systemctl", self._systemctl())
        if self.holders:
            self._write("fuser", f'#!/bin/sh\necho "{self.holders}"\nexit 0\n')

    @property
    def paths(self) -> nr.NodePaths:
        return nr.NodePaths(dropin=str(self.dropin), sysfs_dir=str(self.sysfs),
                            dev=str(self.dev))

    def _write(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def _systemctl(self):
        # daemon-reexec is where real PID1 opens /dev/watchdog0: the emulator
        # mirrors that by flipping sysfs `state` from inactive to armed, so a
        # "config written but nothing petting it" bug cannot hide behind the
        # emulator's optimism. HSCC_FAKE_ARM_STATE overrides it (e.g. "inactive"
        # to exercise the config-only false green).
        return (
            "#!/bin/sh\n"
            f'ST="{self.state}"; DROP="{self.dropin}"\n'
            f'WDSTATE="{self.sysfs / "watchdog0" / "state"}"\n'
            'if [ "$1" = "daemon-reexec" ]; then\n'
            '  # PID1 re-reads Manager: adopt RuntimeWatchdogSec from the drop-in\n'
            '  if [ -f "$DROP" ]; then\n'
            '    v=$(grep -E "^RuntimeWatchdogSec=" "$DROP" | tail -1 | cut -d= -f2)\n'
            '    printf "%s" "${v:-0}" > "$ST"\n'
            '    printf "%s" "${HSCC_FAKE_ARM_STATE:-armed}" > "$WDSTATE"\n'
            '  else printf "0" > "$ST"; printf "inactive" > "$WDSTATE"; fi\n'
            '  exit 0\n'
            'fi\n'
            'if [ "$1" = "show" ]; then\n'
            '  case "$2" in\n'
            '    -p) p="$3";;\n'
            '  esac\n'
            '  if [ "$p" = "RuntimeWatchdogUSec" ]; then printf "%s\\n" "$(cat "$ST")"; exit 0; fi\n'
            '  printf "\\n"; exit 0\n'
            'fi\n'
            'exit 0\n')

    def transport(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["TMPDIR"] = str(self.root)
        env.update(self.env_extra)

        def run(command, timeout):
            proc = subprocess.run(["bash", "-s"], input=command, capture_output=True,
                                  text=True, env=env, timeout=60)
            return {"ok": proc.returncode == 0, "stdout": proc.stdout,
                    "stderr": proc.stderr, "code": proc.returncode}
        return run


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
needs_shasum = pytest.mark.skipif(not shutil.which("shasum"),
                                  reason="sha256sum shim needs shasum")


@needs_bash
@needs_shasum
def test_real_script_creates_the_file_with_exact_bytes(tmp_path):
    node = FakeNode(tmp_path)
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["outcome"] == nr.OK_APPLIED, r["message"]
    assert node.dropin.read_text() == nr.DROPIN_CONTENT, "bytes on disk == module content"
    assert node.state.read_text() == "30s", "systemd adopted the target after reexec"
    assert r["armed"] is True


@needs_bash
@needs_shasum
def test_real_script_second_run_is_a_noop(tmp_path):
    node = FakeNode(tmp_path)
    tp = node.transport()
    assert nr.arm_node(NODE, ssh_cmd=tp, paths=node.paths)["outcome"] == nr.OK_APPLIED
    before = node.dropin.read_bytes()
    r2 = nr.arm_node(NODE, ssh_cmd=tp, paths=node.paths)
    assert r2["outcome"] == nr.OK_UNCHANGED and r2["armed"] is True
    assert node.dropin.read_bytes() == before


@needs_bash
@needs_shasum
def test_real_script_leaves_an_operator_edited_file_untouched(tmp_path):
    edited = "[Manager]\nRuntimeWatchdogSec=300s\n"
    node = FakeNode(tmp_path, existing=edited)
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["outcome"] == nr.DRIFT and r["armed"] is False
    assert node.dropin.read_text() == edited, "operator's file must survive byte-for-byte"


@needs_bash
@needs_shasum
def test_real_script_refuses_a_foreign_holder(tmp_path):
    node = FakeNode(tmp_path, holders="991 ")   # fuser reports a pid, timeout unset
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["outcome"] == nr.REFUSED_FOREIGN and r["armed"] is False
    assert not node.dropin.exists(), "refused node must have nothing written"


@needs_bash
@needs_shasum
def test_real_script_dry_run_writes_nothing(tmp_path):
    node = FakeNode(tmp_path)
    tp = node.transport()
    r = nr.arm_node(NODE, ssh_cmd=tp, dry_run=True, paths=node.paths)
    assert r["action"] == "dry-run" and not node.dropin.exists()
    assert "RuntimeWatchdogSec=30s" in r["message"]


@needs_bash
@needs_shasum
def test_real_script_refuses_when_no_watchdog_core(tmp_path):
    """No core => arming sets a timeout nothing can pet. Must refuse, write
    nothing, and never get as far as escalating."""
    node = FakeNode(tmp_path)
    dead = nr.NodePaths(dropin=str(node.dropin),
                        sysfs_dir=str(node.root / "no-such-sysfs"),
                        dev=str(node.dev))
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=dead)
    assert r["armed"] is False and not node.dropin.exists()
    assert "no watchdog core" in r["message"].lower()


@needs_bash
@needs_shasum
def test_real_script_refuses_when_device_node_is_missing(tmp_path):
    """/sys says a core exists but /dev/watchdog0 does not -> driver mismatch."""
    node = FakeNode(tmp_path)
    node.dev.unlink()
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["armed"] is False and not node.dropin.exists()


@needs_bash
@needs_shasum
def test_real_script_disarm_removes_only_our_file(tmp_path):
    node = FakeNode(tmp_path)
    tp = node.transport()
    nr.arm_node(NODE, ssh_cmd=tp, paths=node.paths)
    assert nr.disarm_node(NODE, ssh_cmd=tp, paths=node.paths)["action"] == "removed"
    assert not node.dropin.exists()

    node2 = FakeNode(tmp_path / "other", existing="[Manager]\nRuntimeWatchdogSec=9s\n")
    r = nr.disarm_node(OTHER, ssh_cmd=node2.transport(), paths=node2.paths)
    assert r["outcome"] == nr.DRIFT and node2.dropin.exists(), \
        "disarm must never delete a file HSCC did not write"


@needs_bash
@needs_shasum
def test_real_script_sudo_refusal_fails_that_node_clearly(tmp_path):
    node = FakeNode(tmp_path, env_extra={"HSCC_FAKE_SUDO_RC": "1"})
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "sudo" in r["message"].lower()
    assert not node.dropin.exists()


@needs_bash
@needs_shasum
def test_real_script_rejects_config_armed_but_device_silent(tmp_path):
    """systemd adopting the timeout is NOT protection; PID1 must hold the core.

    The node writes our bytes and reports RuntimeWatchdogUSec=30s, but its
    watchdog core stays `state=inactive` — the case where a config-only check
    would call the node protected while a hard hang would wedge it forever.
    """
    node = FakeNode(tmp_path, env_extra={"HSCC_FAKE_ARM_STATE": "inactive"})
    r = nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert node.dropin.exists(), "the bytes were in fact written"
    assert r["state"]["runtime_watchdog_usec"] == "30s", "systemd did adopt it"
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "nothing is petting" in r["message"]
    assert "/dev/watchdog0" in r["message"]


@needs_bash
@needs_shasum
def test_real_script_probe_flags_config_armed_but_device_silent(tmp_path):
    """The same false green must show up in --probe (read-only), not just at install."""
    node = FakeNode(tmp_path, env_extra={"HSCC_FAKE_ARM_STATE": "inactive"})
    nr.arm_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    r = nr.probe_node(NODE, ssh_cmd=node.transport(), paths=node.paths)
    assert r["outcome"] == nr.ERROR and r["armed"] is False
    assert "state='inactive'" in r["message"]
