"""Mask snap firmware-updater's notifier user service on every cluster node (t_61850b24).

Node-recovery batch part 5. Context: the GB10 Sparks run ``firmware-updater`` as
a *snap*, and that snap ships a desktop notifier that lives in the desktop
user's ``systemd --user`` session. It fails on these headless-ish nodes with
``error: timeout waiting for snap system profiles to get updated``, and because
its unit carries ``Restart=on-failure`` with ``RestartUSec=100ms``, systemd
restarts it about every five seconds forever. Measured on the fleet (read-only,
via the sparkrun bridge, 2026-10-28): ``NRestarts`` 4177 / 5920 / 5924 on three
nodes, and 5976 ``Main process exited`` journal lines in the last 24 h on one
node alone.

WHY THIS IS NOT A CAUSAL CLAIM
------------------------------
Whether that restart storm has any part in the ``.244`` hard hang of 2026-10-27
is **unproven**, and nothing in this module, its tests, or its report asserts
that it does. What is measured and provable: the loop is real, it is enormous,
it is pure log/process churn, and masking one user unit stops it.

MASK, NOT DISABLE — the decision and the reason
-----------------------------------------------
``disable`` only removes the symlinks in ``*.wants/`` / ``*.target.wants/``
directories: it changes *who pulls a unit in*, not whether the unit can load.
Two measured facts make it the wrong lever here. First, the notifier's fragment
carries **no ``[Install]`` section at all** (``UnitFileState=static``), so
``disable`` has nothing to unlink — on this fleet it is measurably a no-op that
still exits 0. Second, even where a ``[Install]`` section exists, snapd
regenerates the fragment on every snap refresh (the file header says ``#
Auto-generated, DO NOT EDIT``, and its ``WorkingDirectory=/var/snap/
firmware-updater/<revision>`` names the revision directory that changes on
refresh), so a ``disable`` is silently undone the first time snapd refreshes the
package — a fix that reads green today and evaporates next week.

``mask`` is a different mechanism: it places a symlink
``~/.config/systemd/user/<unit> -> /dev/null`` in the user's own unit search
path, and systemd refuses to **load** a unit whose name resolves to ``/dev/null``
— whoever asks, whether the ask comes from a timer, from ``snap run``, from the
generator's ``[Install]`` section, or from a human typing ``systemctl --user
start``. snapd regenerates the *fragment* under ``/etc/xdg/systemd/user``; the
mask sits at a different path and masks the unit *name*, so refreshes overwrite
the fragment and leave the mask intact. The undo is explicit: ``--unmask``.

Because a mask persists across refreshes *and* reboots, it must never be able to
silently disappear and still read as protection — so this card also owns a
doctor check (``doctor_notifier_mask.py``) that reads the mask back from every
node and goes red when it is not there.

SCOPE — deliberately narrow
---------------------------
The masked set is the notifier **service** plus the **timer that exists only to
start it** (measured: the timer's only property of consequence is
``Unit=snap.firmware-updater.firmware-notifier.service``). The timer is included
because a live timer pointed at a masked unit produces a fresh failed job on
every firing (8 ``OnCalendar`` entries a day) instead of the 5-second storm —
which would trade one kind of log spam for another while leaving the same unit
half-managed. Everything else is untouched: the ``firmware-updater`` SNAP stays
installed and refreshable (``fwupd`` updates are still wanted and the operator
runs them by hand — measured ``fwupd`` active on every node), and
``snap.firmware-updater.firmware-updater-app.service`` (the snap's own app
service) is left exactly as snapd wrote it.

NO ROOT, NO PRIVILEGE ESCALATION
--------------------------------
The mask lives in the desktop user's own home, so this is an unprivileged
operation for the ssh user. There is no ``sudo`` anywhere in this module — the
failure mode this module must never produce is a root escalation on 4 nodes to
stop a user-session notifier.

THE BUS PITFALL (the reason this module exists in the batch's idiom)
--------------------------------------------------------------------
``systemctl --user`` needs the user's D-Bus session. Over a plain ssh command
line the classic failure is ``Failed to connect to bus`` — measured here
verbatim with ``env -i`` as ``Failed to connect to bus: No medium found``
(rc=1). On the current fleet the PAM login session already exports both
``XDG_RUNTIME_DIR=/run/user/<uid>`` and
``DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus``, and the socket is
present, so the bootstrap is a no-op today. It is still performed at the top of
the node-side script, in ``${VAR:-derived}`` form — a no-op when the session set
them, a correction when it did not (a ProxyCommand path, a non-PAM transport, or
any future sshd config), so the command does not depend on a PAM accident.
``sudo -u <user>`` is deliberately NOT used: the ssh user already *is* the
desktop user, and switching users would drop the bus environment again.

HONEST SIGNAL: an environment fault is never a data fact
--------------------------------------------------------
A node that answers nothing, a ``systemctl`` that cannot reach the bus, or a
probe that dies before emitting ``HSCC_RC`` comes back as ``error`` naming the
node and the reason — never as ``masked``, never as "not masked", never as a
silent skip. The no-bus failure is specifically an *environment* fault, and the
doctor renders it as "UNVERIFIABLE … NOT verified", never as "not masked": an
operator who cannot tell a broken node from an unmasked one has no actionable
output. (Same rule as ``hscc_daemon/health.py``'s ``HermesRuntimeUnavailable``
and the sibling doctor card.)

Transport: the existing sparkrun ssh path, nothing else — the same sanctioned
resolution ``install_autoreboot._default_ssh`` uses (resolve the sibling
``hscc-cluster`` dir, call ``clusterlib.ssh_cmd``, which delegates to
``sparkrun_bridge.remote_cmd`` → sparkrun's own ``run_remote_script`` with
sparkrun's ssh kwargs). No hand-rolled ``ssh`` anywhere; tests inject a fake at
the ``_ssh`` seam and no test opens a socket or contacts a node.

Opt-out: ``~/.hscc/cluster.json`` ->
``{"node_recovery": {"firmware_notifier_mask": false}}`` (default ON when the
block or subkey is absent). ``node_recovery`` is the batch's shared block, one
subkey per layer, read independently — the watchdog card's ``watchdog`` and the
sysctl card's ``autoreboot`` can neither enable nor disable this layer, and an
unknown subkey is ignored rather than fatal. A non-boolean value is NOT allowed
to silently flip the feature: the default (ON) is kept and the problem is named
in ``config_error``, because a silently-dead opt-out looks exactly like a
working one.

Doctor contract (own check, this card): reuse :func:`probe_node`,
:func:`is_masked_state`, :data:`MASKED_OUTCOMES`, :data:`NOTIFIER_UNITS` and
:data:`MANAGED_STATE_WORDS` rather than re-declaring a unit name or a second
notion of "masked".
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# ── what gets masked ────────────────────────────────────────────────────────
#: The unit the crash-loop lives in. ONE definition: the installer, the probe
#: and the doctor all read this tuple.
NOTIFIER_SERVICE = "snap.firmware-updater.firmware-notifier.service"

#: The timer whose ONLY action is to start that service (measured
#: ``Unit=snap.firmware-updater.firmware-notifier.service``, 8 ``OnCalendar``
#: firings a day). Masking the service but not its timer leaves a live timer
#: failing 8 jobs a day against a unit that can never load.
NOTIFIER_TIMER = "snap.firmware-updater.firmware-notifier.timer"

NOTIFIER_UNITS: tuple[str, ...] = (NOTIFIER_SERVICE, NOTIFIER_TIMER)

#: The mask target directory as systemd resolves it (measured first entry of
#: ``systemctl --user show -p UnitPath`` after the generator dirs:
#: ``/home/<user>/.config/systemd/user``). Diagnostic only — the authoritative
#: read-back is ``systemctl --user is-enabled`` reporting ``masked``.
MASK_DIR_TEMPLATE = "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

#: Every word ``systemctl --user is-enabled`` can legitimately print on this
#: fleet's systemd (255). Anything outside this set is NOT a state — it is
#: stderr-shaped text (``Failed to connect to bus: …``), and the caller must
#: treat it as an environment fault. Single source for the node-side bash
#: matcher and the Python parser, so installer and doctor cannot disagree.
MANAGED_STATE_WORDS: frozenset[str] = frozenset({
    "enabled", "enabled-runtime", "static", "static-runtime", "indirect",
    "masked", "masked-runtime", "generated", "generated-runtime", "transient",
    "disabled", "bad-setting", "error", "alias", "linked", "linked-runtime",
    "runtime", "not-found",
})

#: The words that mean "this unit cannot load". ``masked-runtime`` is a mask
#: made with ``--runtime`` (vanishes on reboot) — it stops the loop today, so it
#: counts as masked, and the doctor notes it as such rather than pretending it
#: is the persistent form.
MASKED_WORDS: frozenset[str] = frozenset({"masked", "masked-runtime"})

#: The opt-out block/subkey. Batch contract: ``node_recovery`` is the shared
#: block, one subkey per layer, read independently by each layer.
NODE_RECOVERY_KEY = "node_recovery"
MASK_SUBKEY = "firmware_notifier_mask"

# ── per-node outcome vocabulary (mirrors hscc-cluster/node_recovery.py) ─────
#   applied / unchanged      -> the node IS quarantined
#   skipped_disabled         -> intentional opt-out (not a fault, not green)
#   drift / error            -> the node is NOT quarantined; act on it
OK_APPLIED = "applied"
OK_UNCHANGED = "unchanged"
SKIP_DISABLED = "skipped_disabled"
DRIFT = "drift"
ERROR = "error"

#: Outcomes that mean "this node's notifier cannot run". Everything else does
#: not, so no branch can promote a fault into protection.
MASKED_OUTCOMES = frozenset({OK_APPLIED, OK_UNCHANGED})

# ── node-side exit codes (HSCC_RC) ─────────────────────────────────────────
# Kept named so tests and the doctor can cite a failure without parsing prose.
# These are OUR codes, emitted as the last `HSCC_RC:` line: `systemctl
# is-enabled` exits 1/4 for legitimate states (masked / not-found), so
# systemctl's own rc is NEVER load-bearing here — only the printed word is.
RC_OK = 0
RC_BAD_MODE = 2
RC_NO_SYSTEMD = 3
RC_NO_BUS = 4
RC_NO_HOME = 5
RC_MASK_FAILED = 6
RC_DRIFT = 7
RC_NOT_MASKED = 8
RC_STILL_RUNNING = 9

Runners = Callable[[str, int], Dict[str, Any]]


@dataclass(frozen=True)
class UnitPaths:
    """Where the mask lives on the node. A seam, not a feature.

    Production always uses the defaults (the real paths); the bash-backed tests
    point ``mask_dir`` at a sandbox because macOS has no
    ``~/.config/systemd/user`` semantics to hang a fake systemd on. The doctor
    must use the defaults too, so installer and doctor cannot drift.
    """
    mask_dir: str = ""          # empty => the node resolves $HOME/.config itself


DEFAULT_PATHS = UnitPaths()


def is_masked_state(state: Dict[str, Any]) -> bool:
    """Does a probe state dict say EVERY managed unit is masked?

    The one definition of "masked", shared by installer and doctor. Unknown or
    missing state is False — never a guess, and never "all three units are
    masked" because the first one was.
    """
    units = state.get("units") or []
    if not units:
        return False
    return all((u.get("state") or "").strip() in MASKED_WORDS for u in units)


def is_bus_ok_state(state: Dict[str, Any]) -> bool:
    """Did the node actually answer as a session it could reach?

    False means the probe never produced a state — an environment fault, which
    must never be rendered as "not masked".
    """
    return bool(state.get("transport_ok"))


# ── config / opt-out ────────────────────────────────────────────────────────

def hscc_dir() -> str:
    """HSCC state dir, env-resolved (never a baked ``~/.hscc``)."""
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def cluster_config_path() -> str:
    """The cluster config that carries the ``node_recovery`` block."""
    return os.path.join(hscc_dir(), "cluster.json")


def notifier_mask_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Read ``node_recovery.firmware_notifier_mask``; **default ON** when absent.

    Returns ``{"firmware_notifier_mask": bool, "config_error": str}``. Own
    reader, own subkey — the batch rule is one subkey per layer read
    independently, so no sibling card can flip this one and this one cannot
    flip theirs. A non-boolean value keeps the default (ON) and is named in
    ``config_error``: an operator who typed ``"false"`` must be told, not
    quietly served a feature they think they switched off.
    """
    out: Dict[str, Any] = {MASK_SUBKEY: True, "config_error": ""}
    target = path or cluster_config_path()
    try:
        with open(target) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return out
    except (json.JSONDecodeError, OSError, IsADirectoryError) as e:
        # Present-but-unreadable is an environment fault, not "no config".
        out["config_error"] = (f"could not read {target} ({e.__class__.__name__}: {e}) "
                               f"— defaults applied ({MASK_SUBKEY} ON)")
        return out
    if not isinstance(cfg, dict):
        out["config_error"] = (f"{target} is not a JSON object — defaults applied "
                               f"({MASK_SUBKEY} ON)")
        return out
    block = cfg.get(NODE_RECOVERY_KEY)
    if block is None:
        return out
    if not isinstance(block, dict):
        out["config_error"] = (f"{NODE_RECOVERY_KEY} must be a mapping, got "
                               f"{type(block).__name__} — defaults applied "
                               f"({MASK_SUBKEY} ON)")
        return out
    if MASK_SUBKEY in block:
        val = block[MASK_SUBKEY]
        if isinstance(val, bool):
            out[MASK_SUBKEY] = val
        else:
            out["config_error"] = (f"{NODE_RECOVERY_KEY}.{MASK_SUBKEY}={val!r} is not "
                                   f"true/false — kept ON")
    return out


def notifier_mask_enabled(path: Optional[str] = None) -> bool:
    """Is the notifier-mask layer enabled for this fleet? (convenience wrapper)"""
    return bool(notifier_mask_config(path)[MASK_SUBKEY])


# ── the node-side script ────────────────────────────────────────────────────
# Shipped base64-encoded in HSCC_SCRIPT_B64 and written to a temp file by the
# one-line bootstrap below, so the script body never has to survive a second
# round of shell quoting from the transport (the bridge already pipes a heredoc;
# nesting a second unquoted one would be expanded remotely).
#
# Contract (shared with the doctor): stdout lines are `KEY: value`; the LAST
# line is always `HSCC_RC: <n>` and is OUR exit code, not systemctl's. A
# missing HSCC_RC therefore means the script itself died — a fault, not a state.
_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# HSCC node-recovery snap-notifier mask — GENERATED by install_notifier_mask.py.
# Do not edit on the node; edit the module. Modes: state | install | uninstall
set -u
MODE="__MODE__"
UNITS=( __UNITS__ )
MASK_DIR="__MASK_DIR__"
# Recognised `systemctl --user is-enabled` answers (same list as the Python
# module's MANAGED_STATE_WORDS — one source, built into both sides).
WORDS=" __WORDS__ "

say() { printf '%s\n' "$1"; }
fail() { say "HSCC_CODE: $2"; say "HSCC_MSG: $3"; say "HSCC_RC: $1"; exit "$1"; }

# ── the user bus: derived, not assumed ──────────────────────────────────────
# A PAM login session normally exports both of these already (measured on every
# cluster node), so these two lines are a no-op today. They are NOT optional:
# without them `systemctl --user` dies with "Failed to connect to bus", and the
# `${VAR:-derived}` form keeps whatever the session set while correcting a
# transport that did not. The runtime dir is derived from OUR uid, so nothing
# here hardcodes a user or an id.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

if ! command -v systemctl >/dev/null 2>&1; then
  fail __RC_NO_SYSTEMD__ NO_SYSTEMD "systemctl not on PATH; cannot read or mask user units here"
fi
if [ -z "${HOME:-}" ]; then
  fail __RC_NO_HOME__ NO_HOME "HOME is unset, so the user unit search path cannot be resolved"
fi

# is_enabled_word <unit> — print the state WORD, or the error text.
# The exit status of `is-enabled` is deliberately swallowed: rc=1 (masked) and
# rc=4 (not-found) are both legitimate answers, and conflating them with our own
# HSCC_RC is exactly how a healthy node gets reported as a failure.
is_enabled_word() { systemctl --user is-enabled "$1" 2>&1 || true; }
is_active_word()  { systemctl --user is-active  "$1" 2>&1 || true; }
mask_link()       { readlink "${MASK_DIR}/$1" 2>/dev/null || true; }

# bus_alive — one cheap round trip. Anything that is not a recognised state
# word is treated as "the bus is not reachable", which is an ENVIRONMENT fault
# and never a statement about the unit.
bus_alive() {
  local w
  w="$(is_enabled_word "${UNITS[0]}")"
  case " $WORDS " in
    *" $w "*) return 0 ;;
    *) say "HSCC_BUS_TEXT: $w"; return 1 ;;
  esac
}

emit_state() {
  say "HSCC_STATE_BEGIN: 1"
  say "HSCC_MASK_DIR: $MASK_DIR"
  say "HSCC_UID: $(id -u 2>/dev/null)"
  say "HSCC_XDG_RUNTIME_DIR: $XDG_RUNTIME_DIR"
  local i=0 w a l
  for u in "${UNITS[@]}"; do
    w="$(is_enabled_word "$u")"
    a="$(is_active_word "$u")"
    l="$(mask_link "$u")"
    say "HSCC_UNIT_$i: $u"
    say "HSCC_UNIT_${i}_STATE: $w"
    say "HSCC_UNIT_${i}_ACTIVE: $a"
    say "HSCC_UNIT_${i}_LINK: $l"
    i=$((i + 1))
  done
  say "HSCC_UNIT_COUNT: $i"
  say "HSCC_STATE_END: 1"
}

case "$MODE" in
  state)
    bus_alive || fail __RC_NO_BUS__ NO_BUS "cannot reach the user manager bus (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR); unit state UNREADABLE — this is not 'not masked'"
    emit_state; say "HSCC_RC: 0"; exit 0
    ;;
  install|uninstall) ;;
  *) fail __RC_BAD_MODE__ BAD_MODE "unknown mode '$MODE'" ;;
esac

bus_alive || fail __RC_NO_BUS__ NO_BUS "cannot reach the user manager bus (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR); refusing to guess unit state"

if [ "$MODE" = "uninstall" ]; then
  # Explicit undo. Refuses a mask link that is NOT ours (someone else's
  # override is their business), and says plainly that the crash-loop returns
  # once the unit can load again.
  REMOVED=0; KEPT=0
  for u in "${UNITS[@]}"; do
    l="$(mask_link "$u")"
    w="$(is_enabled_word "$u")"
    if [ "$w" != "masked" ] && [ "$w" != "masked-runtime" ]; then
      KEPT=$((KEPT + 1)); say "HSCC_SKIP: $u ($w — not masked, nothing to undo)"; continue
    fi
    if [ -n "$l" ] && [ "$l" != "/dev/null" ]; then
      fail __RC_DRIFT__ DRIFT_FOREIGN "${MASK_DIR}/$u points at '$l', not /dev/null — that mask is not HSCC's; refusing to remove it"
    fi
    OUT="$(systemctl --user unmask "$u" 2>&1)"; RC=$?
    [ -n "$OUT" ] && say "HSCC_UNMASK_OUT: $OUT"
    [ "$RC" -ne 0 ] && fail __RC_MASK_FAILED__ UNMASK_FAILED "systemctl --user unmask $u failed (rc=$RC): $OUT"
    REMOVED=$((REMOVED + 1))
  done
  say "HSCC_ACTION: unmasked_$REMOVED"
  say "HSCC_NOTE: the notifier can start again — snapd's timer will restart the crash-loop if the unit still fails"
  emit_state
  say "HSCC_RC: 0"
  exit 0
fi

# ── install: mask each managed unit, never clobber a foreign link ──────────
CHANGED=0; UNCHANGED=0
for u in "${UNITS[@]}"; do
  w="$(is_enabled_word "$u")"
  if [ "$w" = "masked" ] || [ "$w" = "masked-runtime" ]; then
    UNCHANGED=$((UNCHANGED + 1)); say "HSCC_ALREADY: $u ($w)"
    continue
  fi
  l="$(mask_link "$u")"
  if [ -n "$l" ] && [ "$l" != "/dev/null" ]; then
    fail __RC_DRIFT__ DRIFT_FOREIGN "${MASK_DIR}/$u already exists pointing at '$l' — a user unit override we did not write; left untouched. Remove it to let HSCC manage this unit, or set ${NODE_RECOVERY}.${MASK_SUBKEY}=false."
  fi
  # `--now` also stops the currently-running instance (masking alone only
  # prevents the NEXT load, and the whole point is to stop a loop that is
  # running right now). Older systemd without `--now` on mask falls back to
  # mask + explicit stop, so the behaviour does not depend on version.
  OUT="$(systemctl --user mask --now "$u" 2>&1)"; RC=$?
  if [ "$RC" -ne 0 ]; then
    say "HSCC_MASK_NOW_UNSUPPORTED: $OUT"
    OUT="$(systemctl --user mask "$u" 2>&1)"; RC=$?
    [ "$RC" -ne 0 ] && fail __RC_MASK_FAILED__ MASK_FAILED "systemctl --user mask $u failed (rc=$RC): $OUT"
    OUT="$(systemctl --user stop --ignore-dependencies "$u" 2>&1)"; RC=$?
    [ "$RC" -ne 0 ] && say "HSCC_STOP_OUT: $RC: $OUT"
  fi
  CHANGED=$((CHANGED + 1))
  say "HSCC_MASKED: $u"
done

emit_state

# READ-BACK from the node — the arbiter. A mask command that reported success
# but did not make `is-enabled` say `masked` is a fault, and a masked unit that
# is still active means the loop has not stopped yet: neither is protection.
i=0
for u in "${UNITS[@]}"; do
  w="$(is_enabled_word "$u")"
  case " $WORDS " in
    *" $w "*) ;;
    *) fail __RC_NO_BUS__ NO_BUS "state of $u unreadable ('$w') after masking — environment fault, NOT verified" ;;
  esac
  if [ "$w" != "masked" ] && [ "$w" != "masked-runtime" ]; then
    fail __RC_NOT_MASKED__ NOT_MASKED "after mask, systemctl --user is-enabled $u still says '$w'"
  fi
  a="$(is_active_word "$u")"
  if [ "$a" = "active" ] || [ "$a" = "activating" ]; then
    fail __RC_STILL_RUNNING__ STILL_RUNNING "$u is masked but still $a — the restart loop has not stopped yet (the user session must restart, or the stop was refused)"
  fi
  i=$((i + 1))
done

if [ "$CHANGED" -eq 0 ]; then say "HSCC_ACTION: unchanged"; else say "HSCC_ACTION: applied"; fi
say "HSCC_RC: 0"
exit 0
"""


def install_script(mode: str, *, paths: UnitPaths = DEFAULT_PATHS) -> str:
    """Build the node-side bash for *mode* (``state``|``install``|``uninstall``).

    The unit list and the recognised-state word list are interpolated from this
    module's own constants, so the node-side matcher and the Python parser are
    built from one source and cannot drift.
    """
    if mode not in ("state", "install", "uninstall"):
        raise ValueError(f"unknown mode {mode!r}")
    mask_dir = paths.mask_dir or MASK_DIR_TEMPLATE
    return (_SCRIPT_TEMPLATE
            .replace("${NODE_RECOVERY}.${MASK_SUBKEY}",
                     f"{NODE_RECOVERY_KEY}.{MASK_SUBKEY}")
            .replace("__MODE__", mode)
            .replace("__UNITS__", " ".join(NOTIFIER_UNITS))
            .replace("__MASK_DIR__", mask_dir)
            .replace("__WORDS__", " ".join(sorted(MANAGED_STATE_WORDS)))
            .replace("__RC_BAD_MODE__", str(RC_BAD_MODE))
            .replace("__RC_NO_SYSTEMD__", str(RC_NO_SYSTEMD))
            .replace("__RC_NO_BUS__", str(RC_NO_BUS))
            .replace("__RC_NO_HOME__", str(RC_NO_HOME))
            .replace("__RC_MASK_FAILED__", str(RC_MASK_FAILED))
            .replace("__RC_DRIFT__", str(RC_DRIFT))
            .replace("__RC_NOT_MASKED__", str(RC_NOT_MASKED))
            .replace("__RC_STILL_RUNNING__", str(RC_STILL_RUNNING)))


def command_for(mode: str, *, paths: UnitPaths = DEFAULT_PATHS) -> str:
    """The exact remote command for *mode*: the script, piped to bash.

    Base64 keeps the script byte-exact through the transport (no heredoc
    nesting, no remote expansion), exactly like the batch's other installers.
    """
    b64 = base64.b64encode(install_script(mode, paths=paths).encode()).decode()
    return f"printf '%s' '{b64}' | base64 -d | bash -s"


# ── remote round-trip plumbing ──────────────────────────────────────────────

def _run(command: str, timeout: int, *, ssh_cmd: Runners) -> Dict[str, Any]:
    res = ssh_cmd(command, timeout)
    if not isinstance(res, dict):
        return {"ok": False, "stdout": "", "code": None, "stderr": "",
                "error": f"ssh_cmd returned {type(res).__name__}, expected dict"}
    return {"ok": bool(res.get("ok")), "stdout": res.get("stdout") or "",
            "stderr": res.get("stderr") or "", "code": res.get("code")}


def _hscc_lines(stdout: str) -> Dict[str, str]:
    """Every ``HSCC_*: value`` line from a script run (unknown keys kept)."""
    out: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if line.startswith("HSCC_") and ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _rc_of(value: Optional[str], fallback: Any) -> Optional[int]:
    """Integer exit code from an ``HSCC_RC`` string; *fallback* when unparseable.

    The node-side script's own code, never systemctl's: `is-enabled` exits 1 for
    `masked` and 4 for `not-found`, both of which are healthy answers, so
    systemctl's rc must never reach a verdict here.
    """
    text = (value or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return fallback if isinstance(fallback, int) else None


def _units_from(lines: Dict[str, str]) -> List[Dict[str, str]]:
    """Pair up ``HSCC_UNIT_<i>`` triples into [{unit,state,active,link}]."""
    units: List[Dict[str, str]] = []
    i = 0
    while f"HSCC_UNIT_{i}" in lines:
        units.append({
            "unit": lines[f"HSCC_UNIT_{i}"],
            "state": lines.get(f"HSCC_UNIT_{i}_STATE", ""),
            "active": lines.get(f"HSCC_UNIT_{i}_ACTIVE", ""),
            "link": lines.get(f"HSCC_UNIT_{i}_LINK", ""),
        })
        i += 1
    return units


def _state_from(lines: Dict[str, str]) -> Dict[str, Any]:
    units = _units_from(lines)
    return {
        "units": units,
        "mask_dir": lines.get("HSCC_MASK_DIR", ""),
        "uid": lines.get("HSCC_UID", ""),
        "xdg_runtime_dir": lines.get("HSCC_XDG_RUNTIME_DIR", ""),
        "bus_text": lines.get("HSCC_BUS_TEXT", ""),
        # Kept verbatim so the doctor can name a partially-masked node instead
        # of flattening it into "not masked".
        "states": [u["state"] for u in units],
        "active": [u["active"] for u in units],
        "links": [u["link"] for u in units],
    }


def _result(node: str, outcome: str, *, action: str = "", message: str = "",
            code: Optional[int] = None, state: Optional[Dict[str, Any]] = None,
            detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-node result — the contract the doctor check consumes.

    ``masked`` is DERIVED from the outcome, never asserted by the branch that
    produced it: no code path can call a fault "protected".
    """
    r: Dict[str, Any] = {
        "node": node,
        "ok": outcome in MASKED_OUTCOMES,
        "masked": outcome in MASKED_OUTCOMES,
        "outcome": outcome,
        "action": action,
        "message": message,
        "units": list(NOTIFIER_UNITS),
        "state": state or {},
    }
    if code is not None:
        r["code"] = code
    if detail:
        r.update(detail)
    return r


def _remote_state(node: str, *, ssh_cmd: Runners, timeout: int,
                  paths: UnitPaths) -> Dict[str, Any]:
    """Read the node's mask state — read-only, unprivileged.

    Anything that is not proof of state (transport failure, a missing HSCC_RC, a
    non-zero probe) returns ``{"transport_ok": False, "error": ...}``, which the
    caller must render as a fault. Never as "not masked".
    """
    res = _run(command_for("state", paths=paths), timeout, ssh_cmd=ssh_cmd)
    lines = _hscc_lines(res["stdout"])
    if not res["ok"] and not lines:
        why = (res["stderr"] or res["error"] or "ssh probe failed").strip()
        return {"transport_ok": False, "error": why or "ssh probe failed",
                "code": res["code"]}
    rc = lines.get("HSCC_RC")
    if rc != str(RC_OK):
        return {"transport_ok": False,
                "error": lines.get("HSCC_MSG")
                         or f"state probe returned rc={rc!r} (probe incomplete)",
                "code": rc if rc is not None else res["code"],
                "code_name": lines.get("HSCC_CODE", ""),
                "bus_text": lines.get("HSCC_BUS_TEXT", "")}
    state = _state_from(lines)
    units = state["units"]
    if not units:
        return {"transport_ok": False,
                "error": "state probe emitted no unit lines (probe incomplete)",
                "code": res["code"]}
    # A state block that contains a word systemctl would never print is not a
    # state: it is bus-error text that happened to arrive with rc=0.
    for u in units:
        if u["state"] not in MANAGED_STATE_WORDS:
            return {"transport_ok": False,
                    "error": f"{u['unit']}: systemctl --user is-enabled answered "
                             f"{u['state']!r} — not a unit state (bus/environment fault)",
                    "code": res["code"], "code_name": "NO_BUS",
                    "bus_text": u["state"]}
    state["transport_ok"] = True
    return state


# ── public surface ──────────────────────────────────────────────────────────

def _state_error(st: Dict[str, Any]) -> str:
    """The error text for a failed probe, with the node's own words appended.

    When the node answered with something that is not a unit state (the no-bus
    case prints ``Failed to connect to bus: …``), the operator must see THAT
    sentence, not just our summary — it is the difference between "the node is
    down" and "the session bus is unreachable", and both are actionability.
    """
    base = st.get("error") or "unreachable"
    bus = (st.get("bus_text") or "").strip()
    if bus and bus not in base:
        return f"{base} [systemctl said: {bus!r}]"
    return base


def probe_node(node: str, *, ssh_cmd: Runners, timeout: int = 30,
               paths: UnitPaths = DEFAULT_PATHS) -> Dict[str, Any]:
    """Read-only per-node mask state — the probe surface for `hscc doctor`.

    Never writes, never masks, never escalates. Same result shape as
    :func:`mask_node`, so the doctor and the installer cannot diverge on what
    "masked" means.
    """
    try:
        st = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    except Exception as e:  # a per-node fault must not abort a fleet pass
        return _result(node, ERROR,
                       message=f"{node}: unexpected {type(e).__name__}: {e}")
    if not st.get("transport_ok"):
        # Environment fault: an honest error naming the node and the reason.
        return _result(node, ERROR, action="probe",
                       message=f"{node}: UNVERIFIABLE — {_state_error(st)}",
                       code=st.get("code"),
                       detail={"code_name": st.get("code_name", ""),
                               "bus_text": st.get("bus_text", "")})
    if is_masked_state(st):
        runtime_only = any(u["state"] == "masked-runtime" for u in st["units"])
        return _result(node, OK_UNCHANGED, action="probe",
                       message=(f"{'masked-runtime' if runtime_only else 'masked'}"
                                f" (read-back from the node); none of "
                                f"{len(st['units'])} managed unit(s) can load"
                                + ("; NOTE a --runtime mask does not survive reboot"
                                   if runtime_only else "")),
                       state=st)
    unmasked = [f"{u['unit']}={u['state']}" for u in st["units"]
                if u["state"] not in MASKED_WORDS]
    still = [f"{u['unit']} is {u['active']}" for u in st["units"]
             if u["active"] in ("active", "activating")]
    why = ", ".join(unmasked) or "no unit reported a mask"
    return _result(node, ERROR, action="probe",
                   message=f"{node}: NOT MASKED — {why}"
                           + (f"; still running: {', '.join(still)}" if still else ""),
                   state=st)


def mask_node(node: str, *, ssh_cmd: Runners, dry_run: bool = False,
              timeout: int = 60, paths: UnitPaths = DEFAULT_PATHS,
              enabled: bool = True) -> Dict[str, Any]:
    """Ensure the notifier units are masked on ONE node. Never raises.

    ``ssh_cmd(command, timeout)`` is the injected transport; the caller binds
    the node into it (production: :func:`_default_ssh`; tests: a fabricated
    dict responder). Nothing here opens a socket, so a test cannot touch the
    fleet and no ssh is hand-rolled.
    """
    try:
        return _mask_node(node, ssh_cmd=ssh_cmd, dry_run=dry_run,
                          timeout=timeout, paths=paths, enabled=enabled)
    except Exception as e:
        return _result(node, ERROR,
                       message=f"{node}: unexpected {type(e).__name__}: {e}")


def _plan_text(st: Dict[str, Any]) -> str:
    """The per-node dry-run line: exact intent, no writes anywhere."""
    rows = [f"  │ {u['unit']}: is-enabled={u['state'] or '?'}"
            f" is-active={u['active'] or '?'}"
            f" mask_link={u['link'] or '(none)'}" for u in st["units"]]
    todo = [u["unit"] for u in st["units"] if u["state"] not in MASKED_WORDS]
    lines = [f"would run: systemctl --user mask --now <unit> for "
             f"{', '.join(todo) if todo else '(nothing — already masked)'}"]
    lines.append(f"  │ mask target: {st.get('mask_dir') or MASK_DIR_TEMPLATE}"
                 f"/<unit> -> /dev/null (user-owned, no sudo)")
    lines.extend(rows)
    lines.append("  │ untouched: the firmware-updater snap itself, and "
                 "firmware-updater-app.service")
    return "\n".join(lines)


def _mask_node(node: str, *, ssh_cmd: Runners, dry_run: bool,
               timeout: int, paths: UnitPaths, enabled: bool) -> Dict[str, Any]:
    if not enabled:
        return _result(node, SKIP_DISABLED, action="skipped",
                       message=f"{NODE_RECOVERY_KEY}.{MASK_SUBKEY}=false — skipped. "
                               f"If this node's notifier was masked by an earlier run "
                               f"it is STILL masked; undo deliberately with "
                               f"`python3 install_notifier_mask.py --unmask --hosts {node}`")

    st = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    if not st.get("transport_ok"):
        return _result(node, ERROR, action="probe",
                       message=f"{node}: {_state_error(st)}",
                       code=st.get("code"),
                       detail={"code_name": st.get("code_name", "")})

    # A mask link that is not /dev/null is not ours, whatever the state says:
    # the same rule the node-side script applies, so client and node cannot
    # disagree about what "foreign" means.
    foreign = [u for u in st["units"] if u["link"] and u["link"] != "/dev/null"]
    if foreign:
        names = "; ".join(f"{u['unit']} -> {u['link']}" for u in foreign)
        return _result(node, DRIFT, action="refused",
                       message=f"{node}: {names} is a user unit override HSCC did "
                               f"not write — left untouched (remove it to let HSCC "
                               f"manage this unit, or set "
                               f"{NODE_RECOVERY_KEY}.{MASK_SUBKEY}=false)",
                       code=RC_DRIFT, state=st)

    already = is_masked_state(st)
    if dry_run:
        return _result(node, OK_UNCHANGED if already else OK_APPLIED,
                       action="dry-run",
                       message=("already masked — would be a no-op" if already
                                else "would mask (writes nothing in dry-run)"),
                       state=st, detail={"would_write": not already,
                                         "plan": _plan_text(st)})
    if already:
        # Idempotence: no write, no `mask --now` round trip, no restart of
        # anything. Still reports what the node said, so the operator sees the
        # read-back rather than our assumption.
        runtime_only = any(u["state"] == "masked-runtime" for u in st["units"])
        return _result(node, OK_UNCHANGED, action="unchanged",
                       message=f"{node}: {', '.join(NOTIFIER_UNITS)} already masked"
                               + (" (masked-runtime: will NOT survive reboot)"
                                  if runtime_only else "")
                               + f"; {len(st['units'])} unit(s) verified by read-back",
                       state=st)

    res = _run(command_for("install", paths=paths), timeout, ssh_cmd=ssh_cmd)
    lines = _hscc_lines(res["stdout"])
    code = lines.get("HSCC_RC")
    code_i = int(code) if (code or "").lstrip("-").isdigit() else res.get("code")
    action = lines.get("HSCC_ACTION") or ""
    rcode = lines.get("HSCC_CODE") or ""

    if rcode == "DRIFT_FOREIGN" or code_i == RC_DRIFT:
        return _result(node, DRIFT, action="refused",
                       message=lines.get("HSCC_MSG") or "a foreign mask/override "
                                                        "exists at the mask path",
                       code=code_i, state=st)
    if rcode in ("NO_BUS", "NO_SYSTEMD", "NO_HOME") or code_i in (RC_NO_BUS, RC_NO_SYSTEMD,
                                                                  RC_NO_HOME):
        return _result(node, ERROR, action="refused",
                       message=lines.get("HSCC_MSG")
                               or f"node refused the operation ({rcode or 'rc=%s' % code_i})",
                       code=code_i, state=st,
                       detail={"code_name": rcode, "bus_text": lines.get("HSCC_BUS_TEXT", "")})
    if rcode in ("MASK_FAILED", "UNMASK_FAILED") or code_i == RC_MASK_FAILED:
        return _result(node, ERROR, action="failed",
                       message=lines.get("HSCC_MSG") or "systemctl --user mask failed",
                       code=code_i, state=st)
    if rcode in ("NOT_MASKED", "STILL_RUNNING") or code_i in (RC_NOT_MASKED,
                                                             RC_STILL_RUNNING):
        return _result(node, ERROR, action=action or "applied",
                       message=lines.get("HSCC_MSG")
                               or f"mask did not take effect (rc={code_i})",
                       code=code_i, state=st)
    if code_i != RC_OK:
        return _result(node, ERROR, action=action or "unknown",
                       message=lines.get("HSCC_MSG")
                               or f"install script failed (rc={code_i})"
                                  + (f" — {res['stderr'].strip()}" if res["stderr"].strip() else ""),
                       code=code_i, state=st)

    # READ BACK from the node — the arbiter, exactly as the watchdog card does.
    verify = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    if not verify.get("transport_ok"):
        return _result(node, ERROR, action=action or "applied",
                       message=f"{node}: mask reported {action or 'success'} but "
                               f"verification failed: {verify.get('error')}",
                       code=code_i, state=st, detail={"verify": verify})
    if not is_masked_state(verify):
        return _result(node, ERROR, action=action or "applied",
                       message=f"{node}: after mask the node still reports "
                               f"states {verify.get('states')!r} for {list(NOTIFIER_UNITS)}",
                       code=code_i, state=st, detail={"verify": verify})
    still = [u for u in verify["units"] if u["active"] in ("active", "activating")]
    if still:
        return _result(node, ERROR, action=action or "applied",
                       message=f"{node}: masked but "
                               f"{', '.join(u['unit'] for u in still)} still "
                               f"{still[0]['active']} — the loop has not stopped",
                       code=code_i, state=st, detail={"verify": verify})
    return _result(node, OK_UNCHANGED if action == "unchanged" else OK_APPLIED,
                   action=action or "applied",
                   message=f"{', '.join(NOTIFIER_UNITS)} masked and stopped "
                           f"(read-back verified on the node)",
                   state=verify, detail={"verify": verify})


def unmask_node(node: str, *, ssh_cmd: Runners, timeout: int = 60,
                paths: UnitPaths = DEFAULT_PATHS) -> Dict[str, Any]:
    """Undo HSCC's mask on one node. Refuses a mask link that is not /dev/null.

    Explicit by design: nothing in the opt-out path calls this, because reading
    a config key must never delete state on a node the operator just told us to
    leave alone (same rule the watchdog card applies to ``--disarm``).
    """
    try:
        res = _run(command_for("uninstall", paths=paths), timeout, ssh_cmd=ssh_cmd)
    except Exception as e:
        return _result(node, ERROR, message=f"{node}: unexpected {type(e).__name__}: {e}")
    lines = _hscc_lines(res["stdout"])
    code = lines.get("HSCC_RC")
    code_i = int(code) if (code or "").lstrip("-").isdigit() else res.get("code")
    if lines.get("HSCC_CODE") == "DRIFT_FOREIGN" or code_i == RC_DRIFT:
        return _result(node, DRIFT, action="refused",
                       message=lines.get("HSCC_MSG") or "the mask at that path is not HSCC's",
                       code=code_i)
    if code_i != RC_OK:
        return _result(node, ERROR,
                       message=lines.get("HSCC_MSG")
                               or (res["stderr"] or res["error"] or f"unmask failed (rc={code_i})"),
                       code=code_i)
    verify = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    back = "" if verify.get("transport_ok") else \
        f" (verification unreadable: {verify.get('error')})"
    return _result(node, OK_APPLIED, action=lines.get("HSCC_ACTION") or "unmasked",
                   message=f"mask removed for {', '.join(NOTIFIER_UNITS)} — the unit "
                           f"can load again{back}",
                   state=verify if verify.get("transport_ok") else {})


def mask_fleet(nodes: List[str], *, ssh_factory: Callable[[str], Runners],
               dry_run: bool = False, timeout: int = 60,
               config_path: Optional[str] = None,
               unmask: bool = False) -> Dict[str, Any]:
    """Apply the notifier-mask policy across *nodes*; per-node faults isolated.

    ``ssh_factory(node)`` returns that node's ``(command, timeout) -> dict``
    transport (the test seam). ``ok`` is True only when every node is masked or
    explicitly opted out: "3 of 4 quarantined" must not read as a success.
    """
    cfg = notifier_mask_config(config_path)
    enabled = bool(cfg[MASK_SUBKEY])
    results: List[Dict[str, Any]] = []
    for node in nodes:
        try:
            if unmask:
                results.append(unmask_node(node, ssh_cmd=ssh_factory(node),
                                           timeout=timeout))
            elif not enabled:
                results.append(_mask_node(node, ssh_cmd=ssh_factory(node),
                                          dry_run=False, timeout=timeout,
                                          paths=DEFAULT_PATHS, enabled=False))
            else:
                results.append(mask_node(node, ssh_cmd=ssh_factory(node),
                                         dry_run=dry_run, timeout=timeout,
                                         enabled=True))
        except Exception as e:  # a crashing node must not lose the other nodes
            results.append(_result(node, ERROR,
                                   message=f"{node}: unexpected {type(e).__name__}: {e}"))
    opted = [r for r in results if r["outcome"] == SKIP_DISABLED]
    bad = [r for r in results if not r["masked"] and r["outcome"] != SKIP_DISABLED]
    return {
        "ok": not bad,
        "dry_run": dry_run,
        "enabled": enabled,
        "config_error": cfg.get("config_error") or "",
        "target": {"units": list(NOTIFIER_UNITS),
                   "mask_dir": MASK_DIR_TEMPLATE,
                   "snap_untouched": "firmware-updater"},
        "nodes": results,
        "masked": [r["node"] for r in results if r["masked"]],
        "opted_out": [r["node"] for r in opted],
        "needing_attention": [{"node": r["node"], "outcome": r["outcome"],
                               "message": r["message"]} for r in bad],
    }


_MARK = {OK_APPLIED: "ok", OK_UNCHANGED: "ok", SKIP_DISABLED: "--",
         DRIFT: "XX", ERROR: "XX"}


def format_report(summary: Dict[str, Any]) -> List[str]:
    """Plain-text per-node report (bootstrap embeds these lines; no Rich art)."""
    t = summary["target"]
    lines = [f"node-recovery snap-notifier mask — "
             f"{'DRY RUN (writes nothing)' if summary['dry_run'] else 'APPLY'}",
             f"  target: mask {', '.join(t['units'])} for the desktop user "
             f"(no sudo); the {t['snap_untouched']} snap itself is untouched"]
    if not summary["enabled"]:
        lines.append(f"  {NODE_RECOVERY_KEY}.{MASK_SUBKEY}=false in "
                     f"{cluster_config_path()} — skipped by request")
    if summary["config_error"]:
        lines.append(f"  WARN config: {summary['config_error']}")
    for r in summary["nodes"]:
        lines.append(f"  [{_MARK.get(r['outcome'], 'XX')}] {r['node']} "
                     f"{r['outcome']}: {r['message']}")
    if summary["needing_attention"]:
        lines.append(f"  {len(summary['needing_attention'])} node(s) are NOT "
                     f"quarantined and need attention")
    lines.append(f"  masked: {len(summary['masked'])}/{len(summary['nodes'])}"
                 f"; opted out: {len(summary['opted_out'])}")
    return lines


def bootstrap_lines(summary: Dict[str, Any]) -> List[str]:
    """``OK|`` / ``WARN|`` lines for bootstrap.sh's stage summariser.

    bootstrap prints the first line as ok/warn and the rest as per-node detail.
    A stage whose result is "we did not actually quarantine the node" must lead
    with WARN — an OK line here would present a still-looping fleet as fixed.
    """
    n = len(summary["nodes"])
    if not summary["enabled"]:
        return [f"OK|snap firmware-notifier mask skipped — "
                f"{NODE_RECOVERY_KEY}.{MASK_SUBKEY} is false (opt-out in cluster.json)"]
    if summary["dry_run"]:
        return [f"OK|snap firmware-notifier mask dry-run: {n} node(s) planned, "
                f"nothing written"]
    if not n:
        return ["WARN|no nodes were checked — is the sparkrun cluster configured?"]
    head = (f"{'OK' if summary['ok'] else 'WARN'}|snap firmware-notifier mask "
            f"masked+read-back-verified on {len(summary['masked'])}/{n} node(s) "
            f"({NOTIFIER_SERVICE} + its timer; the firmware-updater snap itself "
            f"is untouched)")
    out = [head]
    for r in summary["nodes"]:
        if r["outcome"] in (OK_APPLIED, OK_UNCHANGED):
            out.append(f"OK|{r['node']}: {r['message']}")
        else:
            out.append(f"WARN|{r['node']} [{r['outcome']}]: {r['message']}")
    if summary["config_error"]:
        out.append(f"WARN|config: {summary['config_error']}")
    if not summary["ok"] and not summary["masked"]:
        out.append("WARN|no node got the mask — the notifier restart loop is "
                   "still running on the fleet")
    return out


# ── production transport + CLI ──────────────────────────────────────────────

def _default_ssh(host: str, command: str, timeout: int = 60) -> Dict[str, Any]:
    """Fleet transport: ``hscc-cluster.clusterlib.ssh_cmd`` (sparkrun owns the fleet).

    The same sanctioned resolution ``install_autoreboot._default_ssh`` uses:
    resolve the ``hscc-cluster`` dir that sits beside this one (true in the work
    repo and in the deployed ``~/.hermes/plugins`` layout) and route through
    clusterlib -> sparkrun_bridge -> sparkrun's own ``run_remote_script`` with
    sparkrun's ssh kwargs. NO hand-rolled ssh. If the bridge cannot be
    imported, that is an honest failure for every node, not a fabricated ok.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cluster_dir = os.path.join(os.path.dirname(here), "hscc-cluster")
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


def main(argv: Optional[List[str]] = None) -> int:
    """CLI used by bootstrap.sh and by the operator.

    Default output is one JSON summary line (``--bootstrap-lines`` swaps that for
    the ``OK|``/``WARN|`` report bootstrap prints, so the shared bootstrap.sh
    stays a dozen lines instead of carrying a second inline summariser).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    unmask = "--unmask" in argv
    as_lines = "--bootstrap-lines" in argv
    as_json = "--json" in argv
    hosts: List[str] = []
    if "--hosts" in argv:
        i = argv.index("--hosts")
        hosts = [h for h in argv[i + 1:] if not h.startswith("--")]
    if not hosts:
        hosts = _hosts_from_detect()
    if not hosts:
        print(json.dumps({"nodes": [], "ok": False,
                          "error": "no hosts: pass --hosts or configure a sparkrun cluster"}))
        return 2
    config_path = None
    if "--config" in argv:
        config_path = argv[argv.index("--config") + 1]
    summary = mask_fleet(hosts, ssh_factory=lambda h: (
        lambda cmd, to, _host=h: _default_ssh(_host, cmd, to)),
        dry_run=dry_run, config_path=config_path, unmask=unmask)
    if as_lines:
        print("\n".join(bootstrap_lines(summary)))
    else:
        print(json.dumps(summary, indent=2) if as_json
              else "\n".join(format_report(summary)))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
