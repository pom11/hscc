"""The systemd --user unit-name migration (card t_d8f13693).

Two layers, each proving what the other cannot:

  * fabricated-transport tests — a scripted responder for ``ssh_cmd``, asserting
    the *decisions* (migrate / already-canonical / dry-run / no-bus fault /
    enable-failed / retire-failed / twin / orphan) and, above all, the ORDER in
    which the node is asked to do things;
  * a bash-backed fake node (``FakeNode``) — runs the REAL generated node-side
    script against a stateful ``systemctl --user`` shim and the REAL emitter.
    The emulation is the honest part: ``enable`` only succeeds for a unit file
    with a real ``[Install]``/``WantedBy=`` section, and ``is-enabled`` answers
    ``enabled`` only because of the ``default.target.wants`` symlink — so a
    script that wrote a file but never enabled it, or retired the old unit
    before the new one was supervised, fails HERE rather than looking green in
    Python.

The card's two hard rules, pinned everywhere:

  * **never zero, never two.** No ordering of stubbed answers lets a node end
    with both units active, or with none, and the client still report success —
    the final arbiter is the node's own re-read, not the verbs' exit codes.
  * **an environment fault is never a data fact.** A node whose state could not
    be read (no systemctl, no bus, a word systemctl never prints) is a FAULT and
    nothing is written on it — never "no legacy unit, all clean".

No test here opens a socket, resolves a host, or contacts a node; the production
transport (:func:`migrate_systemd_unit_name._default_ssh`) is never called. Node
addresses are the documented 10.0.0.x placeholders.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import migrate_systemd_unit_name as m

REPO = str(Path(m.__file__).resolve().parents[1])
NODE = "10.0.0.1"
OTHER = "10.0.0.2"
NEW = m.UNIT_NAME
OLD = m.LEGACY_UNIT_NAME


# ── how a node answers ──────────────────────────────────────────────────────

def unit_line(name, *, enabled, active, pid="0"):
    return f"HSCC_UNIT: {name}|enabled={enabled}|active={active}|mainpid={pid}"


def state_block(*, new_enabled="not-found", new_active="inactive", new_pid="0",
                old_enabled="enabled", old_active="active", old_pid="4112",
                file_new="absent", file_old="812", link_old="none"):
    return [
        unit_line(NEW, enabled=new_enabled, active=new_active, pid=new_pid),
        unit_line(OLD, enabled=old_enabled, active=old_active, pid=old_pid),
        f"HSCC_FILE_NEW: {file_new}",
        f"HSCC_FILE_OLD: {file_old}",
        "HSCC_EXEC_NEW: " + ("" if file_new == "absent"
                             else "/usr/bin/python3 -m hscc_daemon start-daemon"),
        "HSCC_EXEC_OLD: /usr/bin/python3 -m hscc_daemon start-daemon",
        f"HSCC_LINK_OLD: {link_old}",
    ]


def ok(lines):
    return {"ok": True, "stdout": "\n".join(list(lines) + ["HSCC_RC: 0"]) + "\n",
            "stderr": "", "code": 0}


def fault(rc, code, msg):
    return {"ok": False, "stdout": f"HSCC_RC: {rc}\nHSCC_CODE: {code}\nHSCC_MSG: {msg}\n",
            "stderr": "", "code": rc}


LEGACY_LIVE = state_block()                      # a node the pre-fix installer made
CANONICAL = state_block(new_enabled="enabled", new_active="active", new_pid="7001",
                        old_enabled="not-found", old_active="inactive",
                        file_new="812", file_old="absent")
ENABLED_NEW = state_block(new_enabled="enabled", new_active="active", new_pid="7001",
                          file_new="812") + ["HSCC_NEW_PID: 7001"]


class Responder:
    """A scripted node: answers each decoded mode, and records the modes asked.

    The recorded MODE sequence is the point — the tests read it out to prove
    enable-new happens before retire-old, that a dry run asks for nothing but a
    probe, and that a node which could not be read is never asked to change.

    A mode may map to ONE answer (every call answers that) or to a LIST of answers
    consumed in order, the last repeating. ``"probe": [before, after]`` is why that
    matters: a migration probes twice (before, and again to verify), and a node that
    reads as "legacy still active" on the second probe is a different node than one
    that reads as migrated — collapsing them would hide the twin rule behind a
    constant.
    """

    def __init__(self, answers: Dict[str, Any]):
        self.answers: Dict[str, Any] = {}
        self._seq: Dict[str, List[Any]] = {}
        for k, v in answers.items():
            if isinstance(v, (list, tuple)):
                assert v, f"{k}: an answer list must not be empty"
                self._seq[k] = list(v)
                self.answers[k] = v[0]
            else:
                self.answers[k] = v
        self.modes: List[str] = []

    def __call__(self, command, timeout=120) -> Dict[str, Any]:
        mode = m.mode_of_command(command)
        assert mode is not None, f"node got a command that is not a known mode: {command!r}"
        self.modes.append(mode)
        if mode not in self.answers:
            raise AssertionError(
                f"node was asked to run {mode!r} but no answer was scripted "
                f"(modes so far: {self.modes})")
        seq = self._seq.get(mode)
        if seq is not None:
            out = seq.pop(0) if len(seq) > 1 else seq[0]
        else:
            out = self.answers[mode]
        out = out(self) if callable(out) else out
        assert isinstance(out, dict), f"{mode} answer must be a dict, got {type(out)}"
        return out


# ── the node-side script is what we think it is ─────────────────────────────

@pytest.mark.parametrize("mode", ["probe", "enable-new", "retire-old"])
def test_command_shape_is_b64_piped_bash_and_carries_no_privilege(mode):
    cmd = m.command_for(mode, pkgs="/home/op/.hermes/plugins")
    assert cmd.startswith("printf '%s' '") and cmd.endswith("| base64 -d | bash -s")
    body = base64.b64decode(m._PAYLOAD.search(cmd).group(1)).decode()
    assert body == m.node_script(mode, pkgs="/home/op/.hermes/plugins")
    assert m.mode_of_command(cmd) == mode
    # a --user operation: sudo would escalate on a cluster node to change
    # something the user already owns
    assert "sudo" not in body, "must never escalate on a node"
    # lingering is the installer's job; a fleet migration must not flip it
    assert "loginctl" not in body


def test_script_uses_the_imported_names_and_retypes_neither():
    """Node and client cannot disagree about WHICH unit is migrated: the names
    arrive only by substituting unit_name.py's constants into the template."""
    body = m.node_script("probe")
    assert f'NEW="{NEW}"' in body and f'OLD="{OLD}"' in body
    assert NEW != OLD, "identical names would make the twin purge a no-op"
    rest = body.replace(NEW, "").replace(OLD, "")
    assert "com.hermes" not in rest and ".service" not in rest, (
        "a unit name typed into the template would survive a rename in "
        "unit_name.py — that is how the two installers drifted apart")


def test_unknown_mode_rejected_before_any_ssh():
    for bad in ("rm-everything", "enable-new; rm -rf /", "", "PROBE"):
        with pytest.raises(ValueError):
            m.node_script(bad)


def test_bus_bootstrap_and_word_validation_present():
    """``systemctl --user`` with no session bus answers with error text, and the
    bug class is reading that as "no unit". The script derives the bus in
    ``${VAR:-derived}`` form (the notifier-mask rule) and validates every word it
    is about to treat as state."""
    body = m.node_script("probe")
    assert "${XDG_RUNTIME_DIR:=" in body
    assert "${DBUS_SESSION_BUS_ADDRESS:=unix:path=" in body
    assert "NO_BUS" in body
    for word in ("enabled", "static", "not-found", "activating", "dormant"):
        assert word in body, f"{word} must be in the recognised-word set"


# ── probe: an unreadable node is a FAULT, never "clean" ─────────────────────

def test_probe_no_bus_is_a_fault_not_an_empty_state():
    r = m.probe_node(lambda cmd, to: fault(
        m.RC_NO_BUS, "NO_BUS",
        "systemctl --user is-enabled answered 'Failed to connect to bus' "
        "— not a unit state"))
    assert r["ok"] is False
    assert r["fault"].startswith("[NO_BUS] "), r["fault"]
    assert m.RC_NO_BUS == 20
    # the whole point of the class: this must never read as "nothing to do"
    assert m.has_legacy(r) is False and m.canonical_healthy(r) is False
    assert m.verify_final(r).startswith("no active unit")


def test_probe_rejects_words_systemd_never_prints():
    r = m.probe_node(lambda cmd, to: ok([
        unit_line(NEW, enabled="enabled", active="active", pid="7001"),
        unit_line(OLD, enabled="Fatal", active="inactive")]))
    assert r["ok"] is False and "bus/environment fault" in r["fault"]


def test_probe_rc0_without_unit_lines_is_a_fault():
    r = m.probe_node(lambda cmd, to: ok(["HSCC_FILE_NEW: 812"]))
    assert r["ok"] is False and "no unit lines" in r["fault"]


@pytest.mark.parametrize("res", [
    {"ok": False, "error": "ssh: connect to host 10.0.0.1 port 22: Connection refused"},
    {"ok": True, "stdout": "", "code": 255},
    "a transport that ignores its own contract",
])
def test_probe_transport_noise_is_a_fault(res):
    r = m.probe_node(lambda cmd, to, _r=res: _r)
    assert r["ok"] is False and r["fault"]


def test_probe_reads_state_and_doctor_can_reuse_it():
    legacy = m.probe_node(lambda cmd, to: ok(LEGACY_LIVE))
    assert legacy["ok"] and m.has_legacy(legacy) and not m.canonical_healthy(legacy)
    done = m.probe_node(lambda cmd, to: ok(CANONICAL))
    assert done["ok"] and not m.has_legacy(done) and m.canonical_healthy(done)
    # a legacy FILE with no [Install] is `static` and can still be pulled in by a
    # socket/timer; a leftover file is still the name we are retiring
    for st in (state_block(old_enabled="static", old_active="inactive", old_pid="0"),
               state_block(old_enabled="not-found", old_active="inactive",
                           old_pid="0", file_new="812", file_old="812")):
        assert m.has_legacy(m.probe_node(lambda cmd, to: ok(st)))


# ── verify_final: the card's postcondition as a predicate ───────────────────

def state(*, new_enabled="enabled", new_active="active", new_pid="7001",
          old_enabled="not-found", old_active="inactive", old_pid="0"):
    return {"units": {NEW: {"enabled": new_enabled, "active": new_active,
                            "mainpid": new_pid},
                      OLD: {"enabled": old_enabled, "active": old_active,
                            "mainpid": old_pid}}}


@pytest.mark.parametrize("st,expect", [
    (state(), ""),                                                    # exactly right
    (state(old_enabled="enabled"), "two enabled"),
    (state(old_enabled="enabled", old_active="active", old_pid="4112"), "two active"),
    (state(new_active="inactive", new_pid="0"), "no active unit"),
    (state(new_enabled="disabled", new_active="inactive", new_pid="0",
           old_enabled="enabled", old_active="active", old_pid="4112"),
     f"{OLD} is the active unit"),
    (state(new_pid="0"), "no MainPID"),
])
def test_verify_final_is_the_never_zero_never_two_rule(st, expect):
    got = m.verify_final(st)
    assert expect in got if expect else got == "", (expect, got)


# ── migrate_node: decisions ─────────────────────────────────────────────────

def test_legacy_node_migrates_with_canonical_up_before_legacy_retired():
    """Ordering IS the safety property: the canonical unit must be enabled and
    supervised BEFORE the legacy one is touched. Purging first would open a
    window with no supervisor, and a failed enable would leave the box unmanaged
    — the inverse twin, and worse for a healing daemon."""
    res = Responder({"probe": [ok(LEGACY_LIVE), ok(CANONICAL)],
                     "enable-new": ok(ENABLED_NEW), "retire-old": ok(CANONICAL)})
    r = m.migrate_node(NODE, ssh_cmd=res)

    assert r["outcome"] == m.OK_MIGRATED
    assert res.modes == ["probe", "enable-new", "retire-old", "probe"], res.modes
    assert any(a.startswith(f"enabled {NEW}") for a in r["actions"])
    assert any(a.startswith(f"retired {OLD}") for a in r["actions"])
    assert "verified: exactly one unit enabled + active" in r["actions"]
    assert r["after"]["units"][OLD]["enabled"] == "not-found"
    assert "MainPID 7001" in r["message"]


def test_already_canonical_node_is_not_touched_at_all():
    res = Responder({"probe": ok(CANONICAL)})
    r = m.migrate_node(NODE, ssh_cmd=res)

    assert r["outcome"] == m.OK_ALREADY
    assert res.modes == ["probe"], "a migrated node must not be restarted"
    assert not r["actions"]


def test_second_run_is_idempotent():
    """Re-running must not restart a healthy daemon — that would turn a routine
    re-apply into a deploy incident."""
    first = Responder({"probe": [ok(LEGACY_LIVE), ok(CANONICAL)],
                       "enable-new": ok(ENABLED_NEW), "retire-old": ok(CANONICAL)})
    assert m.migrate_node(NODE, ssh_cmd=first)["outcome"] == m.OK_MIGRATED
    second = Responder({"probe": ok(CANONICAL)})
    assert m.migrate_node(NODE, ssh_cmd=second)["outcome"] == m.OK_ALREADY
    assert second.modes == ["probe"]


def test_dry_run_writes_nothing_and_shows_the_plan():
    res = Responder({"probe": ok(LEGACY_LIVE)})
    r = m.migrate_node(NODE, ssh_cmd=res, dry_run=True)

    assert r["outcome"] == m.DRY_RUN
    assert res.modes == ["probe"], "a dry run must not ask a node to change anything"
    assert "enable --now" in r["plan"] and "ONLY THEN disable" in r["plan"]
    assert f"{NEW}: enabled=not-found" in r["plan"]
    assert f"{OLD}: enabled=enabled" in r["plan"]


def test_dry_run_on_a_canonical_node_says_nothing_to_do():
    r = m.migrate_node(NODE, ssh_cmd=Responder({"probe": ok(CANONICAL)}), dry_run=True)
    assert r["outcome"] == m.DRY_RUN and "already enabled+active" in r["plan"]


def test_dry_run_never_reports_a_broken_fleet_as_failed():
    """`--dry-run` exiting 1 on healthy nodes reads as "the migration is broken"
    and kills the rehearsal this card exists to encourage."""
    res = Responder({"probe": ok(LEGACY_LIVE)})
    summary = m.migrate_fleet([NODE, OTHER], ssh_factory=lambda h: res, dry_run=True)
    assert summary["ok"] is True and summary["dry_run"] is True
    assert m.migrate_fleet([], ssh_factory=lambda h: res, dry_run=True)["ok"] is False


def test_unreadable_node_is_refused_and_untouched():
    res = Responder({"probe": fault(m.RC_NO_BUS, "NO_BUS", "Failed to connect to bus")})
    r = m.migrate_node(NODE, ssh_cmd=res)

    assert r["outcome"] == m.FAILED and "UNVERIFIABLE" in r["message"]
    assert res.modes == ["probe"], "must not touch a node it cannot read"


def test_enable_failure_keeps_the_legacy_unit_and_says_so():
    res = Responder({"probe": ok(LEGACY_LIVE),
                     "enable-new": fault(22, "NO_PID",
                                         f"{NEW} reports MainPID=0 — nothing is being "
                                         f"supervised; refusing to retire {OLD}")})
    r = m.migrate_node(NODE, ssh_cmd=res)

    assert r["outcome"] == m.FAILED
    assert "NO_PID" in r["message"] and "no supervision lost" in r["message"]
    assert "retire-old" not in res.modes


def test_retire_failure_is_reported_as_twin_state_not_plain_failure():
    """If the legacy unit could not be retired, more than one unit is still
    enabled/active. An operator must be able to see "a twin may be live" in the
    report, distinct from "nothing happened"."""
    res = Responder({"probe": ok(LEGACY_LIVE), "enable-new": ok(ENABLED_NEW),
                     "retire-old": fault(25, "TWIN",
                                         "2 units active — a second daemon loop is supervised")})
    r = m.migrate_node(NODE, ssh_cmd=res)

    assert r["outcome"] == m.TWIN_STATE and "TWIN" in r["message"]
    summary = m.migrate_fleet([NODE], ssh_factory=lambda h: res)
    assert summary["twins"] == [NODE] and summary["ok"] is False
    assert any(l.startswith("WARN|possible twin") for l in m.bootstrap_lines(summary))


def test_final_readback_is_the_arbiter_not_the_exit_codes():
    """Both verbs said OK; the node still has two active units. The client must
    trust the node's re-read and refuse to call it migrated."""
    calls = []

    def node(command, timeout=120):
        mode = m.mode_of_command(command)
        calls.append(mode)
        if mode == "probe":
            # 1st probe: legacy-only. Final probe: BOTH active — the verbs lied.
            return ok(LEGACY_LIVE if len(calls) == 1 else [
                *state_block(new_enabled="enabled", new_active="active", new_pid="7001",
                             old_enabled="enabled", old_active="active", old_pid="4112",
                             file_new="812", file_old="812")])
        if mode == "enable-new":
            return ok(ENABLED_NEW)
        return ok(["HSCC_RC: 0"])

    r = m.migrate_node(NODE, ssh_cmd=node)

    assert r["outcome"] == m.TWIN_STATE
    assert "two active units" in r["message"] and "VERIFY BY HAND" in r["message"]
    assert calls == ["probe", "enable-new", "retire-old", "probe"]


def test_final_readback_failure_is_a_fault_not_a_pass():
    seq = iter([ok(LEGACY_LIVE), ok(ENABLED_NEW), ok(CANONICAL),
                fault(255, "SSH_DIED", "kex_exchange_identification: read: Connection reset")])
    r = m.migrate_node(NODE, ssh_cmd=lambda cmd, to: next(seq))

    assert r["outcome"] == m.FAILED
    assert "read-back failed" in r["message"] and "VERIFY BY HAND" in r["message"]


def test_a_crashing_transport_never_aborts_the_fleet():
    def boom(command, timeout=120):
        raise RuntimeError("sparkrun exploded")

    summary = m.migrate_fleet([NODE, OTHER],
                              ssh_factory=lambda h: boom if h == NODE
                              else Responder({"probe": ok(CANONICAL)}))
    assert summary["failed"] == [NODE] and summary["already"] == [OTHER]
    assert summary["ok"] is False
    assert "unexpected RuntimeError" in summary["nodes"][0]["message"]


# ── fleet report ────────────────────────────────────────────────────────────

def test_report_and_bootstrap_lines_shape():
    res = Responder({"probe": [ok(LEGACY_LIVE), ok(CANONICAL)],
                     "enable-new": ok(ENABLED_NEW), "retire-old": ok(CANONICAL)})
    summary = m.migrate_fleet([NODE, OTHER],
                              ssh_factory=lambda h: res if h == NODE
                              else Responder({"probe": ok(CANONICAL)}))
    assert summary["ok"] and summary["migrated"] == [NODE] and summary["already"] == [OTHER]
    assert summary["unit_name"] == NEW and summary["legacy_unit_name"] == OLD

    lines = m.format_report(summary)
    assert lines[0].startswith(f"OK|{NODE}:")
    assert any(l.startswith("   . ") for l in lines)
    boot = m.bootstrap_lines(summary)
    assert boot[0] == (f"OK|systemd --user daemon unit name unified on 2/2 node(s) "
                       f"— one unit, {NEW}")
    assert not any(l.startswith("WARN|") for l in boot)


def test_empty_fleet_is_a_warning_not_a_silent_success():
    summary = m.migrate_fleet([], ssh_factory=lambda h: Responder({}))
    assert summary["ok"] is False
    assert m.bootstrap_lines(summary) == [
        "WARN|no nodes were checked — is the sparkrun cluster configured?"]


def test_cli_without_hosts_exits_2_and_never_contacts_a_node(monkeypatch, capsys):
    monkeypatch.setattr(m, "_hosts_from_detect", lambda: [])
    monkeypatch.setattr(m, "_default_ssh",
                        lambda *a, **k: pytest.fail("CLI contacted a node"))
    assert m.main(["--dry-run"]) == 2
    assert "no hosts" in capsys.readouterr().out


def test_cli_dry_run_reports_and_writes_nothing(monkeypatch, capsys):
    res = Responder({"probe": ok(LEGACY_LIVE)})
    monkeypatch.setattr(m, "_default_ssh", lambda h, cmd, to=120: res(cmd, to))

    assert m.main(["--dry-run", "--hosts", f"{NODE},{OTHER}"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN — nothing written on 2 node(s)" in out
    assert res.modes == ["probe", "probe"]


def test_cli_json_shape_and_failure_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(m, "_default_ssh",
                        lambda h, cmd, to=120: fault(m.RC_NO_BUS, "NO_BUS", "no bus"))
    assert m.main(["--json", "--hosts", NODE]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["failed"] == [NODE]
    assert payload["unit_name"] == NEW and payload["legacy_unit_name"] == OLD


def test_production_transport_is_the_sparkrun_bridge(monkeypatch):
    """No hand-rolled ssh: the transport is hscc-cluster.clusterlib.ssh_cmd
    (sparkrun owns the fleet)."""
    calls = []

    class FakeClusterlib:
        @staticmethod
        def ssh_cmd(host, command, timeout=120):
            calls.append((host, command, timeout))
            return ok(CANONICAL)

    monkeypatch.setitem(sys.modules, "clusterlib", FakeClusterlib)
    res = m._default_ssh(NODE, "echo hi", 33)
    assert calls == [(NODE, "echo hi", 33)] and res["ok"]

    # and when the bridge is missing that is an honest failure, never ok=True
    monkeypatch.setitem(sys.modules, "clusterlib", None)
    bad = m._default_ssh(NODE, "echo hi")
    assert bad["ok"] is False and "clusterlib unavailable" in bad["stderr"]


def test_pkgs_default_follows_the_environment(monkeypatch):
    """An install rooted elsewhere must be migrated against ITS code.

    Baking ``~/.hermes/plugins`` makes the migration emit a unit whose ExecStart
    and PYTHONPATH name a copy the node does not run — the same rule
    ``install_payload.py`` follows, and the reason
    ``test_bootstrap_isolated.test_helpers_do_not_hardcode_the_home_paths``
    rejects a bare expanduser in a bootstrap helper.
    """
    monkeypatch.delenv("PLUGINS", raising=False)
    assert m._pkgs_from_args([]) == os.path.expanduser("~/.hermes/plugins")
    monkeypatch.setenv("PLUGINS", "/opt/other-runtime/plugins")
    assert m._pkgs_from_args([]) == "/opt/other-runtime/plugins"
    assert m._pkgs_from_args(["--pkgs", "/srv/checkout"]) == "/srv/checkout"


# ── bash-backed fake node: the REAL script against a stateful systemctl ─────

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
# A stateful systemd --user manager, modelled on the parts that matter.
#
# Faithful where a fake could lie:
#   * `enable` only succeeds for a unit file with a real `[Install]` section AND a
#     `WantedBy=` line (real systemd refuses otherwise), and it creates the
#     `default.target.wants` symlink; `is-enabled` answers `enabled` ONLY because
#     of that symlink — so "wrote the file, never enabled it" surfaces as
#     `static`, not as success;
#   * a file with no `[Install]` is `static`; a name with no file anywhere is
#     `not-found` (exit 4); never the empty string a broken probe would produce;
#   * `disable --now` stops the unit (active -> inactive, MainPID -> 0).
set -u
S="$FAKE_STATE_DIR"; U="$FAKE_UNIT_DIR"; LOG="$FAKE_CALLS"
WANTS="$U/default.target.wants"
echo "$*" >> "$LOG"

if [ "${1:-}" != "--user" ]; then
  echo "This program is not intended to be run system-wide." >&2; exit 1
fi
shift

verb=""; unit=""; prop=""; now=0; prev=""
for a in "$@"; do
  if [ "$prev" = "-p" ]; then prop="$a"; prev=""; continue; fi
  case "$a" in
    --now) now=1 ;;
    -p) prev="-p" ;;
    --value|--no-pager|--quiet|--force|--ignore-dependencies) ;;
    *) if [ -z "$verb" ]; then verb="$a"; elif [ -z "$unit" ]; then unit="$a"; fi ;;
  esac
done

ufile() { [ -e "$U/$1" ] || [ -L "$U/$1" ] && printf '%s' "$U/$1" || printf ''; }
has() { grep -Eq "$1" "$2" 2>/dev/null; }

en_word() {
  local p="$U/$1"
  if [ -L "$p" ] && [ "$(readlink "$p")" = "/dev/null" ]; then echo masked; return; fi
  if [ -L "$WANTS/$1" ]; then echo enabled; return; fi
  if [ -e "$p" ]; then
    if has '^\[Install\]' "$p" && has '^WantedBy=' "$p"; then echo disabled; else echo static; fi
    return
  fi
  echo not-found
}
ac_get() { [ -f "$S/$1.active" ] && cat "$S/$1.active" || echo inactive; }
pid_get() { [ -f "$S/$1.mainpid" ] && cat "$S/$1.mainpid" || echo 0; }
ac_set() { printf '%s' "$2" > "$S/$1.active"; }
pid_set() { printf '%s' "$2" > "$S/$1.mainpid"; }
exec_of() { grep '^ExecStart=' "$(ufile "$1")" 2>/dev/null | head -1 | cut -d= -f2-; }

case "$verb" in
  daemon-reload|reset-failed) exit 0 ;;
  is-enabled)
    w="$(en_word "$unit")"; echo "$w"
    case "$w" in enabled) exit 0 ;; masked|not-found) exit 1 ;; *) exit 1 ;; esac ;;
  is-active)
    w="$(ac_get "$unit")"; echo "$w"
    case "$w" in active|activating) exit 0 ;; *) exit 3 ;; esac ;;
  show)
    case "$prop" in
      MainPID) pid_get "$unit" ;;
      ExecStart) exec_of "$unit" ;;
      *) echo "" ;;
    esac; exit 0 ;;
  enable)
    p="$(ufile "$unit")"
    [ -n "$p" ] || { echo "Failed to enable unit: Unit $unit does not exist." >&2; exit 1; }
    if ! has '^\[Install\]' "$p" || ! has '^WantedBy=' "$p"; then
      echo "Failed to execute operation: The unit file $p has no [Install] section, refusing to enable." >&2
      exit 1
    fi
    mkdir -p "$WANTS"; ln -sf "$p" "$WANTS/$unit"
    if [ "$now" = "1" ]; then
      # a unit whose ExecStart is empty/lost (the rendered-console-view bug)
      # does not run the daemon loop: systemd would still report a MainPID, so
      # model the honest part — nothing to supervise, unit lands in `failed`.
      if [ -n "$(exec_of "$unit")" ]; then
        ac_set "$unit" active; pid_set "$unit" "$(( 9100 + $(wc -l < "$LOG") ))"
      else
        ac_set "$unit" failed; pid_set "$unit" 0
        echo "Job for $unit failed because the control process exited with error code." >&2
        exit 1
      fi
    fi
    exit 0 ;;
  disable)
    if [ -n "${FAKE_DISABLE_FAILS:-}" ] && [ "$unit" = "$FAKE_DISABLE_FAILS" ]; then
      echo "Failed to disable unit: Transaction is not destructive." >&2; exit 1
    fi
    rm -f "$WANTS/$unit"
    if [ "$now" = "1" ]; then ac_set "$unit" inactive; pid_set "$unit" 0; fi
    exit 0 ;;
  start|restart)
    p="$(ufile "$unit")"
    [ -n "$p" ] || { echo "Unit $unit not loaded." >&2; exit 5; }
    if [ -n "$(exec_of "$unit")" ]; then
      ac_set "$unit" active; pid_set "$unit" "$(( $(pid_get "$unit") + 1 ))"
    else
      ac_set "$unit" failed; pid_set "$unit" 0; exit 1
    fi
    exit 0 ;;
  stop) ac_set "$unit" inactive; pid_set "$unit" 0; exit 0 ;;
  mask) ln -sf /dev/null "$U/$unit"; ac_set "$unit" inactive; pid_set "$unit" 0; exit 0 ;;
  unmask) [ -L "$U/$unit" ] && rm -f "$U/$unit"; exit 0 ;;
  *) exit 0 ;;
esac
"""

#: systemctl verbs that change nothing. A dry run — and a second run over an
#: already-migrated node — may issue nothing else.
READ_ONLY_VERBS = frozenset({"is-enabled", "is-active", "show", "status",
                             "daemon-reload", "cat", "list-unit-files", "get-wants"})


CLEAN_UNIT = """[Unit]
Description=HSCC Monitoring Daemon (Hermes Spark Cluster Control)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m hscc_daemon start-daemon
WorkingDirectory=/home/op/.hermes/plugins/hscc_daemon
Environment=PATH=/usr/bin:/bin
Restart=on-failure

[Install]
WantedBy=default.target
"""

# What pre-fix `hscc.py plist > UNIT_FILE` actually left on disk (measured on
# this checkout): a rendered Rich panel — box-drawing lines, and every long
# assignment soft-wrapped, so systemd's parser dropped `start-daemon` from
# ExecStart and truncated `Environment=PATH=` at the wrap.
CORRUPT_UNIT = (
    "\u256d\u2500 plist \u2500\u2510\n"
    "\u2502 [Unit]                                       \u2502\n"
    "\u2502 ExecStart=/usr/bin/python3 -m hscc_daemon     \u2502\n"
    "\u2502  start-daemon                                 \u2502\n"
    "\u2502 Environment=PATH=/home/op/.hermes/hermes-age  \u2502\n"
    "\u2502 nt/venv/bin:/usr/bin:/bin                     \u2502\n"
    "\u2502 WantedBy=default.target                       \u2502\n"
    "\u2570\u2500\u2500\u2500\u2518\n")


class FakeNode:
    """Runs the REAL generated node-side script against a stateful
    ``systemctl --user`` shim, the REAL emitter, and a tmp ``$HOME``.

    ``legacy_body=CORRUPT_UNIT`` reproduces a node whose legacy unit file is the
    Rich-rendered view, so the tests prove the migration no longer depends on
    that file being parseable and that the emitter — not a redirect — is what
    makes the canonical unit enable-able.
    """

    def __init__(self, tmp_path, *, legacy_enabled=True, legacy_file=True,
                 legacy_body=CLEAN_UNIT, no_systemctl=False, linger_ok=True):
        self.root = Path(tmp_path) / "node"
        self.bin = self.root / "bin"
        self.home = self.root / "home"
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.state = self.root / "state"
        self.bin.mkdir(parents=True)
        self.unit_dir.mkdir(parents=True)
        self.state.mkdir()
        self.calls = self.root / "systemctl_calls"
        self.calls.write_text("")
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_RUNTIME_DIR": str(self.root / "run"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/dev/null",
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_UNIT_DIR": str(self.unit_dir),
            "FAKE_CALLS": str(self.calls),
            "HSCC_PYBIN": sys.executable,
            "PYTHONPATH": "",
        })
        (self.root / "run").mkdir(exist_ok=True)
        if not no_systemctl:
            p = self.bin / "systemctl"
            p.write_text(STUB_SYSTEMCTL)
            p.chmod(0o755)
        if legacy_file:
            (self.unit_dir / OLD).write_text(legacy_body)
        self._set(OLD, active="active", mainpid="4112")
        if legacy_enabled:
            wants = self.unit_dir / "default.target.wants"
            wants.mkdir(exist_ok=True)
            (wants / OLD).symlink_to(self.unit_dir / OLD)

    def _set(self, unit, **kw):
        if "active" in kw:
            (self.state / f"{unit}.active").write_text(kw["active"])
        if "mainpid" in kw:
            (self.state / f"{unit}.mainpid").write_text(str(kw["mainpid"]))

    def ssh_cmd(self, command, timeout=120):
        res = subprocess.run(["bash", "-c", command], env=self.env,
                             capture_output=True, text=True, timeout=timeout)
        return {"ok": res.returncode == 0, "stdout": res.stdout,
                "stderr": res.stderr, "code": res.returncode}

    def units(self):
        out = {}
        for u in (NEW, OLD):
            p = self.unit_dir / u
            wants = self.unit_dir / "default.target.wants" / u
            if p.is_symlink() and p.readlink() == Path("/dev/null"):
                en = "masked"
            elif wants.is_symlink():
                en = "enabled"
            elif p.exists():
                en = "static" if "[Install]" not in p.read_text() else "disabled"
            else:
                en = "not-found"
            ap = self.state / f"{u}.active"
            pp = self.state / f"{u}.mainpid"
            out[u] = {"enabled": en,
                      "active": ap.read_text() if ap.exists() else "inactive",
                      "mainpid": pp.read_text() if pp.exists() else "0"}
        return out

    def body(self, unit):
        p = self.unit_dir / unit
        return p.read_text() if p.exists() else ""

    def systemd_calls(self):
        return self.calls.read_text().splitlines()

    def verbs(self):
        """The systemctl VERB of each recorded call, in order.

        The shim logs the whole argv ("--user is-enabled x"), so this strips the
        ``--user`` and takes the verb. Assertions compare VERBS, never substrings:
        ``"enable" in "is-enabled"`` is true, so a substring check would both
        false-alarm on a read-only probe and pass on a run that really did enable.
        """
        out = []
        for line in self.systemd_calls():
            parts = [p for p in line.split() if p != "--user"]
            out.append(parts[0] if parts else "")
        return out


@pytest.fixture
def node_factory(tmp_path):
    made = []

    def make(**kw):
        n = FakeNode(tmp_path / f"n{len(made)}", **kw)
        made.append(n)
        return n
    return make


def test_fake_node_probe_sees_the_legacy_unit(node_factory):
    node = node_factory()
    st = m.probe_node(node.ssh_cmd)
    assert st["ok"] is True, st
    assert st["units"][OLD] == {"enabled": "enabled", "active": "active",
                                "mainpid": "4112"}
    assert st["units"][NEW]["enabled"] == "not-found"
    assert m.has_legacy(st) and not m.canonical_healthy(st)


def test_fake_node_without_systemctl_is_a_fault_not_a_clean_node(node_factory):
    node = node_factory(no_systemctl=True)
    r = m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)
    assert r["outcome"] == m.FAILED and "NO_SYSTEMD" in r["message"]
    # nothing was removed: a node we cannot see must not be "cleaned"
    assert (node.unit_dir / OLD).exists()


def test_fake_node_migration_leaves_exactly_one_unit(node_factory):
    node = node_factory(legacy_body=CORRUPT_UNIT)
    res = m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)

    assert res["outcome"] == m.OK_MIGRATED, res["message"]
    units = node.units()
    assert units[NEW]["enabled"] == "enabled" and units[NEW]["active"] == "active"
    assert int(units[NEW]["mainpid"]) > 0
    assert units[OLD]["enabled"] == "not-found" and units[OLD]["active"] == "inactive"
    assert units[OLD]["mainpid"] == "0"
    assert not (node.unit_dir / OLD).exists(), "the legacy file must be gone"
    assert not (node.unit_dir / "default.target.wants" / OLD).exists()
    assert m.verify_final({"units": units}) == ""


def test_fake_node_canonical_unit_is_enableable_where_the_rich_view_was_not(node_factory):
    """The emitter — not a redirected console view — is what makes the unit work.

    The shim refuses to enable a file with no real `[Install]`/`WantedBy=` lines,
    exactly as systemd does, so this pins the CONTENT fix (not just the name):
    the canonical file the emitter writes enables, and the Rich-rendered legacy
    file it replaced would not have.
    """
    node = node_factory(legacy_body=CORRUPT_UNIT)
    assert m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)["outcome"] == m.OK_MIGRATED

    body = node.body(NEW)
    assert "[Install]" in body and "WantedBy=default.target" in body
    exec_lines = [l for l in body.splitlines() if l.startswith("ExecStart=")]
    assert len(exec_lines) == 1, "ExecStart must be ONE physical line"
    assert exec_lines[0].rstrip().endswith("start-daemon"), (
        f"the pre-fix unit lost its subcommand to a soft-wrap: {exec_lines}")
    for line in body.splitlines():
        assert "\u2502" not in line and "\u256d" not in line, \
            "a rendered console border must never reach a unit file"
    assert "PATH=" in body and not any(
        l.startswith("Environment=PATH=") and l.rstrip().endswith(("-", "/", ":"))
        for l in body.splitlines()), "PATH= must not end mid-value (a wrap artifact)"


def test_fake_node_dry_run_changes_nothing_at_all(node_factory):
    node = node_factory()
    before = node.units()
    res = m.migrate_node(NODE, ssh_cmd=node.ssh_cmd, dry_run=True)

    assert res["outcome"] == m.DRY_RUN
    assert node.units() == before
    assert (node.unit_dir / OLD).exists()
    assert not (node.unit_dir / NEW).exists()
    # verb-level, not substring-level: "is-enabled" CONTAINS "enable", so a naive
    # substring check would flag a read-only probe as a mutation (and, worse,
    # would have passed on a run that really did call `enable`).
    wrote = [v for v in node.verbs() if v not in READ_ONLY_VERBS]
    assert not wrote, f"a dry run wrote on the node: {wrote}"


def test_fake_node_second_run_is_a_noop(node_factory):
    node = node_factory()
    assert m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)["outcome"] == m.OK_MIGRATED
    calls_after_first = len(node.systemd_calls())
    pid_after_first = node.units()[NEW]["mainpid"]

    assert m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)["outcome"] == m.OK_ALREADY
    assert node.units()[NEW]["mainpid"] == pid_after_first, \
        "a re-run must not restart the running daemon"
    assert set(node.verbs()[calls_after_first:]) <= READ_ONLY_VERBS, \
        "a second run may only read: " + str(node.systemd_calls()[calls_after_first:])


def test_fake_node_twin_survives_as_a_visible_defect(node_factory):
    """A legacy unit systemd refuses to disable (a real thing: a unit pulled in by
    another unit's `Wants=`) must end as TWIN_STATE with the twin still visible —
    never as success, and never silently "half done".

    This is the case where `disable --now` exits nonzero and the node's own final
    read-back is what catches it: both units are still enabled.
    """
    node = node_factory()
    node.env["FAKE_DISABLE_FAILS"] = OLD

    res = m.migrate_node(NODE, ssh_cmd=node.ssh_cmd)
    units = node.units()

    assert res["outcome"] == m.TWIN_STATE, res["message"]
    assert "TWIN" in res["message"]
    assert units[NEW]["enabled"] == "enabled", "the canonical unit must still be up"
    assert units[OLD]["enabled"] == "enabled", f"the twin is still there: {units}"
    # never two ACTIVE: the node's read-back arithmetic is what says so
    active = [u for u in (NEW, OLD) if units[u]["active"] == "active"]
    assert len(active) <= 1, f"two supervised loops are live: {active}"
    summary = m.migrate_fleet([NODE], ssh_factory=lambda h: node.ssh_cmd)
    assert summary["twins"] == [NODE] and summary["ok"] is False


def test_fake_node_retire_refused_until_canonical_is_up(node_factory):
    """The node script checks the precondition itself, so it cannot be driven out
    of order by a hand-copy into a shell: retire-old on a node whose canonical
    unit is not running must fail WITHOUT touching the legacy unit."""
    node = node_factory()
    r = node.ssh_cmd(m.command_for("retire-old", pkgs=REPO))
    assert r["ok"] is False
    lines = m._hscc_lines(r["stdout"])
    assert m._one(lines, "HSCC_CODE") == "NOT_SINGLETON"
    assert (node.unit_dir / OLD).exists(), "legacy unit must survive a refused retire"
    assert node.units()[OLD]["enabled"] == "enabled"
    assert not any(c.startswith("disable") for c in node.systemd_calls())


def test_fake_node_emitter_is_the_only_writer_and_is_raw(node_factory):
    """unit-emit on the fake node writes a unit systemd's own `enable` accepts —
    and, because the migration script does NOT pass --purge-legacy to it, a
    canonical enable never silently deletes the unit that is currently keeping
    the daemon supervised."""
    node = node_factory()
    r = m._run(node.ssh_cmd, m.command_for("enable-new", pkgs=REPO), 60)
    assert r["ok"] is True, r["stdout"] + r["stderr"]
    assert (node.unit_dir / OLD).exists(), (
        "enable-new must not purge the legacy unit — that would leave zero "
        "supervised units if the enable below it had failed")
    assert node.units()[NEW]["enabled"] == "enabled"
    assert node.body(NEW).startswith("[Unit]")
