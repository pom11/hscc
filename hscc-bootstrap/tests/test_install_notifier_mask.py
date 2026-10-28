"""The snap firmware-notifier mask install (card t_61850b24).

Three layers, each proving something the others cannot:

  * fabricated-transport tests — a scripted dict responder for ``ssh_cmd``,
    asserting the *decisions* (mask / no-op / already-masked / drift / opt-out /
    dry-run / unreachable / no-bus) without a shell;
  * command-shape + bus tests — the mask command's exact unit names and the
    bus-bootstrap lines, since the card's named pitfall is a ``systemctl --user``
    call that silently loses its session bus;
  * a bash-backed fake node (``FakeNode``) — runs the REAL generated node-side
    script against a shimmed ``systemctl --user``. The emulation is the real
    contract: masking flips ``is-enabled`` to ``masked`` AND makes
    ``is-active`` stop reporting a running unit, so a bug that "masks" without
    read-back verification fails here rather than looking green in Python.

Two rules are pinned everywhere: **nothing privileged** (no ``sudo`` anywhere in
the generated command, so the stage can never escalate on any cluster node to stop a
user-session notifier) and **an environment fault is never a data fact** (a
node we could not read is ``error``, never ``masked`` and never "not masked").

No test here opens a socket, resolves a host, or contacts a node. Node
addresses are the documented 10.0.0.x placeholders.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List

import pytest

import install_notifier_mask as nm

NODE = "10.0.0.1"
OTHER = "10.0.0.2"
SVC = nm.NOTIFIER_SERVICE
TIMER = nm.NOTIFIER_TIMER


# ── fabricated transport ────────────────────────────────────────────────────

def state_lines(*, service="static", timer="enabled",
                svc_active="active", timer_active="active",
                svc_link="", timer_link="", with_state_block=True):
    """A node's `state`-mode stdout exactly as the generated script prints it.

    Field names/shapes mirror what the real nodes emit (measured: service
    ``static`` + ``active``, timer ``enabled`` + ``active``, no mask link).
    """
    out = ["HSCC_STATE_BEGIN: 1",
           f"HSCC_MASK_DIR: {nm.MASK_DIR_TEMPLATE}",
           "HSCC_UID: 1000",
           "HSCC_XDG_RUNTIME_DIR: /run/user/1000"]
    if with_state_block:
        out += [f"HSCC_UNIT_0: {SVC}",
                f"HSCC_UNIT_0_STATE: {service}",
                f"HSCC_UNIT_0_ACTIVE: {svc_active}",
                f"HSCC_UNIT_0_LINK: {svc_link}",
                f"HSCC_UNIT_1: {TIMER}",
                f"HSCC_UNIT_1_STATE: {timer}",
                f"HSCC_UNIT_1_ACTIVE: {timer_active}",
                f"HSCC_UNIT_1_LINK: {timer_link}",
                "HSCC_UNIT_COUNT: 2",
                "HSCC_STATE_END: 1"]
    out.append("HSCC_RC: 0")
    return "\n".join(out) + "\n"


def masked_state(**kw):
    """State as it reads AFTER a successful mask (what the read-back must see)."""
    kw.setdefault("service", "masked")
    kw.setdefault("timer", "masked")
    kw.setdefault("svc_active", "inactive")
    kw.setdefault("timer_active", "inactive")
    return state_lines(**kw)


class FakeTransport:
    """Scripted node: answers each command kind from a queue, records everything.

    ``kinds`` maps a command kind — inferred from the mode baked into the
    shipped script ("state"/"install"/"uninstall") — to a list of responses. A
    response list is consumed front-to-back and the last entry repeats.
    """

    def __init__(self, **kinds):
        self.kinds = {k: list(v) for k, v in kinds.items()}
        self.calls: List[Dict[str, Any]] = []

    @staticmethod
    def _kind(command: str) -> str:
        # The script ships base64'd in a `base64 -d | bash -s` pipe, so the mode
        # has to be read out of the decoded bytes — which is itself the shape
        # test the transport sees what command_for() produced.
        m = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", command)
        if not m:
            return "unknown"
        script = base64.b64decode(m.group(1)).decode()
        mode = re.search(r'^MODE="(\w+)"', script, re.M)
        return mode.group(1) if mode else "unknown"

    def __call__(self, command: str, timeout: int) -> Dict[str, Any]:
        kind = self._kind(command)
        self.calls.append({"kind": kind, "command": command, "timeout": timeout})
        queue = self.kinds.get(kind)
        if queue is None:
            return {"ok": False, "stdout": "", "code": 127,
                    "stderr": f"no scripted response for {kind!r}"}
        res = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else {})
        if callable(res):
            res = res(command)
        if isinstance(res, str):
            # A fixture may hand back just the stdout a node would print; that
            # is a *successful* answer, not a transport failure.
            res = {"stdout": res}
        res = dict(res)
        res.setdefault("ok", True)
        res.setdefault("stdout", "")
        res.setdefault("stderr", "")
        res.setdefault("code", 0)
        return res

    def commands(self, kind: str) -> List[str]:
        return [c["command"] for c in self.calls if c["kind"] == kind]

    def decoded(self, kind: str) -> str:
        cmds = self.commands(kind)
        assert cmds, f"no {kind} command was sent"
        m = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", cmds[0])
        return base64.b64decode(m.group(1)).decode()


def ok_transport(**overrides):
    """Transport where state says masked; ``kinds`` overrides win."""
    kinds = {"state": [masked_state()]}
    kinds.update(overrides)
    return FakeTransport(**kinds)


# ── the managed set + one-source constants ─────────────────────────────────

def test_managed_units_are_the_notifier_service_and_its_timer_only():
    assert nm.NOTIFIER_UNITS == (SVC, TIMER)
    # The snap's own app service must NOT be in scope (card: snap untouched).
    assert "firmware-updater-app" not in " ".join(nm.NOTIFIER_UNITS)
    assert "firmware-updater.service" not in " ".join(nm.NOTIFIER_UNITS)


def test_masked_words_are_the_mask_spellings_only():
    assert nm.MASKED_WORDS == {"masked", "masked-runtime"}
    # "static" is the measured pre-mask answer and must NOT count as masked.
    assert "static" not in nm.MASKED_WORDS
    assert "enabled" not in nm.MASKED_WORDS


def test_is_masked_state_requires_every_managed_unit_masked():
    assert nm.is_masked_state({"units": [{"state": "masked"},
                                         {"state": "masked-runtime"}]}) is True
    assert nm.is_masked_state({"units": [{"state": "masked"},
                                         {"state": "static"}]}) is False
    # Empty/missing state is NOT "all masked" — that would be a false green.
    assert nm.is_masked_state({"units": []}) is False
    assert nm.is_masked_state({}) is False


# ── command shape + the bus pitfall ─────────────────────────────────────────

def test_state_command_carries_the_bus_bootstrap_before_any_systemctl_call():
    """The card's named pitfall: `systemctl --user` over ssh needs the session
    bus. The two exports must precede the first systemctl invocation, and must
    be `${VAR:-derived}` so a session that DID export them keeps its values."""
    script = nm.install_script("state")
    # Only executable lines count: the header comments mention `systemctl` (and
    # would otherwise "prove" the exports come late).
    code = "\n".join(ln for ln in script.splitlines()
                     if not ln.lstrip().startswith("#"))
    first_call = code.index("systemctl")
    exports = [ln for ln in code.splitlines()
               if ln.startswith("export XDG_RUNTIME_DIR") or
               ln.startswith("export DBUS_SESSION_BUS_ADDRESS")]
    assert len(exports) == 2, "both bus variables must be set"
    assert code.index(exports[0]) < first_call
    assert code.index(exports[1]) < first_call
    for line in exports:
        assert "${XDG_RUNTIME_DIR:-" in line or "${DBUS_SESSION_BUS_ADDRESS:-" in line
        assert "$(id -u)" in line or "${XDG_RUNTIME_DIR}/bus" in line, \
            "the runtime dir must be derived from the actual uid, never a baked id"


def test_generated_scripts_are_valid_bash_and_contain_no_sudo():
    """bash -n proves parseability; the no-sudo assertion is the privilege
    boundary — this stage must never escalate on a node."""
    for mode in ("state", "install", "uninstall"):
        script = nm.install_script(mode)
        assert script.startswith("#!/usr/bin/env bash")
        assert f'MODE="{mode}"' in script
        left = re.findall(r"__[A-Z_]+__", script)
        assert not left, f"{mode}: unreplaced tokens {left}"
        proc = subprocess.run(["bash", "-n", "-s"], input=script,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{mode}: bash -n failed: {proc.stderr}"
        assert "sudo" not in script, f"{mode}: a privileged call in a user-unit stage"
        assert script.rstrip().endswith("exit 0")
        assert "HSCC_RC:" in script


def test_generated_script_ends_with_our_rc_on_every_path():
    """A failure that returns no HSCC_RC is indistinguishable from a healthy
    node, so every exit path must emit one."""
    script = nm.install_script("install")
    # `fail()` is the single failure exit and it emits the code; the success
    # paths each say it explicitly; and the script's last statement is a 0 exit.
    assert 'say "HSCC_RC: $1"' in script, "the failure helper must emit our rc"
    assert 'say "HSCC_RC: 0"' in script
    assert script.count("HSCC_RC:") >= 4
    assert script.rstrip().endswith("exit 0")
    assert not re.search(r"^exit \d+\s*$", script.rstrip().split("\n")[-2], re.M)


def test_systemctl_rc_is_never_load_bearing():
    """`is-enabled` exits 1 for `masked` and 4 for `not-found` — both healthy.
    The script must swallow those codes (`|| true`) rather than let them reach a
    verdict, and must compare the printed WORD."""
    script = nm.install_script("state")
    assert "systemctl --user is-enabled \"$1\" 2>&1 || true" in script
    assert "case \" $WORDS \" in" in script, "state must be matched against the word list"


def test_words_list_in_bash_is_the_python_constant():
    """One source for the recognised-state vocabulary: the bash matcher and the
    Python parser cannot drift onto different word sets."""
    script = nm.install_script("state")
    m = re.search(r'^WORDS=" (.*) "', script, re.M)
    assert m, "WORDS line missing"
    assert set(m.group(1).split()) == set(nm.MANAGED_STATE_WORDS)


def test_command_for_is_a_single_quoted_base64_pipe():
    cmd = nm.command_for("state")
    assert cmd.startswith("printf '%s' '")
    assert cmd.endswith("| base64 -d | bash -s")
    assert "<<" not in cmd, "no nested heredoc (the transport already pipes one)"


def test_unknown_mode_rejected_client_side():
    with pytest.raises(ValueError):
        nm.install_script("yolo")


def test_install_masks_every_managed_unit():
    tp = FakeTransport(state=[state_lines(), masked_state()],
                       install=[{"ok": True, "stdout":
                                 f"HSCC_MASKED: {SVC}\nHSCC_MASKED: {TIMER}\n"
                                 "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.OK_APPLIED, r["message"]
    script = tp.decoded("install")
    for unit in nm.NOTIFIER_UNITS:
        assert unit in script
    assert "mask --now" in script, "masking without stopping leaves the loop running"


def test_read_back_is_what_makes_it_applied():
    """The verdict comes from the node's ANSWER after the write, not from the
    write's own report — the batch's standing rule."""
    tp = FakeTransport(state=[state_lines(),
                              {"ok": True, "stdout": state_lines(service="static"),
                               "code": 0}],   # "success" that did not mask
                       install=[{"ok": True, "stdout":
                                 f"HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR, "a mask that did not take must never be applied"
    assert "after mask" in r["message"] or "still reports" in r["message"]
    assert r["masked"] is False


def test_masked_but_still_running_is_not_protection():
    tp = FakeTransport(state=[state_lines(),
                              masked_state(svc_active="active")],
                       install=[{"ok": True, "stdout":
                                 "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR
    assert "still active" in r["message"]


# ── idempotence ─────────────────────────────────────────────────────────────

def test_already_masked_is_a_noop_with_no_write():
    tp = ok_transport()
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.OK_UNCHANGED, r["message"]
    assert r["action"] == "unchanged"
    assert tp.commands("install") == [], "already-masked must not send a write"
    assert len(tp.calls) == 1


def test_second_run_stays_a_noop():
    tp = ok_transport()
    first = nm.mask_node(NODE, ssh_cmd=tp)
    second = nm.mask_node(NODE, ssh_cmd=tp)
    assert first["outcome"] == second["outcome"] == nm.OK_UNCHANGED
    assert tp.commands("install") == []


def test_partially_masked_node_is_red_not_green():
    """Service masked but its timer live is a HALF-managed unit: it must not
    read as protected."""
    tp = FakeTransport(state=[masked_state(timer="enabled", timer_active="active")])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR and r["masked"] is False
    assert TIMER in r["message"]


# ── drift: never clobber a foreign override ─────────────────────────────────

def test_foreign_mask_link_is_refused_not_replaced():
    tp = FakeTransport(state=[state_lines(service="masked",
                                          svc_link="/home/u/my-own.target")])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.DRIFT, r["message"]
    assert tp.commands("install") == [], "a foreign override must not be overwritten"


def test_node_side_drift_refusal_reports_drift():
    tp = FakeTransport(state=[state_lines()],
                       install=[{"ok": False, "stdout":
                                 "HSCC_CODE: DRIFT_FOREIGN\n"
                                 "HSCC_MSG: a user unit override we did not write\n"
                                 f"HSCC_RC: {nm.RC_DRIFT}\n", "code": nm.RC_DRIFT}])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.DRIFT
    assert "override" in r["message"]


# ── environment faults: never a data fact ───────────────────────────────────

def test_unreachable_node_is_an_error_never_masked():
    tp = FakeTransport(state=[{"ok": False, "stdout": "", "code": 255,
                               "stderr": "ssh: connect to host port 22: Connection refused"}])
    r = nm.mask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR and r["masked"] is False
    assert "Connection refused" in r["message"]


def test_no_bus_failure_surfaces_as_an_honest_error():
    """The measured no-bus text: `Failed to connect to bus: No medium found`.
    Read naively it is `state != masked` -> "not masked". It is not a unit state
    at all: the node is broken, not unmasked."""
    raw = ("HSCC_BUS_TEXT: Failed to connect to bus: No medium found\n"
           f"HSCC_CODE: NO_BUS\nHSCC_MSG: cannot reach the user manager bus "
           f"(XDG_RUNTIME_DIR=/run/user/1000); unit state UNREADABLE — this is "
           f"not 'not masked'\nHSCC_RC: {nm.RC_NO_BUS}\n")
    tp = FakeTransport(state=[{"ok": False, "stdout": raw, "code": nm.RC_NO_BUS}])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR
    assert "NOT MASKED" not in r["message"], "an env fault must not read as a fact"
    assert "UNVERIFIABLE" in r["message"]
    assert r["code_name"] == "NO_BUS"


def test_bus_error_text_inside_a_state_block_is_still_an_error():
    """rc=0 with a bus sentence where a word belongs is still a fault: anything
    outside MANAGED_STATE_WORDS is not a state."""
    raw = state_lines().replace(f"HSCC_UNIT_0_STATE: {SVC}", "x").replace(
        "HSCC_UNIT_0_STATE: static",
        "HSCC_UNIT_0_STATE: Failed to connect to bus: No medium found")
    tp = FakeTransport(state=[{"ok": True, "stdout": raw, "code": 0}])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR and r["masked"] is False
    assert "not a unit state" in r["message"]


def test_truncated_probe_output_is_a_fault_not_a_state():
    tp = FakeTransport(state=[{"ok": True, "stdout": "HSCC_STATE_BEGIN: 1\n",
                               "code": 0}])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR and "probe" in r["message"]


def test_missing_hsc_rc_is_a_fault():
    tp = FakeTransport(state=[{"ok": True, "stdout": state_lines().replace(
        "HSCC_RC: 0", ""), "code": 0}])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR


def test_non_dict_transport_result_is_a_fault():
    r = nm.mask_node(NODE, ssh_cmd=lambda c, t: "not a dict")
    assert r["outcome"] == nm.ERROR and "expected dict" in r["message"]


def test_mask_node_never_raises_on_a_broken_transport():
    def boom(_c, _t):
        raise RuntimeError("transport exploded")
    r = nm.mask_node(NODE, ssh_cmd=boom)
    assert r["outcome"] == nm.ERROR and "transport exploded" in r["message"]


def test_node_side_refusals_are_reported_verbatim_not_retried():
    for code_name, rc, needle in (("NO_SYSTEMD", nm.RC_NO_SYSTEMD, "systemctl not on PATH"),
                                  ("NO_HOME", nm.RC_NO_HOME, "HOME is unset"),
                                  ("MASK_FAILED", nm.RC_MASK_FAILED, "mask failed")):
        tp = FakeTransport(state=[state_lines()],
                           install=[{"ok": False, "code": rc,
                                     "stdout": f"HSCC_CODE: {code_name}\n"
                                               f"HSCC_MSG: {needle}\n"
                                               f"HSCC_RC: {rc}\n"}])
        r = nm.mask_node(NODE, ssh_cmd=tp)
        assert r["outcome"] == nm.ERROR, code_name
        assert needle in r["message"], code_name
        assert len(tp.commands("install")) == 1, "a refusal must not be retried"


# ── opt-out ─────────────────────────────────────────────────────────────────

def _cfg(tmp_path, block):
    p = tmp_path / "cluster.json"
    data = {"name": "fabricated", "gateway": {"ip": NODE}}
    if block is not None:
        data[nm.NODE_RECOVERY_KEY] = block
    p.write_text(json.dumps(data))
    return str(p)


def test_layer_defaults_on_when_block_absent(tmp_path):
    cfg = nm.notifier_mask_config(_cfg(tmp_path, None))
    assert cfg[nm.MASK_SUBKEY] is True and cfg["config_error"] == ""


def test_layer_defaults_on_when_file_absent(tmp_path):
    assert nm.notifier_mask_config(str(tmp_path / "nope.json"))[nm.MASK_SUBKEY] is True


def test_opt_out_false_is_honoured(tmp_path):
    assert nm.notifier_mask_enabled(
        _cfg(tmp_path, {nm.MASK_SUBKEY: False})) is False


def test_opt_out_skips_and_touches_no_node(tmp_path):
    tp = FakeTransport(state=[masked_state()])
    r = nm.mask_node(NODE, ssh_cmd=tp, enabled=False)
    assert r["outcome"] == nm.SKIP_DISABLED and r["masked"] is False
    assert tp.calls == [], "an opt-out must not send a single remote command"


def test_opt_out_does_not_touch_sibling_subkeys(tmp_path):
    """The batch rule: one subkey per layer, read independently."""
    cfg = nm.notifier_mask_config(
        _cfg(tmp_path, {"watchdog": False, "autoreboot": False}))
    assert cfg[nm.MASK_SUBKEY] is True and cfg["config_error"] == ""
    cfg2 = nm.notifier_mask_config(_cfg(tmp_path, {nm.MASK_SUBKEY: False}))
    assert cfg2[nm.MASK_SUBKEY] is False


def test_unknown_subkey_is_ignored_not_fatal(tmp_path):
    cfg = nm.notifier_mask_config(_cfg(tmp_path, {nm.MASK_SUBKEY: True, "future": 7}))
    assert cfg[nm.MASK_SUBKEY] is True and cfg["config_error"] == ""


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, "", [], {}])
def test_malformed_subkey_stays_on_and_reports(tmp_path, bad):
    cfg = nm.notifier_mask_config(_cfg(tmp_path, {nm.MASK_SUBKEY: bad}))
    assert cfg[nm.MASK_SUBKEY] is True
    assert nm.MASK_SUBKEY in cfg["config_error"] and "not true/false" in cfg["config_error"]


def test_non_mapping_block_reports_and_defaults(tmp_path):
    cfg = nm.notifier_mask_config(_cfg(tmp_path, "yes"))
    assert cfg[nm.MASK_SUBKEY] is True and "must be a mapping" in cfg["config_error"]


def test_corrupt_config_is_an_error_not_a_silent_default(tmp_path):
    p = tmp_path / "cluster.json"
    p.write_text("{not json")
    cfg = nm.notifier_mask_config(str(p))
    assert cfg[nm.MASK_SUBKEY] is True               # fails toward the safe default…
    assert "could not read" in cfg["config_error"]   # …and says so


def test_fleet_opt_out_skips_every_node_and_writes_nothing(tmp_path):
    tp = FakeTransport(state=[masked_state()])
    summary = nm.mask_fleet([NODE, OTHER], ssh_factory=lambda h: tp,
                            config_path=_cfg(tmp_path, {nm.MASK_SUBKEY: False}))
    assert summary["enabled"] is False and summary["ok"] is True
    assert summary["opted_out"] == [NODE, OTHER]
    assert tp.calls == []


def test_fleet_opt_out_names_the_still_masked_state(tmp_path):
    tp = FakeTransport(state=[masked_state()])
    summary = nm.mask_fleet([NODE], ssh_factory=lambda h: tp,
                            config_path=_cfg(tmp_path, {nm.MASK_SUBKEY: False}))
    msg = summary["nodes"][0]["message"]
    assert "STILL masked" in msg and "--unmask" in msg


def test_hscc_dir_honours_env_not_a_baked_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HSCC_DIR", str(tmp_path))
    assert nm.hscc_dir() == str(tmp_path)
    assert nm.cluster_config_path() == os.path.join(str(tmp_path), "cluster.json")


# ── dry-run ─────────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing_and_shows_the_exact_intent():
    tp = FakeTransport(state=[state_lines()])
    r = nm.mask_node(NODE, ssh_cmd=tp, dry_run=True)
    assert r["outcome"] == nm.OK_APPLIED and r["action"] == "dry-run"
    assert r["would_write"] is True
    assert tp.commands("install") == [] and tp.commands("uninstall") == []
    plan = r["plan"]
    assert SVC in plan and TIMER in plan
    assert "-> /dev/null" in plan
    assert "no sudo" in plan
    assert "firmware-updater-app" in plan, "the report must say what is NOT touched"


def test_dry_run_on_an_already_masked_node_reports_noop():
    tp = ok_transport()
    r = nm.mask_node(NODE, ssh_cmd=tp, dry_run=True)
    assert r["outcome"] == nm.OK_UNCHANGED and r["would_write"] is False
    assert tp.commands("install") == []


def test_fleet_dry_run_flags_itself_and_writes_nothing():
    tp = FakeTransport(state=[state_lines()])
    summary = nm.mask_fleet([NODE, OTHER], ssh_factory=lambda h: tp, dry_run=True)
    assert summary["dry_run"] is True
    assert tp.commands("install") == []
    assert "DRY RUN" in "\n".join(nm.format_report(summary))


def test_fleet_dry_run_of_unreachable_node_is_not_ok():
    tp = FakeTransport(state=[{"ok": False, "stdout": "", "code": 255,
                               "stderr": "unreachable"}])
    summary = nm.mask_fleet([NODE], ssh_factory=lambda h: tp, dry_run=True)
    assert summary["ok"] is False
    assert summary["needing_attention"][0]["outcome"] == nm.ERROR


# ── fleet + report ──────────────────────────────────────────────────────────

def test_fleet_ok_requires_every_node_masked():
    good = FakeTransport(state=[masked_state()])
    bad = FakeTransport(state=[state_lines()])
    summary = nm.mask_fleet([NODE, OTHER],
                            ssh_factory=lambda h: good if h == NODE else bad)
    assert summary["ok"] is False
    assert summary["masked"] == [NODE]
    assert summary["needing_attention"][0]["node"] == OTHER


def test_fleet_all_masked_is_ok():
    tp = ok_transport()
    summary = nm.mask_fleet([NODE, OTHER], ssh_factory=lambda h: tp)
    assert summary["ok"] is True and len(summary["masked"]) == 2


def test_fleet_isolates_a_node_that_crashes():
    def factory(host):
        if host == NODE:
            return FakeTransport(state=[masked_state()])

        def boom(_c, _t):
            raise ValueError("node blew up")
        return boom
    summary = nm.mask_fleet([NODE, OTHER], ssh_factory=factory)
    assert summary["masked"] == [NODE], "one crashing node must not lose the other"
    assert summary["ok"] is False


def test_summary_names_the_target_so_doctor_cannot_diverge():
    summary = nm.mask_fleet([], ssh_factory=lambda h: FakeTransport())
    assert summary["target"]["units"] == [SVC, TIMER]
    assert summary["target"]["snap_untouched"] == "firmware-updater"


def test_report_is_plaintext_names_each_node_and_the_untouched_snap():
    tp = FakeTransport(state=[state_lines(), masked_state()],
                       install=[{"stdout": "HSCC_ACTION: applied\nHSCC_RC: 0\n"}])
    summary = nm.mask_fleet([NODE], ssh_factory=lambda h: tp)
    report = "\n".join(nm.format_report(summary))
    assert NODE in report
    assert "firmware-updater snap itself is untouched" in report
    assert "\x1b" not in report


def test_bootstrap_lines_lead_with_warn_when_anything_is_unmasked():
    tp = FakeTransport(state=[state_lines()])
    summary = nm.mask_fleet([NODE, OTHER],
                            ssh_factory=lambda h: (tp if h == NODE else
                                                   FakeTransport(state=[state_lines()])))
    lines = nm.bootstrap_lines(summary)
    assert lines[0].startswith("WARN|"), "a partially-masked fleet must not lead with OK"
    assert any("0/2" in ln for ln in lines)


def test_bootstrap_lines_all_green_leads_with_ok_and_reports_counts():
    tp = ok_transport()
    summary = nm.mask_fleet([NODE, OTHER], ssh_factory=lambda h: tp)
    lines = nm.bootstrap_lines(summary)
    assert lines[0].startswith("OK|") and "2/2" in lines[0]
    assert "firmware-updater snap itself" in lines[0]


def test_bootstrap_lines_for_opt_out_is_an_ok_skip_that_says_so():
    tp = FakeTransport(state=[masked_state()])
    summary = nm.mask_fleet([NODE], ssh_factory=lambda h: tp,
                            config_path=None)
    # enabled by default here; exercise the opted-out summary directly:
    summary["enabled"] = False
    lines = nm.bootstrap_lines(summary)
    assert lines[0].startswith("OK|") and "skipped" in lines[0]
    assert nm.MASK_SUBKEY in lines[0]


def test_bootstrap_lines_for_empty_fleet_warns():
    summary = nm.mask_fleet([], ssh_factory=lambda h: FakeTransport())
    assert nm.bootstrap_lines(summary)[0].startswith("WARN|")


# ── unmask (explicit undo) ──────────────────────────────────────────────────

def test_unmask_sends_the_uninstall_script_and_reports():
    tp = FakeTransport(state=[masked_state()],
                       uninstall=[{"ok": True, "stdout":
                                   "HSCC_ACTION: unmasked_2\nHSCC_RC: 0\n"}])
    r = nm.unmask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.OK_APPLIED
    assert "can load again" in r["message"]
    assert tp.commands("uninstall")


def test_unmask_refuses_a_foreign_mask_link():
    tp = FakeTransport(state=[masked_state(svc_link="/home/u/own")],
                       uninstall=[{"ok": False, "code": nm.RC_DRIFT, "stdout":
                                   "HSCC_CODE: DRIFT_FOREIGN\n"
                                   "HSCC_MSG: points at '/home/u/own', not /dev/null\n"
                                   f"HSCC_RC: {nm.RC_DRIFT}\n"}])
    r = nm.unmask_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.DRIFT


def test_opt_out_never_unmasks():
    """Reading a config key must never delete state on a node the operator just
    told us to leave alone."""
    tp = FakeTransport(state=[masked_state()],
                       uninstall=[{"ok": True, "stdout": "HSCC_RC: 0\n"}])
    nm.mask_fleet([NODE], ssh_factory=lambda h: tp, unmask=False,
                  config_path=None)
    assert tp.commands("uninstall") == []


# ── probe must be read-only ─────────────────────────────────────────────────

def test_probe_never_writes():
    tp = FakeTransport(state=[masked_state()])
    nm.probe_node(NODE, ssh_cmd=tp)
    assert tp.commands("install") == [] and tp.commands("uninstall") == []
    assert tp.calls[0]["kind"] == "state"


def test_probe_result_carries_what_the_doctor_needs():
    tp = FakeTransport(state=[masked_state()])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["masked"] is True and r["units"] == [SVC, TIMER]
    assert r["state"]["states"] == ["masked", "masked"]
    assert r["state"]["transport_ok"] is True


def test_unmasked_probe_is_a_data_fact_saying_not_masked():
    """Unlike the bus fault, this IS a fact we read: the wording must be
    'NOT MASKED', and must name each unit's actual state."""
    tp = FakeTransport(state=[state_lines()])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["outcome"] == nm.ERROR and r["masked"] is False
    assert "NOT MASKED" in r["message"]
    assert f"{SVC}=static" in r["message"]


def test_masked_runtime_is_reported_as_masked_but_flagged():
    tp = FakeTransport(state=[masked_state(service="masked-runtime",
                                           timer="masked-runtime")])
    r = nm.probe_node(NODE, ssh_cmd=tp)
    assert r["masked"] is True, "a runtime mask does stop the loop today"
    assert "runtime" in r["message"] and "survive reboot" in r["message"]


# ── address discipline ──────────────────────────────────────────────────────

def test_no_real_addresses_in_module_or_generated_scripts():
    src = open(os.path.join(os.path.dirname(nm.__file__),
                            "install_notifier_mask.py")).read()
    bad = re.compile(r"\b(?:192\.168\.\d+\.\d+|100\.6[5-9]\.\d+\.\d+"
                     r"|100\.(?:7\d|8\d|9\d|1[01]\d|12[0-7])\.\d+\.\d+)\b")
    assert not bad.search(src), bad.findall(src)
    for mode in ("state", "install", "uninstall"):
        script = nm.install_script(mode)
        assert not bad.search(script)
        assert "10.0.0." in "10.0.0.x"  # placeholders are the only allowed form


# ── bash-backed fake node: runs the REAL generated script ──────────────────

class FakeNode:
    """Executes the generated script against a shimmed ``systemctl --user``.

    The emulation is deliberately honest about the two things a fake could lie
    about:

      * ``mask`` is what flips ``is-enabled`` to ``masked`` — and a script that
        masked but never stopped the unit must be caught by the ``is-active``
        leg, so a masked unit reports ``inactive`` only when the script asked
        systemd to stop it (``mask --now``, or mask + ``stop``);
      * ``is-enabled`` exits 1 for ``masked`` and 4 for ``not-found``, exactly
        like systemd 255 (measured), so the script must survive those codes.

    ``no_bus=True`` reproduces the failure the card names: systemctl can only
    answer with ``Failed to connect to bus``.
    """

    def __init__(self, tmp_path, *, service="static", timer="enabled",
                 no_bus=False, existing_link=None, now_supported=True,
                 mask_fails=False):
        self.root = tmp_path / "node"
        self.bin = self.root / "bin"
        self.mask_dir = self.root / "home" / ".config" / "systemd" / "user"
        self.bin.mkdir(parents=True)
        self.mask_dir.mkdir(parents=True)
        self.state = self.root / "unit_state.json"
        self.state.write_text(json.dumps({
            SVC: {"enabled": service, "active": "active"},
            TIMER: {"enabled": timer, "active": "active"},
        }))
        self.no_bus = no_bus
        self.mask_fails = mask_fails
        self.now_supported = now_supported
        self.calls = self.root / "systemctl_calls"
        self.calls.write_text("")
        if existing_link:
            for unit, target in existing_link.items():
                (self.mask_dir / unit).symlink_to(target)
        self._write("systemctl", self._systemctl())

    def _write(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def _systemctl(self):
        """A stateful user manager, modelled on the parts that matter.

        Mask semantics are systemd's: ``mask`` writes a ``/dev/null`` symlink in
        the user unit dir, ``is-enabled`` reports ``masked`` **because of that
        symlink** (so the script cannot fake a pass by mutating some other
        store), and ``--now`` additionally stops the unit. Exit codes match
        systemd 255 as measured on the fleet: ``masked`` -> 1, ``not-found`` ->
        4, ``inactive`` from ``is-active`` -> 4.
        """
        return (
            "#!/bin/sh\n"
            f'S="{self.state}"; CALLS="{self.calls}"; M="{self.mask_dir}"\n'
            'if [ "$1" != "--user" ]; then echo "user manager only" >&2; exit 1; fi\n'
            'shift\n'
            'echo "$@" >> "$CALLS"\n'
            f'if [ "{self.no_bus}" = "True" ]; then\n'
            '  echo "Failed to connect to bus: No medium found"; exit 1\n'
            'fi\n'
            f'if [ "{self.mask_fails}" = "True" ] && [ "$1" = "mask" ]; then\n'
            '  echo "Cannot create mask symlink: Read-only file system"; exit 1\n'
            'fi\n'
            'verb="$1"; shift\n'
            'now=0\n'
            'args=""\n'
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    --now) now=1 ;;\n'
            '    --ignore-dependencies) ;;\n'
            '    *) args="$args $a" ;;\n'
            '  esac\n'
            'done\n'
            f'if [ "$verb" = "mask" ] && [ "$now" = "1" ] && [ "{self.now_supported}" != "True" ]; then\n'
            '  echo "Unknown option --now"; exit 1\n'
            'fi\n'
            'set_state() { # unit field value\n'
            '  python3 - "$S" "$1" "$2" "$3" <<PY\n'
            'import json,sys\n'
            'p,u,f,v=sys.argv[1:5]\n'
            'd=json.load(open(p)); d.setdefault(u,{})[f]=v\n'
            'json.dump(d,open(p,"w"))\n'
            'PY\n'
            '}\n'
            'get_field() {\n'
            '  python3 - "$S" "$1" "$2" <<PY\n'
            'import json,sys\n'
            'p,u,f=sys.argv[1:4]\n'
            'd=json.load(open(p))\n'
            'print(d.get(u,{}).get(f,"not-found"))\n'
            'PY\n'
            '}\n'
            'case "$verb" in\n'
            '  mask)\n'
            '    for u in $args; do ln -sf /dev/null "$M/$u"\n'
            '      set_state "$u" enabled masked\n'
            '      [ "$now" = "1" ] && set_state "$u" active inactive; done; exit 0 ;;\n'
            '  unmask)\n'
            '    for u in $args; do [ -L "$M/$u" ] && rm -f "$M/$u"\n'
            '      set_state "$u" enabled static; done; exit 0 ;;\n'
            '  stop)\n'
            '    for u in $args; do set_state "$u" active inactive; done; exit 0 ;;\n'
            '  is-enabled)\n'
            '    rc=0\n'
            '    for u in $args; do\n'
            '      if [ -L "$M/$u" ]; then\n'
            '        t=$(readlink "$M/$u")\n'
            '        if [ "$t" = "/dev/null" ]; then echo masked; rc=1; continue; fi\n'
            '      fi\n'
            '      w=$(get_field "$u" enabled); echo "$w"\n'
            '      case "$w" in not-found) rc=4 ;; esac\n'
            '    done; exit $rc ;;\n'
            '  is-active)\n'
            '    for u in $args; do w=$(get_field "$u" active); echo "$w"\n'
            '      case "$w" in active|activating) exit 0 ;; esac; done; exit 4 ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n'
            'exit 0\n')

    def transport(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["TMPDIR"] = str(self.root)
        # The user-session env, exactly as a PAM ssh session provides it.
        env["XDG_RUNTIME_DIR"] = "/run/user/501"
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/501/bus"
        env["HOME"] = str(self.root / "home")

        def run(command, timeout):
            proc = subprocess.run(["bash", "-s"], input=command, capture_output=True,
                                  text=True, env=env, timeout=90)
            return {"ok": proc.returncode == 0, "stdout": proc.stdout,
                    "stderr": proc.stderr, "code": proc.returncode}
        return run

    def links(self):
        return {p.name: os.readlink(p) for p in self.mask_dir.iterdir()
                if p.is_symlink()}

    def systemctl_calls(self):
        return self.calls.read_text()


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
needs_python3 = pytest.mark.skipif(not shutil.which("python3"),
                                   reason="fake systemctl uses python3")


@needs_bash
@needs_python3
def test_real_script_masks_both_units_and_verifies(tmp_path):
    """End-to-end: the generated command, run under bash against the emulated
    user manager. Mask links must exist at the real relative path, and the
    verdict must come from the node's own post-write answers."""
    node = FakeNode(tmp_path)
    r = nm.mask_node(NODE, ssh_cmd=node.transport(),
                     paths=nm.UnitPaths(mask_dir=str(node.mask_dir)))
    assert r["outcome"] == nm.OK_APPLIED, r["message"]
    assert node.links() == {SVC: "/dev/null", TIMER: "/dev/null"}
    assert r["state"]["states"] == ["masked", "masked"]
    assert r["state"]["active"] == ["inactive", "inactive"]
    assert f"mask --now {SVC}" in node.systemctl_calls()


@needs_bash
@needs_python3
def test_real_script_second_run_writes_nothing(tmp_path):
    node = FakeNode(tmp_path)
    paths = nm.UnitPaths(mask_dir=str(node.mask_dir))
    first = nm.mask_node(NODE, ssh_cmd=node.transport(), paths=paths)
    assert first["outcome"] == nm.OK_APPLIED
    mutating = re.compile(r"^(mask|unmask|stop|start|restart|daemon-reload)")
    before = [ln for ln in node.systemctl_calls().splitlines() if mutating.match(ln)]
    second = nm.mask_node(NODE, ssh_cmd=node.transport(), paths=paths)
    assert second["outcome"] == nm.OK_UNCHANGED, second["message"]
    assert "already masked" in second["message"]
    after = [ln for ln in node.systemctl_calls().splitlines() if mutating.match(ln)]
    assert after == before, "idempotence = zero new MUTATING systemctl calls"


@needs_bash
@needs_python3
def test_real_script_falls_back_when_mask_now_is_unsupported(tmp_path):
    """systemd without `mask --now` must still end up masked AND stopped — the
    whole point is to stop a loop that is running now."""
    node = FakeNode(tmp_path, now_supported=False)
    r = nm.mask_node(NODE, ssh_cmd=node.transport(),
                     paths=nm.UnitPaths(mask_dir=str(node.mask_dir)))
    assert r["outcome"] == nm.OK_APPLIED, r["message"]
    calls = node.systemctl_calls()
    assert f"mask {SVC}" in calls
    assert f"stop --ignore-dependencies {SVC}" in calls
    assert node.links() == {SVC: "/dev/null", TIMER: "/dev/null"}


@needs_bash
@needs_python3
def test_real_script_reports_a_mask_command_failure_as_an_error(tmp_path):
    node = FakeNode(tmp_path, mask_fails=True)
    r = nm.mask_node(NODE, ssh_cmd=node.transport(),
                     paths=nm.UnitPaths(mask_dir=str(node.mask_dir)))
    assert r["outcome"] == nm.ERROR
    assert "Read-only file system" in r["message"] or "mask" in r["message"]
    assert node.links() == {}, "a failed mask must leave no partial state behind"


@needs_bash
@needs_python3
def test_real_script_no_bus_is_an_error_naming_the_bus(tmp_path):
    """THE pitfall, proven by running the real script: no session bus must yield
    an env-fault error, never a 'not masked' data fact and never a write."""
    node = FakeNode(tmp_path, no_bus=True)
    paths = nm.UnitPaths(mask_dir=str(node.mask_dir))
    st = nm.probe_node(NODE, ssh_cmd=node.transport(), paths=paths)
    assert st["outcome"] == nm.ERROR
    assert "UNVERIFIABLE" in st["message"]
    assert "No medium found" in st["message"]
    assert node.links() == {}, "a node we cannot talk to must not be written to"
    r = nm.mask_node(NODE, ssh_cmd=node.transport(), paths=paths)
    assert r["outcome"] == nm.ERROR and node.links() == {}


@needs_bash
@needs_python3
def test_real_script_refuses_a_foreign_override(tmp_path):
    node = FakeNode(tmp_path, existing_link={SVC: str(tmp_path / "someone-else")})
    r = nm.mask_node(NODE, ssh_cmd=node.transport(),
                     paths=nm.UnitPaths(mask_dir=str(node.mask_dir)))
    assert r["outcome"] == nm.DRIFT, r["message"]
    assert os.readlink(node.mask_dir / SVC) == str(tmp_path / "someone-else"), \
        "the foreign link must be byte-for-byte untouched"


@needs_bash
@needs_python3
def test_real_script_unmask_removes_only_our_links(tmp_path):
    node = FakeNode(tmp_path)
    paths = nm.UnitPaths(mask_dir=str(node.mask_dir))
    assert nm.mask_node(NODE, ssh_cmd=node.transport(),
                        paths=paths)["outcome"] == nm.OK_APPLIED
    # An operator's own override, next to ours: --unmask must leave it alone.
    (node.mask_dir / "operator-owned.service").symlink_to(str(tmp_path / "x"))
    r = nm.unmask_node(NODE, ssh_cmd=node.transport(), paths=paths)
    assert r["outcome"] == nm.OK_APPLIED, r["message"]
    assert node.links() == {"operator-owned.service": str(tmp_path / "x")}


@needs_bash
@needs_python3
def test_real_script_state_mode_reads_pre_mask_state(tmp_path):
    node = FakeNode(tmp_path)
    st = nm.probe_node(NODE, ssh_cmd=node.transport(),
                       paths=nm.UnitPaths(mask_dir=str(node.mask_dir)))
    assert st["masked"] is False and st["outcome"] == nm.ERROR
    assert st["state"]["states"] == ["static", "enabled"], \
        "the fake's initial state, read exactly as the node reports it"
    assert "NOT MASKED" in st["message"]
    # Behavioural proof of the read-only guarantee the doctor check relies on:
    # the SHARED script contains mask/stop/daemon-reload arms, so only running
    # it under bash proves state mode never reaches them. A textual assertion
    # on the script could not (the strings are in there by design, MODE-gated).
    mutating = re.compile(r"^(mask|unmask|stop|start|restart|daemon-reload)\b")
    offenders = [ln for ln in node.systemctl_calls().splitlines()
                 if mutating.match(ln)]
    assert offenders == [], f"state mode must not mutate, ran: {offenders}"
