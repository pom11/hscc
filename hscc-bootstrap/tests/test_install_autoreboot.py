"""Fabricated-node fixtures for install_autoreboot (card t_1fc3fe83).

Every assertion runs against an in-memory FakeNode behind a stubbed ``_ssh``
(the sparkrun bridge seam) — no real network, no real ssh, no real cluster.
The fake implements exactly the surfaces the module's remote scripts use:
``/proc/sys`` knob existence + values, the drop-in file, and what
``sysctl --system`` does (file content becomes the live value).

All hosts are documented placeholders (10.0.0.x).
"""

import base64
import json

import install_autoreboot as m


def b64_of(content: str) -> str:
    return base64.b64encode(content.encode()).decode()


class FakeNode:
    """One fabricated node: knob presence, live values, the drop-in file.

    ``apply_on_write``: True -> a successful write runs sysctl --system (file
    values become live), like a real kernel. Set False to fabricate the
    read-back mismatch (file lands but the value does not stick).
    """

    def __init__(self, *, has_hardlockup=True, knobs=None, file=None,
                 apply_on_write=True, sudo_ok=True, reachable=True):
        self.has_hardlockup = has_hardlockup
        self.file = file  # None = absent
        self.apply_on_write = apply_on_write
        self.sudo_ok = sudo_ok
        self.reachable = reachable
        self.calls = []          # every command received, in order
        defaults = {
            "kernel.panic": "0",
            "kernel.panic_on_oops": "0",
            "kernel.softlockup_panic": "0",
            "kernel.hung_task_panic": "0",
            "kernel.hung_task_timeout_secs": "120",
            "kernel.hardlockup_panic": "0",
        }
        self.knobs = dict(defaults)
        if knobs:
            self.knobs.update(knobs)
        if not has_hardlockup:
            self.knobs.pop("kernel.hardlockup_panic", None)

    @property
    def present_keys(self):
        return set(self.knobs)

    def ssh(self, host, command, timeout=60):
        assert host, "host must be passed through"
        self.calls.append(command)
        if not self.reachable:
            return {"ok": False, "stdout": "", "stderr": "ssh: connect to host timed out",
                    "code": 255}
        if command.startswith("for k in"):
            return self._probe()
        if "base64 -d" in command:
            return self._write(command)
        if "sysctl --system" in command:
            return self._reapply()
        return {"ok": False, "stdout": "", "stderr": f"unexpected command: {command[:80]}",
                "code": 1}

    # ── the fabricated remote shell ────────────────────────────────────────
    def _probe(self):
        lines = []
        for key in m.ALL_KNOBS:
            if key in self.knobs:
                lines.append(f"KNOB {key}")
                lines.append(f"VAL {key} {self.knobs[key]}")
        lines.append("---FILE---")
        out = "\n".join(lines)
        if self.file is not None:
            out += "\n" + self.file
        return {"ok": True, "stdout": out, "stderr": "", "code": 0}

    def _write(self, command):
        if not self.sudo_ok:
            return {"ok": False, "stdout": "",
                    "stderr": "sudo: a password is required", "code": 1}
        payload = command.split("<<'HSCC_B64'\n", 1)[1].split("\nHSCC_B64", 1)[0]
        content = base64.b64decode(payload.strip()).decode()
        self.file = content
        if self.apply_on_write:
            self._apply_file()
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}

    def _reapply(self):
        if not self.sudo_ok:
            return {"ok": False, "stdout": "",
                    "stderr": "sudo: a password is required", "code": 1}
        self._apply_file()
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}

    def _apply_file(self):
        for line in (self.file or "").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k in self.knobs:
                self.knobs[k] = v


def write_call_count(node: FakeNode) -> int:
    return sum(1 for c in node.calls if "base64 -d" in c)


def sysctl_call_count(node: FakeNode) -> int:
    return sum(1 for c in node.calls if "sysctl --system" in c)


# ── drop-in content (exact) ────────────────────────────────────────────────

def test_dropin_content_exact_with_hardlockup():
    content = m.build_dropin(True)
    lines = content.splitlines()
    assert lines[0].startswith("#")
    assert "kernel.panic = 10" in lines
    assert "kernel.panic_on_oops = 1" in lines
    assert "kernel.softlockup_panic = 1" in lines
    assert "kernel.hung_task_panic = 1" in lines
    assert "kernel.hung_task_timeout_secs = 600" in lines
    assert "kernel.hardlockup_panic = 1" in lines
    # File order is the declared order — the doctor card pins the same order.
    order = [l.split(" =")[0] for l in lines if " = " in l]
    assert order == list(m.MANAGED_KEYS) + [m.HARDLOCKUP_KEY]


def test_dropin_content_exact_without_hardlockup():
    content = m.build_dropin(False)
    assert "hardlockup" not in content
    assert len([l for l in content.splitlines() if " = " in l]) == len(m.MANAGED_KEYS)


def test_hardlockup_is_conditional_constant():
    # The doctor card reuses this exact split — pin it against silent edits.
    assert m.HARDLOCKUP_KEY == "kernel.hardlockup_panic"
    assert m.HARDLOCKUP_KEY not in m.MANAGED_KEYS
    assert m.MANAGED_KEYS["kernel.hung_task_timeout_secs"] == "600"
    assert m.MANAGED_KEYS["kernel.panic"] == "10"


# ── install path ───────────────────────────────────────────────────────────

def test_fresh_node_installed_and_verified():
    n = FakeNode()
    r = m.install_node("10.0.0.1", _ssh=n.ssh)
    assert r["action"] == "installed"
    assert r["error"] is None
    assert n.file == m.build_dropin(True)
    # read-back: every managed knob now live at the desired value
    for k, v in m.MANAGED_KEYS.items():
        assert n.knobs[k] == v, k
    assert n.knobs[m.HARDLOCKUP_KEY] == "1"
    # probe -> write -> read-back probe
    assert len(n.calls) == 3


def test_conditional_knob_absent_skips_key_not_node():
    n = FakeNode(has_hardlockup=False)
    r = m.install_node("10.0.0.2", _ssh=n.ssh)
    assert r["action"] == "installed"
    assert r["skipped_keys"] == [m.HARDLOCKUP_KEY]
    assert n.file is not None
    assert "hardlockup" not in n.file
    # wanted set excluded it: its absence must not appear in changed_keys
    assert m.HARDLOCKUP_KEY not in r["changed_keys"]


def test_readback_mismatch_fails_node_loudly():
    n = FakeNode(apply_on_write=False)  # file lands, value never sticks
    r = m.install_node("10.0.0.3", _ssh=n.ssh)
    assert r["action"] == "failed"
    assert "read-back mismatch" in r["error"]
    # names the offending key(s) with expected vs actual
    assert "kernel.panic" in r["error"] and "expected '10'" in r["error"]


def test_idempotent_second_run_is_noop():
    n = FakeNode()
    first = m.install_node("10.0.0.1", _ssh=n.ssh)
    assert first["action"] == "installed"
    before_calls = len(n.calls)
    second = m.install_node("10.0.0.1", _ssh=n.ssh)
    assert second["action"] == "unchanged"
    # probe only — no writes, no sysctl --system
    assert len(n.calls) == before_calls + 1
    assert write_call_count(n) == 1
    assert sysctl_call_count(n) == 1


def test_drift_operator_edited_file_reported_never_clobbered():
    edited = m.build_dropin(True).replace("kernel.panic = 10", "kernel.panic = 0")
    n = FakeNode(file=edited, knobs={"kernel.panic": "0"})
    r = m.install_node("10.0.0.4", _ssh=n.ssh)
    assert r["action"] == "drift"
    assert r["error"] is None
    assert n.file == edited, "operator file was clobbered"
    assert write_call_count(n) == 0
    assert sysctl_call_count(n) == 0, "drift node must not be sysctl'd either"
    assert "left untouched" in r["note"]


def test_opt_out_skips_without_touching_node():
    n = FakeNode()
    r = m.install_node("10.0.0.5", _ssh=n.ssh, enabled=False)
    assert r["action"] == "skipped-disabled"
    assert n.calls == []


def test_dry_run_reports_diff_writes_nothing():
    n = FakeNode()
    r = m.install_node("10.0.0.6", _ssh=n.ssh, dry_run=True)
    assert r["action"] == "dry-run"
    assert n.file is None
    assert write_call_count(n) == 0
    assert sysctl_call_count(n) == 0
    assert "kernel.panic = 10" in r["diff"]
    assert "kernel.hung_task_timeout_secs = 600" in r["diff"]


def test_dry_run_shows_live_drift_on_matching_file():
    n = FakeNode(file=m.build_dropin(True), knobs={"kernel.panic": "0"})
    r = m.install_node("10.0.0.6", _ssh=n.ssh, dry_run=True)
    assert r["action"] == "dry-run"
    assert "kernel.panic" in r["diff"]


def test_file_correct_live_drifted_reapplies_without_rewrite():
    n = FakeNode(file=m.build_dropin(True), knobs={"kernel.softlockup_panic": "0"})
    r = m.install_node("10.0.0.7", _ssh=n.ssh)
    assert r["action"] == "live-reapplied"
    assert write_call_count(n) == 0, "file already correct — must not rewrite"
    assert sysctl_call_count(n) >= 1
    assert n.knobs["kernel.softlockup_panic"] == "1"


def test_sudo_refused_fails_node_clearly_not_hang():
    n = FakeNode(sudo_ok=False)
    r = m.install_node("10.0.0.8", _ssh=n.ssh)
    assert r["action"] == "failed"
    assert "sudo -n was refused" in r["error"]


def test_unreachable_node_fails_not_fabricated_ok():
    n = FakeNode(reachable=False)
    r = m.install_node("10.0.0.9", _ssh=n.ssh)
    assert r["action"] == "failed"
    assert "probe failed" in r["error"]


# ── remote script shapes ───────────────────────────────────────────────────

def test_probe_command_uses_proc_sys_and_always_exits_zero():
    cmd = m.probe_command()
    assert "---FILE---" in cmd
    # The key list + the /proc/sys path are the probe surface (the dotted ->
    # slash rewrite happens on-node via tr, exactly what sysctl(8) reads).
    assert "/proc/sys/" in cmd
    for key in m.ALL_KNOBS:
        assert key in cmd
    # `[ -f ] && cat` exits 1 when the file is absent — the probe must not
    # render the normal fresh-node state as a transport failure.
    assert "exit 0" in cmd
    assert '] && cat' not in cmd


def test_write_command_is_atomic_root_via_sudo_n():
    cmd = m.write_command("kernel.panic = 10\n")
    assert "set -euo pipefail" in cmd
    assert "base64 -d" in cmd
    assert "sudo -n install" in cmd
    assert "sudo -n mv -f" in cmd
    assert "sudo -n sysctl --system" in cmd
    # every privileged step is non-interactive
    for line in cmd.splitlines():
        if line.startswith(("chmod", "mv ", "sysctl ", "install ")):
            raise AssertionError(f"privilege escalation missing: {line}")
    # final path only ever reached by the same-dir rename
    stage = cmd.split("sudo -n install")[1]
    assert "mv -f" in stage


def test_parse_probe_roundtrips_absent_and_present_file():
    absent = "KNOB kernel.panic\nVAL kernel.panic 10\n---FILE---\n"
    parsed = m.parse_probe(absent)
    assert parsed["file"] is None
    assert parsed["knobs"] == {"kernel.panic": "10"}
    present = absent + "# x\nkernel.panic = 10\n"
    assert m.parse_probe(present)["file"] == "# x\nkernel.panic = 10\n"


# ── opt-out reader ─────────────────────────────────────────────────────────

def test_optout_reader_default_on_when_absent():
    assert m.autoreboot_enabled({}) is True
    assert m.autoreboot_enabled({"node_recovery": {}}) is True
    assert m.autoreboot_enabled({"node_recovery": {"watchdog": True}}) is True


def test_optout_reader_explicit_false():
    assert m.autoreboot_enabled({"node_recovery": {"autoreboot": False}}) is False
    assert m.autoreboot_enabled({"node_recovery": {"autoreboot": True}}) is True


def test_optout_reader_unknown_keys_ignored_and_non_dict_safe():
    assert m.autoreboot_enabled(
        {"node_recovery": {"autoreboot": True, "future_layer": {"a": 1}}}) is True
    assert m.autoreboot_enabled(None) is True
    assert m.autoreboot_enabled("nonsense") is True
    assert m.autoreboot_enabled({"node_recovery": "nonsense"}) is True


def test_read_cluster_config_missing_file_defaults_empty(tmp_path):
    assert m.read_cluster_config(str(tmp_path)) == {}
    (tmp_path / "cluster.json").write_text("{ not json")
    assert m.read_cluster_config(str(tmp_path)) == {}
    (tmp_path / "cluster.json").write_text(json.dumps(
        {"node_recovery": {"autoreboot": False}}))
    assert m.autoreboot_enabled(m.read_cluster_config(str(tmp_path))) is False


# ── fleet fan-out ──────────────────────────────────────────────────────────

def test_fleet_one_bad_node_does_not_suppress_the_others():
    good, bad = FakeNode(), FakeNode(apply_on_write=False)
    nodes = {"10.0.0.1": good, "10.0.0.2": bad}

    def ssh(host, command, timeout=60):
        return nodes[host].ssh(host, command, timeout)

    out = m.install_fleet(["10.0.0.1", "10.0.0.2"], _ssh=ssh,
                          _config_loader=lambda _d: {})
    assert out["ok"] is False
    by_host = {r["host"]: r for r in out["results"]}
    assert by_host["10.0.0.1"]["action"] == "installed"
    assert by_host["10.0.0.2"]["action"] == "failed"


def test_fleet_optout_touches_no_nodes():
    n = FakeNode()
    out = m.install_fleet(["10.0.0.1"], _ssh=n.ssh,
                          _config_loader=lambda _d: {"node_recovery": {"autoreboot": False}})
    assert out["ok"] is True and out["skipped_reason"]
    assert n.calls == []


# ── main() CLI contract used by bootstrap.sh ───────────────────────────────

def test_main_reports_json_and_rc(monkeypatch, capsys):
    n = FakeNode()
    monkeypatch.setattr(m, "_default_ssh", n.ssh)
    monkeypatch.setattr(m, "read_cluster_config", lambda _d=None: {})
    rc = m.main(["--hosts", "10.0.0.1"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["results"][0]["action"] == "installed"


def test_main_dry_run_writes_nothing(monkeypatch, capsys):
    n = FakeNode()
    monkeypatch.setattr(m, "_default_ssh", n.ssh)
    monkeypatch.setattr(m, "read_cluster_config", lambda _d=None: {})
    rc = m.main(["--hosts", "10.0.0.1", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["results"][0]["action"] == "dry-run"
    assert n.file is None and write_call_count(n) == 0


def test_main_no_hosts_is_a_loud_rc2(monkeypatch, capsys):
    monkeypatch.setattr(m, "_hosts_from_detect", lambda: [])
    rc = m.main([])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2 and payload["ok"] is False


def test_main_failing_node_is_rc1(monkeypatch, capsys):
    n = FakeNode(reachable=False)
    monkeypatch.setattr(m, "_default_ssh", n.ssh)
    monkeypatch.setattr(m, "read_cluster_config", lambda _d=None: {})
    rc = m.main(["--hosts", "10.0.0.1"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
