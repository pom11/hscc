"""GPU clock cap node-recovery layer (install_gpu_clock_cap.py).

Two layers:

  * fabricated-transport tests — a scripted ``ssh_cmd`` responder, asserting
    the decisions (capped / not capped / unreachable / opt-out / bad config /
    dry-run) and that "3 of 4 capped" is not ok;
  * a bash-backed fake node — runs the REAL generated node-side script with
    ``sudo`` / ``systemctl`` / ``nvidia-smi`` shimmed into a sandbox, so a script
    that writes the unit but never applies the lock fails here.

No test opens a socket or contacts a node; addresses are 10.0.0.x placeholders.
"""

import json
import os
import shutil
import subprocess

import pytest

import install_gpu_clock_cap as gc

NODE = "10.0.0.1"
OTHER = "10.0.0.2"


def _state_out(present=True, cap=2200, enabled="enabled", active="active", clk=2184,
               action=None, rc=0):
    lines = []
    if action:
        lines.append(f"HSCC_ACTION: {action}")
    lines.append(f"HSCC_UNIT_PRESENT: {1 if present else 0}")
    if present:
        lines.append(f"HSCC_UNIT_CAP: {cap}")
    lines += [f"HSCC_UNIT_ENABLED: {enabled}", f"HSCC_UNIT_ACTIVE: {active}",
              f"HSCC_SM_CLOCK: {clk}", "HSCC_MAX_SM: 3003", f"HSCC_RC: {rc}"]
    return {"ok": rc == 0, "stdout": "\n".join(lines), "stderr": "", "code": rc}


def _responder(by_mode):
    """ssh_cmd fake: picks the reply by which mode the command carries."""
    calls = []

    def ssh(cmd, _timeout):
        import base64
        script = base64.b64decode(cmd.split("'")[3]).decode()
        mode = next(m for m in ("state", "install", "uninstall") if f'MODE="{m}"' in script)
        calls.append(mode)
        return by_mode[mode]
    ssh.calls = calls
    return ssh


# ── config ──────────────────────────────────────────────────────────────────

def _cfg(tmp_path, block):
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps({"node_recovery": block}))
    return str(p)


def test_config_default_when_absent(tmp_path):
    c = gc.clock_cap_config(str(tmp_path / "missing.json"))
    assert c == {"enabled": True, "cap_mhz": 2200, "config_error": ""}


def test_config_custom_value_and_opt_out(tmp_path):
    assert gc.clock_cap_config(_cfg(tmp_path, {"gpu_clock_cap_mhz": 1900}))["cap_mhz"] == 1900
    assert gc.clock_cap_config(_cfg(tmp_path, {"gpu_clock_cap_mhz": False}))["enabled"] is False


@pytest.mark.parametrize("bad", ["2200", 500, 9999, True, 2200.5])
def test_config_bad_value_keeps_default_and_says_so(tmp_path, bad):
    c = gc.clock_cap_config(_cfg(tmp_path, {"gpu_clock_cap_mhz": bad}))
    assert c["enabled"] is True and c["cap_mhz"] == 2200
    assert "gpu_clock_cap_mhz" in c["config_error"]


def test_sibling_subkeys_do_not_flip_this_layer(tmp_path):
    c = gc.clock_cap_config(_cfg(tmp_path, {"watchdog": False, "firmware_notifier_mask": False}))
    assert c["enabled"] is True


# ── verdicts ────────────────────────────────────────────────────────────────

def test_capped_node_is_ok():
    r = gc.probe_node(NODE, 2200, ssh_cmd=_responder({"state": _state_out()}))
    assert r["capped"] and r["outcome"] == gc.OK_UNCHANGED


@pytest.mark.parametrize("kw,needle", [
    ({"present": False}, "missing"),
    ({"cap": 2400}, "want 2200"),
    ({"enabled": "disabled"}, "is-enabled=disabled"),
    ({"active": "inactive"}, "is-active=inactive"),
    ({"clk": 2476}, "above the 2200 MHz cap"),
])
def test_each_missing_condition_is_not_capped(kw, needle):
    r = gc.probe_node(NODE, 2200, ssh_cmd=_responder({"state": _state_out(**kw)}))
    assert not r["capped"] and needle in r["message"]


def test_unreachable_is_error_not_capped():
    dead = {"ok": False, "stdout": "", "stderr": "Host is down", "code": 255}
    r = gc.probe_node(NODE, 2200, ssh_cmd=_responder({"state": dead}))
    assert r["outcome"] == gc.ERROR and "UNVERIFIABLE" in r["message"] and "Host is down" in r["message"]


def test_install_applies_and_reads_back():
    ssh = _responder({"install": _state_out(action="applied")})
    r = gc.cap_node(NODE, 2200, ssh_cmd=ssh)
    assert r["outcome"] == gc.OK_APPLIED and ssh.calls == ["install"]


def test_install_that_does_not_take_is_error():
    ssh = _responder({"install": _state_out(action="applied", clk=2476)})
    r = gc.cap_node(NODE, 2200, ssh_cmd=ssh)
    assert r["outcome"] == gc.ERROR and "read-back" in r["message"]


def test_sudo_refusal_is_named():
    out = {"ok": False, "stdout": "HSCC_MSG: sudo -n install refused: a password is required\nHSCC_RC: 5",
           "stderr": "", "code": 5}
    r = gc.cap_node(NODE, 2200, ssh_cmd=_responder({"install": out}))
    assert r["outcome"] == gc.ERROR and "sudo -n" in r["message"]


def test_dry_run_only_reads():
    ssh = _responder({"state": _state_out(present=False, enabled="", active="", clk=2476)})
    r = gc.cap_node(NODE, 2200, ssh_cmd=ssh, dry_run=True)
    assert ssh.calls == ["state"] and "would install" in r["message"]


def test_fleet_partial_is_not_ok(tmp_path):
    replies = {NODE: _responder({"install": _state_out(action="applied")}),
               OTHER: _responder({"install": {"ok": False, "stdout": "", "stderr": "down", "code": 255}})}
    s = gc.cap_fleet([NODE, OTHER], ssh_factory=replies.__getitem__,
                     config_path=str(tmp_path / "none.json"))
    assert not s["ok"] and s["capped"] == [NODE]
    assert gc.bootstrap_lines(s)[0].startswith("WARN|")


def test_fleet_opt_out_touches_nothing(tmp_path):
    ssh = _responder({})
    s = gc.cap_fleet([NODE], ssh_factory=lambda _h: ssh,
                     config_path=_cfg(tmp_path, {"gpu_clock_cap_mhz": False}))
    assert s["ok"] and ssh.calls == [] and s["nodes"][0]["outcome"] == gc.SKIP_DISABLED


def test_doctor_check_shape(tmp_path):
    good = _responder({"state": _state_out()})
    res = gc.doctor_check([NODE], ssh_factory=lambda _h: good,
                          config_path=str(tmp_path / "none.json"))
    assert set(res) == {"name", "ok", "detail", "fix", "fatal"}
    assert res["ok"] and res["fatal"] is False and "1/1" in res["detail"]


def test_doctor_default_transport_refused_in_pytest(tmp_path):
    res = gc.doctor_check([NODE], config_path=str(tmp_path / "none.json"))
    assert not res["ok"] and "refused inside a pytest" in res["detail"]


def test_parse_hosts_splits_bootstrap_csv():
    assert gc.parse_hosts(["--hosts", "10.0.0.1,10.0.0.2", "--bootstrap-lines"]) == [NODE, OTHER]
    assert gc.parse_hosts(["--hosts", "10.0.0.1", "10.0.0.2"]) == [NODE, OTHER]


def test_notifier_mask_cli_splits_bootstrap_csv(monkeypatch):
    """Regression: bootstrap passes ONE comma-joined --hosts arg to the notifier stage."""
    import install_notifier_mask as nm
    seen = {}

    def fake_fleet(nodes, **_kw):
        seen["nodes"] = nodes
        return {"ok": True, "dry_run": True, "enabled": True, "config_error": "",
                "target": {"units": [], "mask_dir": "", "snap_untouched": ""},
                "nodes": [], "masked": [], "opted_out": [], "needing_attention": []}
    monkeypatch.setattr(nm, "mask_fleet", fake_fleet)
    nm.main(["--hosts", "10.0.0.1,10.0.0.2", "--dry-run", "--json"])
    assert seen["nodes"] == [NODE, OTHER]


def test_unit_text_is_a_boot_persistent_oneshot():
    u = gc.unit_text(2200)
    assert "ExecStart=/usr/bin/nvidia-smi -lgc 0,2200" in u
    assert "ExecStop=/usr/bin/nvidia-smi -rgc" in u
    assert "RemainAfterExit=yes" in u and "WantedBy=multi-user.target" in u


# ── bash-backed fake node: the REAL script against shimmed tools ────────────

pytestmark_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class FakeNode:
    """Sandbox with shims: sudo (runs the command; `install` → cp),
    systemctl (enabled/active in files; restart applies the unit's -lgc cap;
    disable --now runs -rgc), and a fake nvidia-smi holding the clock."""

    def __init__(self, root):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.unit = root / "etc" / gc.UNIT_NAME
        self.unit.parent.mkdir()
        self.smi = root / "nvidia-smi"
        (root / "clock").write_text("2476")
        self._shim("sudo", r'''[ "$1" = -n ] && shift
if [ "$1" = install ]; then shift; while [ $# -gt 2 ]; do shift; done; cp "$1" "$2"; else "$@"; fi''')
        self._shim("systemctl", rf'''
S="{root}"; U="{self.unit}"
case "$1" in
  is-enabled) cat "$S/enabled" 2>/dev/null || echo disabled ;;
  is-active)  cat "$S/active" 2>/dev/null || echo inactive ;;
  daemon-reload) : ;;
  enable) echo enabled > "$S/enabled" ;;
  restart) c=$(sed -n 's/^ExecStart=.* -lgc 0,\([0-9]*\).*/\1/p' "$U"); echo $((c - 16)) > "$S/clock"; echo active > "$S/active" ;;
  disable) echo disabled > "$S/enabled"; echo inactive > "$S/active"; echo 2476 > "$S/clock" ;;
esac''')
        self.smi.write_text(rf'''#!/usr/bin/env bash
case "$*" in
  *clocks.max.sm*) echo 3003 ;;
  *clocks.sm*) cat "{root}/clock" ;;
  -rgc) echo 2476 > "{root}/clock" ;;
esac''')
        self.smi.chmod(0o755)

    def _shim(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body + "\n")
        p.chmod(0o755)

    def run(self, mode, cap=2200):
        script = gc.install_script(mode, cap, unit_path=str(self.unit), smi=str(self.smi))
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        p = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True,
                           env=env, timeout=30)
        return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr,
                "code": p.returncode}


@pytestmark_bash
def test_real_script_install_then_idempotent_then_uninstall(tmp_path):
    node = FakeNode(tmp_path)
    first = node.run("install")
    lines = gc._hscc_lines(first["stdout"])
    assert lines["HSCC_ACTION"] == "applied", first
    st = gc._state_from(lines)
    assert gc.cap_problems(st, 2200) == [], st
    assert node.unit.read_text() == gc.unit_text(2200)

    again = gc._hscc_lines(node.run("install")["stdout"])
    assert again["HSCC_ACTION"] == "unchanged"

    changed = gc._hscc_lines(node.run("install", cap=1900)["stdout"])
    assert changed["HSCC_ACTION"] == "applied" and changed["HSCC_UNIT_CAP"] == "1900"

    gone = gc._hscc_lines(node.run("uninstall")["stdout"])
    assert gone["HSCC_ACTION"] == "removed" and gone["HSCC_UNIT_PRESENT"] == "0"
    assert gone["HSCC_SM_CLOCK"] == "2476"


@pytestmark_bash
def test_real_script_without_driver_fails_honestly(tmp_path):
    node = FakeNode(tmp_path)
    node.smi.unlink()
    out = node.run("state")
    lines = gc._hscc_lines(out["stdout"])
    assert lines["HSCC_RC"] == "3" and "not found" in lines["HSCC_MSG"]
