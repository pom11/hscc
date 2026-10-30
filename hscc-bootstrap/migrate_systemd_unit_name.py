"""Retire the legacy ``hscc_daemon.service`` unit name on every cluster node (t_d8f13693).

WHY
---
Until this card the two Linux install paths wrote DIFFERENT systemd --user unit
names for the SAME daemon (``systemd-setup.sh`` -> ``hscc_daemon.service``,
``install.py`` -> ``com.hermes.hscc_daemon.service``). Nodes installed by the
shell helper therefore still carry the old unit, and both failure modes the card
names are live on such a box:

  * ``systemctl --user disable com.hermes.hscc_daemon.service`` leaves the
    legacy unit enabled, so the daemon returns at next login;
  * a re-install can leave BOTH units enabled — two supervisors for one daemon
    loop, i.e. the twin class one level above ``daemon_singleton.py``.

WHAT THIS DOES
--------------
Per node, read-only FIRST, then (unless ``--dry-run``) converge onto exactly one
enabled unit under the canonical name:

  1. probe: which names exist, which are enabled, which are active, and what each
     unit's ``ExecStart`` resolves to;
  2. write the canonical unit with the node's OWN
     ``hscc_daemon.install.cmd_unit_emit`` — the same emitter ``hscc install``
     uses, so the interpreter and paths are resolved on that node, not here;
  3. ``enable --now`` it and read it back enabled, active, with a non-zero
     MainPID;
  4. ONLY then ``disable --now`` + remove the legacy unit, and read the final
     state back: at most one enabled, at most one active, canonical one active.

THE TWIN RULE IS A POSTCONDITION, NOT A HOPE. Every write path ends in a final
read-back, and a node that ends with zero active units, two enabled units, or two
active units is reported ``twin-state``/``failed`` with names and pids — never as
migrated. The ordering is what makes the rule satisfiable: the new unit is proven
supervised BEFORE the old one is touched, so no failure path can leave zero
supervised units. The worst interruption is a brief overlap of two supervisors,
which ``daemon_singleton``'s lock already tolerates (the loser refuses to tick).
There is deliberately NO "retire the old unit first" option: that order can leave
a node with nothing supervising the daemon, which is the worse half of the class.

HONEST SIGNAL: unreadable state is never "absent"
-------------------------------------------------
``systemctl --user`` without the session bus answers with error text, or nothing.
Every state word is validated against systemd's own vocabulary (``ENABLED_WORDS``
/ ``ACTIVE_WORDS`` — the SAME sets build the node-side matcher and the client
parser, so they cannot disagree) and an unknown word aborts as ``NO_BUS``. A
migration script that read "unreadable" as "absent" would delete a unit on a node
it could not see; this one refuses and says so.

NEVER RUNS AGAINST LIVE NODES BY ITSELF
---------------------------------------
This module ships the script; the operator schedules the run (the card's
constraint). It reaches nodes only through the sanctioned sparkrun bridge
(``hscc-cluster/clusterlib.ssh_cmd`` -> ``sparkrun_bridge``, the same resolution
``install_notifier_mask`` / ``install_autoreboot`` use), never via hand-rolled
ssh, and tests inject a fake transport so no test can open a socket.
``--dry-run`` writes nothing anywhere and is the shape the operator reviews first.

It never touches the operator's launchd job or plist: the whole module is
Linux/systemd --user only.

Address discipline: no real LAN/tailnet address appears here; fixtures use the
documented placeholder convention (10.0.0.x for a LAN node).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── the two names: imported, never re-typed ─────────────────────────────────
# hscc_daemon/unit_name.py is the single source (test_unit_name_drift.py pins
# every reader to it; test_restart_daemon.py's candidate pin proves the restart
# helper resolves through the same file).
_BOOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DAEMON_DIR = os.path.join(os.path.dirname(_BOOT_DIR), "hscc_daemon")
if _DAEMON_DIR not in sys.path:
    sys.path.insert(0, _DAEMON_DIR)

try:
    from unit_name import LEGACY_UNIT_NAME, UNIT_NAME  # noqa: E402
except ImportError as exc:  # pragma: no cover - layout must provide it
    raise SystemExit(f"migrate_systemd_unit_name: cannot import unit_name.py: {exc}")


# ── outcomes ────────────────────────────────────────────────────────────────
OK_MIGRATED = "migrated"
OK_ALREADY = "already-canonical"
DRY_RUN = "dry-run"
SKIPPED = "skipped"
FAILED = "failed"
#: The node ended in (or in the middle of producing) the state this card exists
#: to prevent. Distinct from FAILED on purpose: an operator must be able to tell
#: "a twin may be live" from "nothing happened" at a glance in the report.
TWIN_STATE = "twin-state"

OK_OUTCOMES = frozenset({OK_MIGRATED, OK_ALREADY})

#: The node-side script's rc for "I could not read a trustworthy unit state"
#: (no systemctl, no bus, a word systemctl never prints). The client renders it
#: as a fault, never as "no legacy unit".
RC_NO_BUS = 20

#: Every word ``systemctl --user is-enabled`` / ``is-active`` can legitimately
#: print on the fleet's systemd (255). Anything outside the set is NOT a state:
#: it is bus-error text. Single source — the node-side matcher and the Python
#: parser are both built from these sets (the idiom ``install_notifier_mask``
#: uses), so client and node cannot disagree about what counts as state.
ENABLED_WORDS = frozenset({
    "alias", "bad-link", "disabled", "disabled-runtime", "enabled",
    "enabled-runtime", "generated", "indirect", "masked", "masked-runtime",
    "not-found", "static",
})
ACTIVE_WORDS = frozenset({
    "activating", "active", "deactivating", "dormant", "inactive", "failed",
})

#: Words that mean "systemd will start this again" / "systemd is running this
#: now" — the arithmetic behind the twin rule.
ENABLED_ON = ("enabled", "enabled-runtime")
ACTIVE_ON = ("active", "activating")


@dataclass
class NodeResult:
    node: str
    outcome: str
    actions: List[str] = field(default_factory=list)
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    plan: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"node": self.node, "outcome": self.outcome,
                             "actions": self.actions, "before": self.before,
                             "after": self.after, "message": self.message}
        if self.plan:
            d["plan"] = self.plan
        return d


# ── the node-side script ────────────────────────────────────────────────────
# Modes: probe (read-only), enable-new, retire-old. The unit names and the
# recognised-word sets are substituted from this module's constants, so the node
# and the client cannot drift on what is being migrated or what counts as state.
_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# hscc t_d8f13693 — systemd --user unit-name migration, node side.
# Modes: probe | enable-new | retire-old. HSCC_* lines only.
set -uo pipefail

MODE="__MODE__"
NEW="__NEW__"
OLD="__OLD__"
PKGS="__PKGS__"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
NEW_FILE="$UNIT_DIR/$NEW"
OLD_FILE="$UNIT_DIR/$OLD"

RC_OK=0
RC_NO_BUS=__RC_NO_BUS__
RC_NO_PYTHON=21
RC_ENABLE_FAILED=22
RC_NOT_SINGLETON=25
RC_EMIT_FAILED=26
RC_NO_SYSTEMD=27
RC_BAD_MODE=28
RC_REMOVE_FAILED=29

# Every word `systemctl --user is-enabled` / `is-active` can legitimately print
# (systemd 255, the fleet's version). Anything outside it is stderr-shaped text.
WOK_ENABLED=" __ENABLED_WORDS__ "
WOK_ACTIVE=" __ACTIVE_WORDS__ "

say() { printf '%s\n' "$*"; }
fail() { say "HSCC_RC: $1"; say "HSCC_CODE: $2"; say "HSCC_MSG: $3"; exit 1; }

command -v systemctl >/dev/null 2>&1 \
  || fail $RC_NO_SYSTEMD NO_SYSTEMD "no systemctl on this node — not a systemd host; nothing to migrate"

# The bus pitfall (same rule as the notifier-mask card): derive the session bus
# in ${VAR:-derived} form so a non-PAM transport does not turn unit state into
# guesswork. A node we cannot read is a FAULT, never "no unit present".
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
: "${DBUS_SESSION_BUS_ADDRESS:=unix:path=${XDG_RUNTIME_DIR}/bus}"
export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS

# 2>&1 on purpose: the error text is what tells us the bus is gone. The word is
# validated by check_words() in the MAIN shell, because a `fail` inside a command
# substitution would only exit the subshell and be silently swallowed.
raw_en() { systemctl --user is-enabled "$1" 2>&1 | head -n 1 | tr -d '[:space:]'; }
raw_ac() { systemctl --user is-active  "$1" 2>&1 | head -n 1 | tr -d '[:space:]'; }
main_pid() { systemctl --user show "$1" -p MainPID --value 2>/dev/null | tr -d '[:space:]'; }
exec_of() {
  # ExecStart as systemd resolved it (not the file text, which may be a symlink
  # or carry a drop-in override).
  systemctl --user show "$1" -p ExecStart --value 2>/dev/null \
    | tr '\n' ' ' | sed 's/[[:space:]]\{1,\}/ /g; s/^ //; s/ $//'
}

check_words() {
  local u w
  for u in "$NEW" "$OLD"; do
    w="$(raw_en "$u")"
    case "$WOK_ENABLED" in
      *" $w "*) ;;
      *) fail $RC_NO_BUS NO_BUS "systemctl --user is-enabled $u answered '$w' — not a unit state (bus/environment fault); state UNREADABLE, NOT 'no unit'" ;;
    esac
    w="$(raw_ac "$u")"
    case "$WOK_ACTIVE" in
      *" $w "*) ;;
      *) fail $RC_NO_BUS NO_BUS "systemctl --user is-active $u answered '$w' — not a unit state (bus/environment fault); state UNREADABLE, NOT 'no unit'" ;;
    esac
  done
}

emit_state() {
  local u
  for u in "$NEW" "$OLD"; do
    say "HSCC_UNIT: $u|enabled=$(raw_en "$u")|active=$(raw_ac "$u")|mainpid=$(main_pid "$u")"
  done
  say "HSCC_FILE_NEW: $([ -e "$NEW_FILE" ] && wc -c < "$NEW_FILE" 2>/dev/null | tr -d ' ' || echo absent)"
  say "HSCC_FILE_OLD: $([ -e "$OLD_FILE" ] && wc -c < "$OLD_FILE" 2>/dev/null | tr -d ' ' || echo absent)"
  say "HSCC_EXEC_NEW: $(exec_of "$NEW")"
  say "HSCC_EXEC_OLD: $(exec_of "$OLD")"
  say "HSCC_LINK_OLD: $([ -L "$OLD_FILE" ] && readlink "$OLD_FILE" || echo none)"
}

case "$MODE" in
  probe)
    check_words
    emit_state; say "HSCC_RC: $RC_OK"; exit 0 ;;
  enable-new|retire-old) ;;
  *) fail $RC_BAD_MODE BAD_MODE "unknown mode '$MODE'" ;;
esac

check_words

PY=""
for c in "${HSCC_PYBIN:-}" "$HOME/.hermes/hermes-agent/venv/bin/python" "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
  [ -n "$c" ] && [ -x "$c" ] && { PY="$c"; break; }
done

if [ "$MODE" = "enable-new" ]; then
  [ -n "$PY" ] || fail $RC_NO_PYTHON NO_PYTHON "no usable python3 to run the unit emitter"
  # 1. Write the canonical unit with the daemon's OWN emitter (the raw template;
  # never a redirected console view — that is what made the legacy unit file
  # unparseable in the first place).
  EMIT_OUT="$(PYTHONPATH="$PKGS${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c 'import sys; from hscc_daemon.install import cmd_unit_emit; sys.exit(cmd_unit_emit(sys.argv[1:]))' --unit-dir "$UNIT_DIR" --json 2>&1)"
  EMIT_RC=$?
  say "HSCC_EMIT: $EMIT_OUT"
  [ "$EMIT_RC" -eq 0 ] || fail $RC_EMIT_FAILED EMIT_FAILED "unit emitter failed (rc=$EMIT_RC): $EMIT_OUT"
  [ -f "$NEW_FILE" ] || fail $RC_EMIT_FAILED EMIT_FAILED "emitter reported success but $NEW_FILE was not written"
  systemctl --user daemon-reload

  # 2. Enable + start. `enable --now` starts the unit only when it was not
  # already running; `restart` would be wrong here — on a fully-migrated box it
  # would kill a healthy daemon for no reason.
  OUT="$(systemctl --user enable --now "$NEW" 2>&1)"; RC=$?
  [ -n "$OUT" ] && say "HSCC_ENABLE_OUT: $OUT"
  [ "$RC" -eq 0 ] || fail $RC_ENABLE_FAILED ENABLE_FAILED "systemctl --user enable --now $NEW failed (rc=$RC): $OUT"

  # 3. Read it back: enabled, active, and the supervisor reports a pid. A unit
  # that enabled but never came up must NOT be followed by retiring the old one
  # — that is exactly how a migration produces an unsupervised daemon.
  check_words
  W="$(raw_en "$NEW")"
  [ "$W" = "enabled" ] || [ "$W" = "enabled-runtime" ] \
    || fail $RC_ENABLE_FAILED NOT_ENABLED "$NEW is '$W' after enable --now — refusing to retire $OLD"
  A="$(raw_ac "$NEW")"
  case "$A" in active|activating) ;; *)
    fail $RC_ENABLE_FAILED NO_PID "$NEW is '$A' after enable --now — refusing to retire $OLD" ;;
  esac
  i=0; P="$(main_pid "$NEW")"
  while [ "${P:-0}" = "0" ] && [ "$i" -lt 10 ]; do sleep 1; i=$((i+1)); P="$(main_pid "$NEW")"; done
  case "$P" in ''|0)
    fail $RC_ENABLE_FAILED NO_PID "$NEW reports MainPID=$P — nothing is being supervised; refusing to retire $OLD" ;;
  esac
  say "HSCC_NEW_PID: $P"
  emit_state; say "HSCC_RC: $RC_OK"; exit 0
fi

# retire-old: the canonical unit must ALREADY be enabled+active with a pid.
# Verified on the node rather than trusted from the caller, so the script cannot
# be driven out of order — the twin rule survives a copy-paste into a shell.
A_NEW="$(raw_ac "$NEW")"; P_NEW="$(main_pid "$NEW")"
case "$A_NEW" in active|activating) ;; *)
  fail $RC_NOT_SINGLETON NOT_SINGLETON "$NEW is '$A_NEW' — bring it up before retiring $OLD (never leave zero supervised units)" ;;
esac
case "$P_NEW" in ''|0)
  fail $RC_NOT_SINGLETON NOT_SINGLETON "$NEW reports no MainPID — refusing to retire $OLD" ;;
esac

EN_O="$(raw_en "$OLD")"
if [ "$EN_O" != "not-found" ] || [ -e "$OLD_FILE" ]; then
  OUT="$(systemctl --user disable --now "$OLD" 2>&1)"
  say "HSCC_DISABLE_OUT: $OUT"
  # A legacy file that is a SYMLINK is somebody's override, not our unit:
  # `disable` already inerted it and deleting a foreign symlink is not ours to
  # do. Report it instead of removing it.
  if [ -L "$OLD_FILE" ]; then
    say "HSCC_KEPT_LINK: $OLD_FILE -> $(readlink "$OLD_FILE") (a user override, left in place; remove by hand)"
  elif [ -e "$OLD_FILE" ]; then
    rm -f "$OLD_FILE" && say "HSCC_REMOVED: $OLD_FILE" \
      || fail $RC_REMOVE_FAILED REMOVE_FAILED "could not remove $OLD_FILE"
  fi
  systemctl --user daemon-reload
fi

# FINAL read-back: at most one enabled, at most one active, and the canonical
# unit is the active one. Anything else is the twin class or an orphan —
# reported as such, never as success.
check_words
EN_N="$(raw_en "$NEW")"; EN_O="$(raw_en "$OLD")"
AC_N="$(raw_ac "$NEW")"; AC_O="$(raw_ac "$OLD")"
n_enabled=0; n_active=0
case "$EN_N" in enabled|enabled-runtime) n_enabled=$((n_enabled+1));; esac
case "$EN_O" in enabled|enabled-runtime) n_enabled=$((n_enabled+1));; esac
case "$AC_N" in active|activating) n_active=$((n_active+1));; esac
case "$AC_O" in active|activating) n_active=$((n_active+1));; esac
say "HSCC_FINAL: new=$EN_N/$AC_N old=$EN_O/$AC_O enabled=$n_enabled active=$n_active"
[ "$n_enabled" -le 1 ] || fail $RC_NOT_SINGLETON TWIN "$n_enabled units enabled ($NEW=$EN_N, $OLD=$EN_O) — the twin class"
[ "$n_active" -le 1 ] || fail $RC_NOT_SINGLETON TWIN "$n_active units active ($NEW=$AC_N, $OLD=$AC_O) — a second daemon loop is supervised; reap it"
[ "$n_active" -ge 1 ] || fail $RC_NOT_SINGLETON ORPHAN "no active unit after retiring $OLD — the daemon lost its supervisor; re-run enable-new"
case "$AC_N" in active|activating) ;; *)
  fail $RC_NOT_SINGLETON WRONG_UNIT "$OLD is the active unit, not $NEW" ;;
esac
emit_state; say "HSCC_RC: $RC_OK"; exit 0
"""


def node_script(mode: str, *, pkgs: str = "") -> str:
    """Build the node-side bash for *mode* (``probe``|``enable-new``|``retire-old``).

    The unit names and the recognised-word sets are substituted from this
    module's constants, so node and client cannot drift.
    """
    if mode not in ("probe", "enable-new", "retire-old"):
        raise ValueError(f"unknown mode {mode!r}")
    return (_SCRIPT_TEMPLATE
            .replace("__MODE__", mode)
            .replace("__NEW__", UNIT_NAME)
            .replace("__OLD__", LEGACY_UNIT_NAME)
            .replace("__PKGS__", pkgs.replace("'", "'\"'\"'"))
            .replace("__RC_NO_BUS__", str(RC_NO_BUS))
            .replace("__ENABLED_WORDS__", " ".join(sorted(ENABLED_WORDS)))
            .replace("__ACTIVE_WORDS__", " ".join(sorted(ACTIVE_WORDS))))


def command_for(mode: str, *, pkgs: str = "") -> str:
    """The exact remote command for *mode*: the script piped to bash.

    Base64 keeps it byte-exact through the transport (no heredoc nesting, no
    remote expansion), exactly like the batch's other installers.
    """
    b64 = base64.b64encode(node_script(mode, pkgs=pkgs).encode()).decode()
    return f"printf '%s' '{b64}' | base64 -d | bash -s"


# ── parsing the node's answer ───────────────────────────────────────────────
_LINE = re.compile(r"^HSCC_([A-Z_]+): (.*)$")


def _hscc_lines(stdout: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for raw in (stdout or "").splitlines():
        m = _LINE.match(raw.strip())
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def _one(lines: Dict[str, List[str]], key: str) -> str:
    v = lines.get(key)
    return v[0] if v else ""


def _parse_units(raw_lines: List[str]) -> Dict[str, Dict[str, str]]:
    """Parse ``HSCC_UNIT: name|enabled=..|active=..|mainpid=..`` lines."""
    out: Dict[str, Dict[str, str]] = {}
    for line in raw_lines:
        parts = line.split("|")
        if len(parts) < 4:
            continue
        kv: Dict[str, str] = {}
        for chunk in parts[1:]:
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                kv[k.strip()] = v.strip()
        out[parts[0].strip()] = {"enabled": kv.get("enabled", ""),
                                 "active": kv.get("active", ""),
                                 "mainpid": kv.get("mainpid", "")}
    return out


def _bad_word(units: Dict[str, Dict[str, str]]) -> str:
    """A state word outside systemd's vocabulary, or '' when all are valid.

    The node validates too; this is the second half of the contract — text that
    reached the client from a partially-working probe is still never treated as
    "no unit".
    """
    for name, u in units.items():
        if u["enabled"] and u["enabled"] not in ENABLED_WORDS:
            return (f"{name}: is-enabled reported {u['enabled']!r} — not a unit "
                    f"state (bus/environment fault)")
        if u["active"] and u["active"] not in ACTIVE_WORDS:
            return (f"{name}: is-active reported {u['active']!r} — not a unit "
                    f"state (bus/environment fault)")
    return ""


def _fault(lines: Dict[str, List[str]], res: Dict[str, Any]) -> str:
    """The honest fault text for a probe that produced no trustworthy state.

    Environment faults (no systemctl, no bus, no python) must never be read as
    "the node has no legacy unit" — that is how a migration script starts
    deleting things on a node it could not actually see.
    """
    code = _one(lines, "HSCC_CODE") or "NO_STATE"
    msg = _one(lines, "HSCC_MSG") or (res.get("stderr") or res.get("error")
                                      or "node produced no HSCC_RC")
    return f"[{code}] {msg}".strip()


Runners = Callable[[str, int], Dict[str, Any]]


def _run(ssh_cmd: Runners, command: str, timeout: int) -> Dict[str, Any]:
    res = ssh_cmd(command, timeout)
    if not isinstance(res, dict):
        return {"ok": False, "stdout": "", "stderr": "", "code": None,
                "error": f"ssh_cmd returned {type(res).__name__}, expected dict"}
    return {"ok": bool(res.get("ok")), "stdout": res.get("stdout") or "",
            "stderr": res.get("stderr") or "", "code": res.get("code"),
            "error": res.get("error") or ""}


def probe_node(ssh_cmd: Runners, *, pkgs: str = "",
               timeout: int = 60) -> Dict[str, Any]:
    """Read-only per-node unit state — the probe surface a doctor check reuses.

    ``{"ok": True, "units": {...}, "file_old": ..., ...}`` or
    ``{"ok": False, "fault": "..."}``. Never writes, never enables, never
    removes; a node whose state cannot be read is a FAULT, never "clean".
    """
    res = _run(ssh_cmd, command_for("probe", pkgs=pkgs), timeout)
    lines = _hscc_lines(res["stdout"])
    if _one(lines, "HSCC_RC") != "0":
        return {"ok": False, "fault": _fault(lines, res),
                "rc": _one(lines, "HSCC_RC")}
    units = _parse_units(lines.get("HSCC_UNIT", []))
    if not units:
        return {"ok": False,
                "fault": "probe returned rc 0 but no unit lines (probe "
                         "incomplete)"}
    bad = _bad_word(units)
    if bad:
        return {"ok": False, "fault": bad, "rc": str(RC_NO_BUS)}
    return {"ok": True, "units": units,
            "file_new": _one(lines, "HSCC_FILE_NEW"),
            "file_old": _one(lines, "HSCC_FILE_OLD"),
            "exec_new": _one(lines, "HSCC_EXEC_NEW"),
            "exec_old": _one(lines, "HSCC_EXEC_OLD"),
            "link_old": _one(lines, "HSCC_LINK_OLD")}


def has_legacy(state: Dict[str, Any]) -> bool:
    """True when the node still carries the retired unit in ANY form.

    "Any form" matters: a file with no `[Install]` section is `static` and can
    still be started by socket/timer activation, and a symlink override is not
    ours to delete. Either way the name is still live on that box.
    """
    u = state.get("units", {}).get(LEGACY_UNIT_NAME, {})
    return bool((u.get("enabled") and u["enabled"] != "not-found")
                or (u.get("active") and u["active"] not in ("inactive",
                                                            "dormant", "failed"))
                or (state.get("file_old") or "absent") != "absent")


def canonical_healthy(state: Dict[str, Any]) -> bool:
    """True when the canonical unit is enabled, active, and reports a pid."""
    u = state.get("units", {}).get(UNIT_NAME, {})
    return (u.get("enabled") in ENABLED_ON
            and u.get("active") in ACTIVE_ON
            and u.get("mainpid") not in ("", "0", None))


def verify_final(state: Dict[str, Any]) -> str:
    """'' when the node is exactly-right; otherwise the defect in words.

    The whole point of the card expressed as a predicate: never zero, never two.
    """
    units = state.get("units", {})
    new = units.get(UNIT_NAME, {})
    old = units.get(LEGACY_UNIT_NAME, {})
    enabled = [n for n, u in ((UNIT_NAME, new), (LEGACY_UNIT_NAME, old))
               if u.get("enabled") in ENABLED_ON]
    active = [n for n, u in ((UNIT_NAME, new), (LEGACY_UNIT_NAME, old))
              if u.get("active") in ACTIVE_ON]
    if len(active) > 1:
        return f"two active units ({', '.join(active)}) — a supervised twin is live"
    if len(enabled) > 1:
        return f"two enabled units ({', '.join(enabled)}) — the twin class"
    if not active:
        return "no active unit — the daemon lost its supervisor"
    if UNIT_NAME not in active:
        return f"{LEGACY_UNIT_NAME} is the active unit, not {UNIT_NAME}"
    if new.get("mainpid") in ("", "0"):
        return f"{UNIT_NAME} reports no MainPID"
    return ""


# ── per-node migration ──────────────────────────────────────────────────────
def _slim(state: Dict[str, Any]) -> Dict[str, Any]:
    """The report shape: names, words, pids — no raw transport noise."""
    return {"units": state.get("units", {}),
            "file_new": state.get("file_new", ""),
            "file_old": state.get("file_old", ""),
            "exec_new": state.get("exec_new", ""),
            "exec_old": state.get("exec_old", "")}


def _plan_text(state: Dict[str, Any]) -> str:
    units = state.get("units", {})
    rows = " || ".join(
        f"{n}: enabled={units.get(n, {}).get('enabled') or 'no-unit'} "
        f"active={units.get(n, {}).get('active') or 'no-unit'} "
        f"MainPID={units.get(n, {}).get('mainpid') or '-'}"
        for n in (UNIT_NAME, LEGACY_UNIT_NAME))
    if not has_legacy(state) and canonical_healthy(state):
        verb = (f"nothing — {UNIT_NAME} is already enabled+active and "
                f"{LEGACY_UNIT_NAME} is absent")
    else:
        verb = (f"write {UNIT_NAME} with the node's own emitter, enable --now, "
                f"read back enabled+active with a MainPID, ONLY THEN disable "
                f"--now + remove {LEGACY_UNIT_NAME}, then re-read the final "
                f"state (never zero active, never two)")
    return f"{verb} || {rows}"


def migrate_node(node: str, *, ssh_cmd: Runners, dry_run: bool = False,
                 timeout: int = 120, pkgs: str = "") -> Dict[str, Any]:
    """Converge ONE node onto the canonical unit name. Never raises.

    ``ssh_cmd(command, timeout)`` is the injected transport; the caller binds the
    node into it (production: :func:`_default_ssh`; tests: a fabricated
    responder). Nothing here opens a socket, so no test can touch the fleet.
    """
    try:
        return _migrate(node, ssh_cmd=ssh_cmd, dry_run=dry_run,
                        timeout=timeout, pkgs=pkgs)
    except Exception as e:  # a per-node fault must not abort a fleet pass
        return NodeResult(node, FAILED,
                          message=f"{node}: unexpected {type(e).__name__}: {e}"
                          ).to_dict()


def _migrate(node: str, *, ssh_cmd: Runners, dry_run: bool, timeout: int,
             pkgs: str) -> Dict[str, Any]:
    before = probe_node(ssh_cmd, pkgs=pkgs, timeout=timeout)
    if not before.get("ok"):
        return NodeResult(node, FAILED,
                          message=f"{node}: UNVERIFIABLE — {before.get('fault')}; "
                                  f"refusing to change unit state on a node we "
                                  f"cannot read").to_dict()

    legacy = has_legacy(before)
    canonical = canonical_healthy(before)

    if dry_run:
        plan = _plan_text(before)
        return NodeResult(node, DRY_RUN, before=_slim(before),
                          message=f"{node}: {plan}", plan=plan).to_dict()

    if not legacy and canonical:
        pid = before["units"][UNIT_NAME]["mainpid"]
        return NodeResult(node, OK_ALREADY, before=_slim(before),
                          after=_slim(before),
                          message=f"{node}: already on {UNIT_NAME} (MainPID "
                                  f"{pid}); no {LEGACY_UNIT_NAME} to retire"
                          ).to_dict()

    actions: List[str] = []

    # 1. Enable the canonical unit. It writes the file with the node's own
    # emitter and refuses to go further if the unit does not come up supervised.
    r = _run(ssh_cmd, command_for("enable-new", pkgs=pkgs), timeout)
    lines = _hscc_lines(r["stdout"])
    if _one(lines, "HSCC_RC") != "0":
        # enable-new stops before touching the legacy unit on any failure, so the
        # node keeps whatever supervision it had. Say exactly that.
        return NodeResult(node, FAILED, actions=actions, before=_slim(before),
                          message=f"{node}: {_fault(lines, r)} — "
                                  f"{LEGACY_UNIT_NAME} left as it was (no "
                                  f"supervision lost)").to_dict()
    actions.append(f"enabled {UNIT_NAME} (MainPID "
                   f"{_one(lines, 'HSCC_NEW_PID') or '?'})")

    # 2. Retire the legacy unit — only now that the canonical one is supervised.
    if legacy:
        r2 = _run(ssh_cmd, command_for("retire-old", pkgs=pkgs), timeout)
        lines2 = _hscc_lines(r2["stdout"])
        code2 = _one(lines2, "HSCC_CODE") or "RETIRE_FAILED"
        if _one(lines2, "HSCC_RC") != "0":
            after = probe_node(ssh_cmd, pkgs=pkgs, timeout=timeout)
            # TWIN / ORPHAN / NOT_SINGLETON all mean "the state we must never
            # ship"; anything else is the verb itself failing.
            outcome = TWIN_STATE if code2 in ("TWIN", "ORPHAN",
                                              "NOT_SINGLETON") else FAILED
            return NodeResult(node, outcome, actions=actions,
                              before=_slim(before),
                              after=_slim(after) if after.get("ok") else {},
                              message=f"{node}: {code2} — {_fault(lines2, r2)}"
                                      ).to_dict()
        actions.append(f"retired {LEGACY_UNIT_NAME}")
        if _one(lines2, "HSCC_KEPT_LINK"):
            actions.append("left a foreign override symlink in place "
                           "(operator removes by hand)")

    # 3. FINAL read-back from the node. The arbiter is the node's own answer, not
    # the verbs' exit codes.
    after = probe_node(ssh_cmd, pkgs=pkgs, timeout=timeout)
    if not after.get("ok"):
        return NodeResult(node, FAILED, actions=actions, before=_slim(before),
                          message=f"{node}: changes were written but the final "
                                  f"read-back failed ({after.get('fault')}) — "
                                  f"VERIFY BY HAND").to_dict()
    defect = verify_final(after)
    if defect:
        twin = defect.startswith("two")
        return NodeResult(node, TWIN_STATE if twin else FAILED,
                          actions=actions, before=_slim(before),
                          after=_slim(after),
                          message=f"{node}: {defect} — VERIFY BY HAND").to_dict()
    actions.append("verified: exactly one unit enabled + active")
    pid = after["units"][UNIT_NAME]["mainpid"]
    return NodeResult(node, OK_MIGRATED, actions=actions, before=_slim(before),
                      after=_slim(after),
                      message=f"{node}: {UNIT_NAME} enabled+active (MainPID "
                              f"{pid}); {LEGACY_UNIT_NAME} retired").to_dict()


# ── fleet pass + report ─────────────────────────────────────────────────────
def migrate_fleet(nodes: List[str], *, ssh_factory: Callable[[str], Runners],
                  dry_run: bool = False, pkgs: str = "") -> Dict[str, Any]:
    results = [migrate_node(n, ssh_cmd=ssh_factory(n), dry_run=dry_run,
                            pkgs=pkgs) for n in nodes]
    done = [r for r in results if r["outcome"] in OK_OUTCOMES]
    return {"nodes": results, "dry_run": dry_run,
            "unit_name": UNIT_NAME, "legacy_unit_name": LEGACY_UNIT_NAME,
            "ok": bool(results) and len(done) == len(results),
            "migrated": [r["node"] for r in results
                         if r["outcome"] == OK_MIGRATED],
            "already": [r["node"] for r in results
                        if r["outcome"] == OK_ALREADY],
            "twins": [r["node"] for r in results
                      if r["outcome"] == TWIN_STATE],
            "failed": [r["node"] for r in results if r["outcome"] == FAILED]}


_TAG = {OK_MIGRATED: "OK", OK_ALREADY: "OK", DRY_RUN: "PLAN",
        TWIN_STATE: "TWIN", FAILED: "FAIL", SKIPPED: "SKIP"}


def format_report(summary: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for r in summary["nodes"]:
        out.append(f"{_TAG.get(r['outcome'], 'FAIL')}|{r['node']}: {r['message']}")
        for a in r["actions"]:
            out.append(f"   . {a}")
    if summary["dry_run"]:
        out.append(f"DRY RUN — nothing written on {len(summary['nodes'])} node(s).")
    return out


def bootstrap_lines(summary: Dict[str, Any]) -> List[str]:
    """``OK|``/``WARN|`` lines for a bootstrap-style summariser."""
    n = len(summary["nodes"])
    if summary["dry_run"]:
        return [f"OK|systemd unit-name migration dry-run: {n} node(s) planned, "
                f"nothing written"]
    if not n:
        return ["WARN|no nodes were checked — is the sparkrun cluster configured?"]
    settled = len(summary["migrated"]) + len(summary["already"])
    out = [f"{'OK' if summary['ok'] else 'WARN'}|systemd --user daemon unit name "
           f"unified on {settled}/{n} node(s) — one unit, {UNIT_NAME}"]
    for r in summary["nodes"]:
        if r["outcome"] in OK_OUTCOMES:
            out.append(f"OK|{r['node']}: {r['message']}")
        else:
            out.append(f"WARN|{r['node']} [{r['outcome']}]: {r['message']}")
    if summary["twins"]:
        out.append(f"WARN|possible twin on {', '.join(summary['twins'])} — more "
                   f"than one unit is enabled or active, so the daemon may have "
                   f"two supervisors. Reap before trusting any restart.")
    return out


# ── production transport + CLI ──────────────────────────────────────────────
def _default_ssh(host: str, command: str, timeout: int = 120) -> Dict[str, Any]:
    """Fleet transport: ``hscc-cluster.clusterlib.ssh_cmd`` (sparkrun owns the fleet).

    The same sanctioned resolution ``install_notifier_mask._default_ssh`` uses.
    NO hand-rolled ssh. If the bridge cannot be imported that is an honest
    failure for every node, never a fabricated ok.
    """
    boot_dir = os.path.dirname(os.path.abspath(__file__))
    cluster_dir = os.path.join(os.path.dirname(boot_dir), "hscc-cluster")
    if cluster_dir not in sys.path:
        sys.path.insert(0, cluster_dir)
    try:
        import clusterlib  # noqa: PLC0415 - late on purpose (path set above)
    except ImportError as e:  # pragma: no cover - layout must provide it
        return {"ok": False, "stdout": "", "code": 127,
                "stderr": f"clusterlib unavailable ({e}); sparkrun bridge unreachable"}
    return clusterlib.ssh_cmd(host, command, timeout=timeout)


def _hosts_from_detect() -> List[str]:
    """Cluster hosts via detect.py (sparkrun cluster list) — bootstrap's own path."""
    boot_dir = os.path.dirname(os.path.abspath(__file__))
    if boot_dir not in sys.path:
        sys.path.insert(0, boot_dir)
    try:
        import detect  # noqa: PLC0415
    except ImportError:
        return []
    cluster = detect.detect_cluster()
    return list(cluster.get("hosts") or []) if cluster else []


def _pkgs_from_args(argv: List[str]) -> str:
    """PYTHONPATH for the node-side emitter.

    The migration targets the DEPLOYED runtime copy, so the default is
    ``~/.hermes/plugins`` (where ``hscc_daemon/`` sits on a node). ``--pkgs``
    overrides it for a box that runs from a checkout.
    """
    if "--pkgs" in argv:
        i = argv.index("--pkgs")
        if i + 1 < len(argv):
            return argv[i + 1]
    return os.path.expanduser("~/.hermes/plugins")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI for the operator. Read-only unless you drop ``--dry-run``.

    ``--dry-run`` writes nothing anywhere and prints the per-node plan — the
    shape to review before scheduling the real run. ``--hosts a,b`` names nodes
    (default: ``sparkrun cluster list``). ``--json`` / ``--bootstrap-lines`` pick
    the report shape. There is no flag that changes the enable-new-before-retire
    ordering: that ordering is the safety property.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    as_json = "--json" in argv
    as_lines = "--bootstrap-lines" in argv
    hosts: List[str] = []
    if "--hosts" in argv:
        i = argv.index("--hosts")
        for tok in argv[i + 1:]:
            if tok.startswith("--"):
                break
            hosts.extend(h for h in tok.split(",") if h)
    if not hosts:
        hosts = _hosts_from_detect()
    if not hosts:
        print(json.dumps({"nodes": [], "ok": False,
                          "error": "no hosts: pass --hosts or configure a "
                                   "sparkrun cluster"}))
        return 2
    summary = migrate_fleet(hosts, ssh_factory=lambda h: (
        lambda cmd, to, _host=h: _default_ssh(_host, cmd, to)),
        dry_run=dry_run, pkgs=_pkgs_from_args(argv))
    if as_lines:
        print("\n".join(bootstrap_lines(summary)))
    else:
        print(json.dumps(summary, indent=2) if as_json
              else "\n".join(format_report(summary)))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
