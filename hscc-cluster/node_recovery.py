"""Arm the GB10 SBSA hardware watchdog on every cluster node (node-recovery part 1).

WHY THIS EXISTS
---------------
A DGX Spark froze solid on 2026-10-27: the journal stops mid-afternoon with no
panic, oops or softlockup line, the node answered nothing on ping/ssh/IB for
about two hours, and the operator power-cycled it by hand. Sparks have **no
BMC** — nothing outside the SoC can reset one. Each GB10 does expose a hardware
watchdog (``/dev/watchdog0``, "SBSA Generic Watchdog") that ships unused. When
PID1 owns and pets it, a hard hang resets the SoC within the timeout instead of
wedging until someone walks to the rack.

Arm it through systemd, NOT the ``watchdog`` userspace daemon: PID1 opens the
device, pets it from its main loop, and closes it on clean shutdown, so a
planned reboot never leaves the device armed behind it. The whole install is one
drop-in under ``/etc/systemd/system.conf.d/`` plus a ``daemon-reexec``.

SHARED WITH THE DOCTOR CHECK (sibling card t_94598b3a)
-----------------------------------------------------
``DROPIN_PATH``, ``RUNTIME_WATCHDOG_SEC``, ``REBOOT_WATCHDOG_SEC``,
:data:`ARMED_OUTCOMES` and the result shape returned by :func:`probe_node` are
the contract. The doctor must reuse ``probe_node`` / ``is_armed_state`` rather
than invent a second notion of "armed" or hardcode a diverging timeout.

Address discipline: no real LAN/tailnet address appears in this module. Host
strings come from the live topology at run time; fixtures use the documented
placeholder convention (10.0.0.x for a LAN node).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# ── The install target ──────────────────────────────────────────────────────
#: Drop-in location. The ``90-`` prefix leaves an operator's own ``00-``/``10-``
#: file able to win by systemd's lexicographic last-write-wins rule.
DROPIN_PATH = "/etc/systemd/system.conf.d/90-hscc-watchdog.conf"

#: PID1 pets /dev/watchdog0 at ~a third of this interval, so a hang that stops
#: the main loop resets the SoC ~30s later. 0 would disable the watchdog.
RUNTIME_WATCHDOG_SEC = 30

#: Grace systemd allows itself to shut down before forcing a reboot — the
#: second line of defence if PID1 is wedged on the way down.
REBOOT_WATCHDOG_SEC = "2min"

#: The opt-out block lives in ~/.hscc/cluster.json under this key. The key
#: naming is a batch contract: the sysctl sibling card reads the sibling
#: ``autoreboot`` subkey; this card owns ``watchdog``. Subkeys are read
#: independently and unknown subkeys are ignored, so an older consumer cannot
#: trip over a newer key and vice versa.
NODE_RECOVERY_KEY = "node_recovery"
WATCHDOG_SUBKEY = "watchdog"
AUTOREBOOT_SUBKEY = "autoreboot"

#: The watchdog core directory and device node the SBSA driver exposes.
WATCHDOG_SYSFS_DIR = "/sys/class/watchdog"
WATCHDOG_DEV = "/dev/watchdog0"

#: Canonical content. Byte-exact on purpose: idempotence is a content compare,
#: so changing this string forces one re-write on every node.
DROPIN_CONTENT = (
    "# Managed by HSCC (node-recovery watchdog; hscc bootstrap).\n"
    "# PID1 (systemd) opens and pets /dev/watchdog0 and closes it on clean\n"
    "# shutdown, so a hard hang resets the SoC instead of wedging until someone\n"
    "# power-cycles the box. To stop HSCC managing this, set\n"
    "#   node_recovery: {watchdog: false}\n"
    "# in ~/.hscc/cluster.json and re-run bootstrap, then remove this file.\n"
    "[Manager]\n"
    f"RuntimeWatchdogSec={RUNTIME_WATCHDOG_SEC}s\n"
    f"RebootWatchdogSec={REBOOT_WATCHDOG_SEC}\n"
)

#: Every spelling of "30 seconds" `systemctl show -p RuntimeWatchdogUSec
#: --value` can print (systemd prints a human duration; older builds print raw
#: microseconds). ONE source: both the Python matcher and the bash `grep -E`
#: inside the node script are built from this tuple, so the installer and the
#: doctor can never disagree on what counts as armed.
ARMED_ACCEPTED = ("30s", "30000000", "30000000us", "0min 30s", "30 s")
_ARMED_ALT = "|".join(ARMED_ACCEPTED)
_ARMED_RE = re.compile(r"^(?:" + "|".join(re.escape(a) for a in ARMED_ACCEPTED) + r")$")

# ── Per-node outcome vocabulary ─────────────────────────────────────────────
# The three groups mean different things to the operator and to the doctor:
#   applied / unchanged            -> the node IS protected
#   skipped_disabled               -> intentional opt-out (not a fault)
#   drift / refused / error        -> the node is NOT protected; act on it
OK_APPLIED = "applied"
OK_UNCHANGED = "unchanged"
SKIP_DISABLED = "skipped_disabled"
DRIFT = "drift"
REFUSED_FOREIGN = "refused_foreign_watchdog"
ERROR = "error"

#: Outcomes that mean "this node is protected". Everything else means it is not.
ARMED_OUTCOMES = frozenset({OK_APPLIED, OK_UNCHANGED})

#: Exit codes the node-side script reports (HSCC_RC). Kept here so tests and the
#: doctor can name a failure without parsing prose.
RC_OK = 0
RC_BAD_MODE = 2
RC_NO_PREREQ = 3
RC_NEED_ROOT = 4
RC_WRITE_FAILED = 5
RC_FOREIGN_HOLDER = 6
RC_DRIFT = 7
RC_REEXEC_FAILED = 8

Runners = Callable[[str, int], Dict[str, Any]]


@dataclass(frozen=True)
class NodePaths:
    """Where the watchdog lives on the node. Fixed production defaults.

    A seam, not a feature: the bash-backed tests point these at a sandbox
    because they cannot fabricate ``/sys/class/watchdog`` on macOS. Production
    always uses the defaults, and the doctor check must too — so the installer
    and the doctor cannot drift onto different paths.
    """
    dropin: str = DROPIN_PATH
    sysfs_dir: str = WATCHDOG_SYSFS_DIR
    dev: str = WATCHDOG_DEV


DEFAULT_PATHS = NodePaths()


def expected_dropin() -> str:
    """The exact bytes the managed drop-in must hold on every node."""
    return DROPIN_CONTENT


def dropin_sha256() -> str:
    """sha256 of :data:`DROPIN_CONTENT` — the idempotence fingerprint."""
    return hashlib.sha256(DROPIN_CONTENT.encode()).hexdigest()


def is_armed_state(state: Dict[str, Any]) -> bool:
    """Does a probe state dict say PID1 is running the watchdog at target?

    The one definition of "armed", shared by the installer and the doctor check.
    An unreadable value is False (not armed) — never a guess.
    """
    value = (state.get("runtime_watchdog_usec") or "").strip()
    return _ARMED_RE.match(value) is not None


# ── Config / opt-out ────────────────────────────────────────────────────────

def hscc_dir() -> str:
    """HSCC state dir, env-resolved (never a baked ``~/.hscc``)."""
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def cluster_config_path() -> str:
    """The cluster config that carries the ``node_recovery`` block."""
    return os.path.join(hscc_dir(), "cluster.json")


def node_recovery_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the ``node_recovery`` block; **default ON** when absent.

    Returns ``{"watchdog": bool, "autoreboot": bool, "config_error": str}``.
    Subkeys are read INDEPENDENTLY: ``autoreboot: false`` (the sysctl card's
    knob) can never switch the watchdog off, and an unknown subkey is ignored
    rather than fatal — so an older consumer survives a newer file.

    A non-boolean subkey (``"false"``, ``0``, a typo) does NOT silently flip the
    feature: the default (ON) is kept and the problem is reported in
    ``config_error``. A silently-dead opt-out looks exactly like a working one.
    Absent file / absent block is the documented default and NOT an error.
    """
    out: Dict[str, Any] = {"watchdog": True, "autoreboot": True, "config_error": ""}
    target = path or cluster_config_path()
    try:
        with open(target) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return out
    except (json.JSONDecodeError, OSError, IsADirectoryError) as e:
        # Present but unreadable is an environment fault, not "no config".
        out["config_error"] = (f"could not read {target} ({e.__class__.__name__}: {e}) "
                               f"— defaults applied (watchdog ON)")
        return out
    if not isinstance(cfg, dict):
        out["config_error"] = (f"{target} is not a JSON object — defaults applied "
                               f"(watchdog ON)")
        return out
    block = cfg.get(NODE_RECOVERY_KEY)
    if block is None:
        return out
    if not isinstance(block, dict):
        out["config_error"] = (f"{NODE_RECOVERY_KEY} must be a mapping, got "
                               f"{type(block).__name__} — defaults applied (watchdog ON)")
        return out
    for subkey, name in ((WATCHDOG_SUBKEY, "watchdog"), (AUTOREBOOT_SUBKEY, "autoreboot")):
        if subkey not in block:
            continue
        val = block[subkey]
        if isinstance(val, bool):
            out[name] = val
        else:
            out["config_error"] = ((out["config_error"] + "; ") if out["config_error"] else "")
            out["config_error"] += (f"{NODE_RECOVERY_KEY}.{subkey}={val!r} is not "
                                    f"true/false — kept ON")
    return out


def watchdog_enabled(path: Optional[str] = None) -> bool:
    """Is the watchdog layer enabled for this fleet? (convenience wrapper)"""
    return bool(node_recovery_config(path)[WATCHDOG_SUBKEY])


# ── The node-side script ────────────────────────────────────────────────────
# Shipped to the node base64-encoded in HSCC_WANTED_B64. Two reasons, both
# load-bearing: (1) a nested unquoted heredoc inside the heredoc the bridge
# already uses would be expanded by the REMOTE shell and silently blank our
# `$()`; (2) it keeps the file we may overwrite out of the argv of the shell
# that decides whether to overwrite it.
#
# Contract (shared with the doctor): every stdout line is `KEY: value`; the
# LAST line is always `HSCC_RC: <n>` and carries the real exit code. A failure
# therefore never arrives as a bare `ok` line — a missing HSCC_RC means the
# script itself died, which the caller also treats as a fault.
_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# HSCC node-recovery watchdog — GENERATED by hscc-cluster/node_recovery.py.
# Do not edit on the node; edit the module. Modes: state | install | uninstall
set -u
MODE="__MODE__"
DEV="__DEV__"
SYSFS_DIR="__SYSFS_DIR__"
PATH_FILE="__PATH_FILE__"

say() { printf '%s\n' "$1"; }
fail() { say "HSCC_CODE: $2"; say "HSCC_MSG: $3"; say "HSCC_RC: $1"; exit "$1"; }

runtime_usec() { systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null; }

emit_state() {
  say "HSCC_STATE_BEGIN: 1"
  say "HSCC_PATH: $PATH_FILE"
  if [ -f "$PATH_FILE" ]; then
    say "HSCC_PRESENT: true"
    say "HSCC_SHA256: $(sha256sum "$PATH_FILE" 2>/dev/null | cut -d' ' -f1)"
  else
    say "HSCC_PRESENT: false"
    say "HSCC_SHA256:"
  fi
  if command -v systemctl >/dev/null 2>&1; then
    say "HSCC_RUNTIME_WATCHDOG_USEC: $(runtime_usec)"
    say "HSCC_REBOOT_WATCHDOG_USEC: $(systemctl show -p RebootWatchdogUSec --value 2>/dev/null)"
  else
    say "HSCC_SYSTEMD: missing"
    say "HSCC_RUNTIME_WATCHDOG_USEC:"
    say "HSCC_REBOOT_WATCHDOG_USEC:"
  fi
  if [ -d "$SYSFS_DIR" ]; then
    say "HSCC_WATCHDOG_CLASS: present"
    say "HSCC_WATCHDOG_DEVICES: $(ls "$SYSFS_DIR" 2>/dev/null | tr '\n' ' ')"
    [ -r "$SYSFS_DIR/watchdog0/name" ] && say "HSCC_WATCHDOG0_NAME: $(cat "$SYSFS_DIR/watchdog0/name" 2>/dev/null)"
  else
    say "HSCC_WATCHDOG_CLASS: missing"
    say "HSCC_WATCHDOG_DEVICES:"
  fi
  # Advisory only. `fuser` needs no privilege to list pids but is not installed
  # everywhere, so an empty result is NOT proof of no holder — the authoritative
  # signal is HSCC_RUNTIME_WATCHDOG_USEC (a live systemd at the target is the
  # legitimate holder; any other live petter is foreign).
  if command -v fuser >/dev/null 2>&1; then
    say "HSCC_FUSER: $(fuser "$DEV" 2>/dev/null | tr -s ' ')"
  else
    say "HSCC_FUSER: unavailable"
  fi
  say "HSCC_STATE_END: 1"
}

case "$MODE" in
  state) emit_state; say "HSCC_RC: 0"; exit 0 ;;
  install|uninstall) ;;
  *) fail __RC_BAD__ BAD_MODE "unknown mode '$MODE'" ;;
esac

# ── privilege (checked first: it is what makes the rest possible) ────────────
# `sudo -n` only, never an interactive password: this transport does not
# allocate a tty, so a password prompt would hang the node until timeout. A
# refusal must come back as a clear failure for THIS node.
if ! command -v sudo >/dev/null 2>&1; then
  fail __RC_ROOT__ NEED_ROOT "sudo not present and this shell is not root (uid=$(id -u 2>/dev/null)); cannot write $PATH_FILE"
fi
if [ "$(id -u)" = "0" ]; then
  SUDO=""
  say "HSCC_PRIVILEGE: root"
elif sudo -n true >/dev/null 2>&1; then
  SUDO="sudo -n"
  say "HSCC_PRIVILEGE: sudo-n"
else
  fail __RC_ROOT__ NEED_ROOT "sudo -n refused on this node and the ssh user is not root; HSCC will not hang on a password prompt. Grant NOPASSWD for 'sudo tee $PATH_FILE' and 'sudo systemctl daemon-reexec', or arm the watchdog by hand."
fi

# ── prerequisites (before any write: arming a timeout nothing can pet is worse
# than not arming, because PID1 then owns a device no driver services) ───────
if ! command -v systemctl >/dev/null 2>&1; then
  fail __RC_PREQ__ NO_SYSTEMD "systemctl not on PATH; cannot manage PID1's runtime watchdog here"
fi
if [ ! -d "$SYSFS_DIR" ]; then
  fail __RC_PREQ__ NO_WATCHDOG_CORE "$SYSFS_DIR absent: no watchdog driver on this node"
fi
if [ ! -e "$DEV" ]; then
  fail __RC_PREQ__ NO_WATCHDOG_DEVICE "$DEV absent though $SYSFS_DIR exists; check the sbsa-gwdt driver before arming"
fi

if [ "$MODE" = "uninstall" ]; then
  if [ ! -f "$PATH_FILE" ]; then say "HSCC_ACTION: absent"; say "HSCC_RC: 0"; exit 0; fi
  if ! cmp -s <(printf '%s' "$HSCC_WANTED_B64" | base64 -d) "$PATH_FILE"; then
    fail __RC_DRIFT__ DRIFT_FOREIGN "$PATH_FILE is not HSCC-managed content; refusing to remove a file we did not write"
  fi
  $SUDO rm -f "$PATH_FILE" || fail __RC_WRITE__ UNLINK_FAILED "could not remove $PATH_FILE"
  REEXEC_OUT="$($SUDO systemctl daemon-reexec 2>&1)"; REEXEC_RC=$?
  [ -n "$REEXEC_OUT" ] && say "HSCC_REEXEC: $REEXEC_OUT"
  say "HSCC_ACTION: removed"
  emit_state
  say "HSCC_RC: 0"
  exit 0
fi

# ── never steal a device: refuse a foreign holder ────────────────────────────
# If a live systemd is already at the target timeout it IS the holder and we
# are a no-op. If the timeout is unset/zero but something has the device open,
# that consumer is foreign and taking it over could reset the box underneath a
# workload that depends on it. No O_EXCL open is attempted here: opening a
# watchdog device can itself start the kernel timeout.
_rt="$(runtime_usec)"
if command -v fuser >/dev/null 2>&1; then
  _holders="$(fuser "$DEV" 2>/dev/null | tr -s ' ')"
  if [ -n "$_holders" ] && { [ -z "$_rt" ] || [ "$_rt" = "0" ]; }; then
    say "HSCC_FUSER: $_holders"
    fail __RC_FOREIGN__ FOREIGN_HOLDER "$DEV is held by pid(s) [$_holders] while systemd RuntimeWatchdogSec is unset; refusing to steal the device"
  fi
fi

emit_state

# ── decide: unchanged | drift | write ────────────────────────────────────────
if [ -f "$PATH_FILE" ]; then
  if cmp -s <(printf '%s' "$HSCC_WANTED_B64" | base64 -d) "$PATH_FILE"; then
    if printf '%s' "$_rt" | grep -Eq '^(__ARMED_RE__)$'; then
      say "HSCC_ACTION: unchanged"; say "HSCC_RC: 0"; exit 0
    fi
    # Content is right but PID1 has not taken it (manual system.conf edit, or a
    # reexec that never happened). Re-exec is idempotent and does not restart
    # units, so re-applying the same bytes is the fix, not a clobber.
    say "HSCC_ACTION: reexec"
  else
    # Present and NOT ours. Either the operator edited it deliberately or
    # another tool owns the path — both mean STOP. The doctor check surfaces
    # this as RED rather than letting a divergent timeout read as protected.
    fail __RC_DRIFT__ DRIFT_FOREIGN "$PATH_FILE holds content HSCC did not write; left untouched. Review it, or delete it to let HSCC manage the watchdog, or set node_recovery.watchdog=false."
  fi
fi

# ── write atomically (stage in /tmp, then a same-directory root rename) ─────
DIR="$(dirname "$PATH_FILE")"
$SUDO mkdir -p "$DIR" || fail __RC_WRITE__ MKDIR_FAILED "cannot create $DIR"
TMP="$(mktemp "${TMPDIR:-/tmp}/90-hscc-watchdog.conf.XXXXXX")" \
  || fail __RC_WRITE__ MKTEMP_FAILED "could not create a staging file"
if ! printf '%s' "$HSCC_WANTED_B64" | base64 -d > "$TMP" 2>/dev/null; then
  rm -f "$TMP"; fail __RC_WRITE__ ENCODE_FAILED "could not decode the staged drop-in"
fi
if ! $SUDO tee "$PATH_FILE.tmp-hscc" > /dev/null < "$TMP"; then
  rm -f "$TMP"; $SUDO rm -f "$PATH_FILE.tmp-hscc"
  fail __RC_WRITE__ WRITE_FAILED "could not stage the drop-in beside $PATH_FILE"
fi
rm -f "$TMP"
# `tee` runs as root, so the staged file is already root-owned; set the MODE
# before the rename (tee's mode comes from root's umask, which we do not
# control) so a drop-in under a root-owned dir is never briefly world-writable.
$SUDO chmod 0644 "$PATH_FILE.tmp-hscc" \
  || fail __RC_WRITE__ CHMOD_FAILED "could not set mode on the staged drop-in"
if ! $SUDO mv -f "$PATH_FILE.tmp-hscc" "$PATH_FILE"; then
  $SUDO rm -f "$PATH_FILE.tmp-hscc"
  fail __RC_WRITE__ RENAME_FAILED "could not publish $PATH_FILE"
fi

# daemon-reexec is what makes PID1 re-read the Manager section. It re-execs
# PID1 in place: idempotent, restarts no units, safe on a busy node.
REEXEC_OUT="$($SUDO systemctl daemon-reexec 2>&1)"; REEXEC_RC=$?
[ -n "$REEXEC_OUT" ] && say "HSCC_REEXEC: $REEXEC_OUT"
if [ "$REEXEC_RC" -ne 0 ]; then
  say "HSCC_ACTION: applied_reexec_failed"
  say "HSCC_CODE: REEXEC_FAILED"
  say "HSCC_MSG: drop-in written but 'systemctl daemon-reexec' failed (rc=$REEXEC_RC); RuntimeWatchdogSec stays inert until PID1 re-reads its configuration"
  say "HSCC_RC: __RC_REEXEC__"
  exit __RC_REEXEC__
fi
say "HSCC_ACTION: applied"
emit_state
say "HSCC_RC: 0"
exit 0
"""


def install_script(mode: str, *, paths: NodePaths = DEFAULT_PATHS) -> str:
    """Build the node-side script for *mode* (``state``|``install``|``uninstall``).

    The desired content travels separately as ``HSCC_WANTED_B64`` (see
    :func:`wanted_env_prefix`), so the script never re-quotes file bytes.
    """
    if mode not in ("state", "install", "uninstall"):
        raise ValueError(f"unknown mode {mode!r}")
    return (_SCRIPT_TEMPLATE
            .replace("__MODE__", mode)
            .replace("__PATH_FILE__", posixpath.normpath(paths.dropin))
            .replace("__SYSFS_DIR__", posixpath.normpath(paths.sysfs_dir))
            .replace("__DEV__", posixpath.normpath(paths.dev))
            .replace("__ARMED_RE__", _ARMED_ALT)
            .replace("__RC_BAD__", str(RC_BAD_MODE))
            .replace("__RC_PREQ__", str(RC_NO_PREREQ))
            .replace("__RC_ROOT__", str(RC_NEED_ROOT))
            .replace("__RC_WRITE__", str(RC_WRITE_FAILED))
            .replace("__RC_FOREIGN__", str(RC_FOREIGN_HOLDER))
            .replace("__RC_DRIFT__", str(RC_DRIFT))
            .replace("__RC_REEXEC__", str(RC_REEXEC_FAILED)))


def wanted_env_prefix() -> str:
    """Shell line that carries the desired content to the node (base64)."""
    return "export HSCC_WANTED_B64=" + base64.b64encode(
        DROPIN_CONTENT.encode()).decode() + "\n"


# ── Remote round-trip plumbing ──────────────────────────────────────────────

def _run(command: str, timeout: int, *, ssh_cmd: Runners) -> Dict[str, Any]:
    res = ssh_cmd(command, timeout)
    if not isinstance(res, dict):
        return {"ok": False, "stdout": "", "code": None,
                "stderr": f"ssh_cmd returned {type(res).__name__}, expected dict"}
    return {"ok": bool(res.get("ok")), "stdout": res.get("stdout") or "",
            "stderr": res.get("stderr") or "", "code": res.get("code")}


def _rc_of(value: Optional[str], fallback: Any) -> Optional[int]:
    """Integer exit code from an ``HSCC_RC`` string; *fallback* when unparseable."""
    text = (value or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return fallback if isinstance(fallback, int) else None


def _hscc_lines(stdout: str) -> Dict[str, str]:
    """Every ``HSCC_*: value`` line from a script run (unknown keys kept)."""
    out: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if line.startswith("HSCC_") and ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _state_from(lines: Dict[str, str]) -> Dict[str, Any]:
    """Normalise the node's state block into the shape :func:`is_armed_state` reads."""
    return {
        "present": lines.get("HSCC_PRESENT") == "true",
        "sha256": lines.get("HSCC_SHA256") or "",
        "runtime_watchdog_usec": lines.get("HSCC_RUNTIME_WATCHDOG_USEC") or "",
        "reboot_watchdog_usec": lines.get("HSCC_REBOOT_WATCHDOG_USEC") or "",
        "watchdog_class": lines.get("HSCC_WATCHDOG_CLASS") or "present",
        "watchdog_devices": lines.get("HSCC_WATCHDOG_DEVICES") or "",
        "watchdog0_name": lines.get("HSCC_WATCHDOG0_NAME") or "",
        "fuser": lines.get("HSCC_FUSER") or "",
        "systemd": lines.get("HSCC_SYSTEMD") or "present",
    }


def _result(node: str, outcome: str, *, action: str = "", message: str = "",
            code: Optional[int] = None, state: Optional[Dict[str, Any]] = None,
            detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-node result — the contract the doctor check consumes.

    ``armed`` is DERIVED from the outcome (never asserted by the code path that
    produced it), so no branch can claim protection for a fault.
    """
    r: Dict[str, Any] = {
        "node": node,
        "ok": outcome in ARMED_OUTCOMES,
        "armed": outcome in ARMED_OUTCOMES,
        "outcome": outcome,
        "action": action,
        "message": message,
        "dropin_path": DROPIN_PATH,
        "expected": {"RuntimeWatchdogSec": f"{RUNTIME_WATCHDOG_SEC}s",
                     "RebootWatchdogSec": REBOOT_WATCHDOG_SEC},
        "state": state or {},
    }
    if code is not None:
        r["code"] = code
    if detail:
        r.update(detail)
    return r


def _remote_state(node: str, *, ssh_cmd: Runners, timeout: int,
                  paths: NodePaths) -> Dict[str, Any]:
    """Ask the node for its watchdog state — read-only, unprivileged.

    Returns ``{"transport_ok": False, "error": ...}`` for anything that is not
    proof of state: a transport failure, a missing HSCC_RC, a non-zero probe.
    The caller must render those as a fault, never as "not armed".
    """
    res = _run(wanted_env_prefix() + install_script("state", paths=paths),
               timeout, ssh_cmd=ssh_cmd)
    lines = _hscc_lines(res["stdout"])
    if not res["ok"] and not lines:
        return {"transport_ok": False,
                "error": (res["stderr"] or "ssh probe failed").strip() or "ssh probe failed",
                "code": res["code"]}
    rc = lines.get("HSCC_RC")
    if rc != str(RC_OK):
        return {"transport_ok": False,
                "error": lines.get("HSCC_MSG")
                         or f"state probe returned rc={rc!r} (probe incomplete)",
                "code": rc if rc is not None else res["code"]}
    state = _state_from(lines)
    state["transport_ok"] = True
    return state


# ── Public surface ──────────────────────────────────────────────────────────

def probe_node(node: str, *, ssh_cmd: Runners, timeout: int = 30,
               paths: NodePaths = DEFAULT_PATHS) -> Dict[str, Any]:
    """Read-only per-node watchdog state — the probe surface for `hscc doctor`.

    Never writes and never escalates. Same result shape as :func:`arm_node`, so
    the doctor and the installer cannot diverge on what "armed" means.
    """
    st = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    if not st.get("transport_ok"):
        # Environment fault: an honest ERROR naming the node, never 'ok'.
        return _result(node, ERROR, message=st.get("error") or "probe failed",
                       code=st.get("code"))
    armed = is_armed_state(st)
    holders = (st.get("fuser") or "").strip()
    foreign = (not armed) and holders not in ("", "unavailable")
    if armed:
        return _result(node, OK_UNCHANGED, action="probe",
                       message=f"RuntimeWatchdogUSec={st['runtime_watchdog_usec']} "
                               f"(target {RUNTIME_WATCHDOG_SEC}s); "
                               f"drop-in {'present' if st['present'] else 'ABSENT'}",
                       state=st)
    if foreign:
        return _result(node, REFUSED_FOREIGN, action="probe",
                       message=f"{DROPIN_PATH} not armed and {st.get('watchdog0_name') or '/dev/watchdog0'} "
                               f"held by [{st['fuser']}]",
                       state=st)
    why = []
    if st.get("watchdog_class") == "missing":
        why.append("no watchdog core on this node")
    if not st.get("present"):
        why.append(f"{DROPIN_PATH} absent")
    if st.get("systemd") == "missing":
        why.append("no systemctl")
    return _result(node, ERROR, action="probe",
                   message=f"RuntimeWatchdogUSec={st['runtime_watchdog_usec'] or 'unset'!r}, "
                           f"expected {RUNTIME_WATCHDOG_SEC}s"
                           + ("; " + "; ".join(why) if why else ""),
                   state=st)


def arm_node(node: str, *, ssh_cmd: Runners, dry_run: bool = False,
             timeout: int = 60, paths: NodePaths = DEFAULT_PATHS,
             enabled: bool = True) -> Dict[str, Any]:
    """Arm the watchdog on ONE node. Node-level faults are returned, not raised.

    ``ssh_cmd(command, timeout)`` is the injected transport — the caller binds
    the node into it (production: :func:`_default_ssh_factory`; tests: a
    fabricated dict responder). Nothing here opens a socket or shells out, so a
    test never touches the network and no ssh is hand-rolled.
    """
    try:
        return _arm_node(node, ssh_cmd=ssh_cmd, dry_run=dry_run,
                         timeout=timeout, paths=paths, enabled=enabled)
    except Exception as e:  # a per-node fault must not abort the fleet pass
        return _result(node, ERROR,
                       message=f"unexpected {type(e).__name__}: {e}")


def _arm_node(node: str, *, ssh_cmd: Runners, dry_run: bool,
              timeout: int, paths: NodePaths, enabled: bool) -> Dict[str, Any]:
    if not enabled:
        return _result(node, SKIP_DISABLED,
                       message=f"{NODE_RECOVERY_KEY}.{WATCHDOG_SUBKEY}=false — "
                               f"node opted out of the hardware watchdog (not armed)")

    st = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    if not st.get("transport_ok"):
        return _result(node, ERROR,
                       message=f"{node}: {st.get('error') or 'unreachable'}",
                       code=st.get("code"))

    # No watchdog core means arming sets a timeout nothing can pet — and PID1
    # would then own a device no driver services. Refuse before escalating.
    if st.get("watchdog_class") == "missing":
        return _result(node, ERROR, action="refused",
                       message=f"{node}: no watchdog core under {paths.sysfs_dir} — "
                               f"refusing to arm a timeout no driver can pet",
                       state=st)

    managed = bool(st.get("present")) and st.get("sha256") == dropin_sha256()
    drift = bool(st.get("present")) and not managed

    if dry_run:
        outcome = OK_UNCHANGED if managed else (DRIFT if drift else OK_APPLIED)
        return _result(node, outcome, action="dry-run",
                       message=_plan_text(st, managed=managed, drift=drift),
                       state=st,
                       detail={"would_write": not managed and not drift})

    if drift:
        # Never clobber a file we did not write — see the script's DRIFT branch.
        return _result(node, DRIFT, action="refused",
                       message=f"{node}: {paths.dropin} holds content HSCC did not write "
                               f"— left untouched (review or remove it to let HSCC manage "
                               f"the watchdog; or set {NODE_RECOVERY_KEY}.{WATCHDOG_SUBKEY}=false)",
                       code=RC_DRIFT, state=st)

    # Already exactly ours: nothing to write. But "the file is right" is not
    # "the node is protected" — PID1 must actually have adopted it, so confirm
    # from the state we just read and re-exec only if it has not.
    if managed:
        if is_armed_state(st):
            return _result(node, OK_UNCHANGED, action="unchanged",
                           message=f"{node}: {paths.dropin} already managed and "
                                   f"RuntimeWatchdogUSec={st['runtime_watchdog_usec']}",
                           state=st)
        # Content right, PID1 never took it (manual system.conf edit, or a run
        # that died before daemon-reexec). Re-applying identical bytes + re-exec
        # is the fix, not a clobber.

    res = _run(wanted_env_prefix() + install_script("install", paths=paths),
               timeout, ssh_cmd=ssh_cmd)
    lines = _hscc_lines(res["stdout"])
    code = _rc_of(lines.get("HSCC_RC"), res.get("code"))
    action = lines.get("HSCC_ACTION") or ""
    rcode = lines.get("HSCC_CODE") or ""

    # Node-side refusals: report verbatim, never retry, never escalate.
    if rcode == "FOREIGN_HOLDER" or code == RC_FOREIGN_HOLDER:
        return _result(node, REFUSED_FOREIGN, action="refused",
                       message=lines.get("HSCC_MSG")
                               or "device held by another consumer; refusing to steal it",
                       code=code, state=st)
    if rcode == "DRIFT_FOREIGN" or code == RC_DRIFT:
        return _result(node, DRIFT, action="refused",
                       message=lines.get("HSCC_MSG") or f"{paths.dropin} is not HSCC-managed",
                       code=code if code is not None else RC_DRIFT, state=st)
    if rcode in ("NEED_ROOT", "NO_SYSTEMD", "NO_WATCHDOG_CORE", "NO_WATCHDOG_DEVICE") \
            or code in (RC_NEED_ROOT, RC_NO_PREREQ):
        return _result(node, ERROR, action="refused",
                       message=lines.get("HSCC_MSG")
                               or f"node refused the install ({rcode or 'rc=%s' % code})",
                       code=code, state=st)
    if code != RC_OK:
        return _result(node, ERROR, action=action or "unknown",
                       message=lines.get("HSCC_MSG")
                               or f"install script failed (rc={code})"
                                  + (f" — {res['stderr'].strip()}" if res["stderr"].strip() else ""),
                       code=code, state=st)

    # Re-read FROM THE NODE. A local guess at "armed" is precisely how an
    # environment fault becomes a false green.
    verify = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    if not verify.get("transport_ok"):
        return _result(node, ERROR,
                       message=f"install reported {action or 'success'} but verification "
                               f"failed: {verify.get('error')}",
                       code=code, state=st, detail={"verify": verify})
    if not is_armed_state(verify):
        return _result(node, ERROR, action=action or "unknown",
                       message=f"after install RuntimeWatchdogUSec="
                               f"{verify.get('runtime_watchdog_usec') or 'unset'!r}; expected "
                               f"{RUNTIME_WATCHDOG_SEC}s (daemon-reexec may have been refused)",
                       code=code, state=st, detail={"verify": verify})
    return _result(node, OK_UNCHANGED if action in ("unchanged", "reexec") else OK_APPLIED,
                   action=action or "applied",
                   message=f"RuntimeWatchdogSec={RUNTIME_WATCHDOG_SEC}s armed "
                           f"(RuntimeWatchdogUSec={verify['runtime_watchdog_usec']})",
                   state=verify, detail={"verify": verify})


def disarm_node(node: str, *, ssh_cmd: Runners, timeout: int = 60,
                paths: NodePaths = DEFAULT_PATHS) -> Dict[str, Any]:
    """Remove OUR drop-in (opt-out cleanup). Leaves a foreign file alone.

    Used when ``node_recovery.watchdog`` flips to false: the setting alone does
    not disarm a node that was armed by a previous run, and silently leaving it
    armed while the config says off is the same lie in the other direction.
    """
    res = _run(wanted_env_prefix() + install_script("uninstall", paths=paths),
               timeout, ssh_cmd=ssh_cmd)
    lines = _hscc_lines(res["stdout"])
    code = _rc_of(lines.get("HSCC_RC"), res.get("code"))
    action = lines.get("HSCC_ACTION") or ""
    if lines.get("HSCC_CODE") == "DRIFT_FOREIGN":
        return _result(node, DRIFT, action="refused",
                       message=lines.get("HSCC_MSG") or f"{paths.dropin} is not HSCC-managed",
                       code=code if code is not None else RC_DRIFT)
    if code != RC_OK:
        return _result(node, ERROR,
                       message=lines.get("HSCC_MSG") or (res["stderr"] or "uninstall failed"),
                       code=code)
    verify = _remote_state(node, ssh_cmd=ssh_cmd, timeout=timeout, paths=paths)
    return _result(node, SKIP_DISABLED, action=action or "removed",
                   message=f"{paths.dropin} removed ({'still armed until reboot' if verify.get('transport_ok') and is_armed_state(verify) else 'not armed'})",
                   state=verify if verify.get("transport_ok") else {})


def _skip_disabled(node: str) -> Dict[str, Any]:
    """Opt-out result: a SKIP, not a removal.

    Deliberately performs NO remote call at all — reading a config key must not
    cause a root escalation or a delete on a node the operator just told us to
    leave alone. The message says what that implies (a node armed by an earlier
    run stays armed) and names the explicit command that changes it.
    """
    return _result(node, SKIP_DISABLED, action="skipped",
                   message=f"{NODE_RECOVERY_KEY}.{WATCHDOG_SUBKEY}=false — skipped. "
                           f"If this node was armed by an earlier run it is STILL "
                           f"armed; remove it deliberately with "
                           f"`python3 -m hscc-cluster.node_recovery --disarm --hosts {node}`")


def _plan_text(st: Dict[str, Any], *, managed: bool, drift: bool) -> str:
    """The per-node dry-run line: exact intent, no writes anywhere."""
    current = st.get("runtime_watchdog_usec") or "unset"
    if managed:
        return (f"unchanged: {DROPIN_PATH} already holds the managed content "
                f"(sha256 {dropin_sha256()[:12]}…), "
                f"RuntimeWatchdogUSec={current}")
    if drift:
        return (f"DRIFT — {DROPIN_PATH} exists with non-HSCC content (sha256 "
                f"{(st.get('sha256') or '?')[:12]}… vs managed {dropin_sha256()[:12]}…); "
                f"would leave it untouched and report RED")
    return (f"would create {DROPIN_PATH} with:\n"
            + "".join(f"  │ {ln}\n" for ln in DROPIN_CONTENT.rstrip("\n").splitlines())
            + f"  │ then: sudo -n systemctl daemon-reexec\n"
            + f"  │ current RuntimeWatchdogUSec={current}; "
              f"watchdog devices=[{st.get('watchdog_devices') or '?'}]")


def arm_fleet(nodes: List[str], *, ssh_factory: Callable[[str], Runners],
              dry_run: bool = False, timeout: int = 60,
              config_path: Optional[str] = None,
              disarm: bool = False) -> Dict[str, Any]:
    """Apply the watchdog policy across *nodes*; per-node faults isolated.

    ``ssh_factory(node)`` returns that node's ``(command, timeout) -> dict``
    transport (the test seam). ``ok`` is True only when every node is either
    protected or explicitly opted out; the summary names every node that is
    neither, because "3 of 4 protected" must not read as a successful run.
    """
    cfg = node_recovery_config(config_path)
    enabled = bool(cfg[WATCHDOG_SUBKEY])
    results: List[Dict[str, Any]] = []
    for node in nodes:
        try:
            if disarm:
                results.append(disarm_node(node, ssh_cmd=ssh_factory(node),
                                           timeout=timeout))
            elif not enabled:
                # Opt-out is a SKIP, not a removal: bootstrap must never delete
                # a file as a side effect of reading a config key. If the node
                # is still armed from an earlier run, say so and hand the
                # operator the explicit command (--disarm).
                results.append(_skip_disabled(node))
            else:
                results.append(arm_node(node, ssh_cmd=ssh_factory(node),
                                        dry_run=dry_run, timeout=timeout,
                                        enabled=True))
        except Exception as e:  # a crashing node must not lose the other nodes
            results.append(_result(node, ERROR, message=f"unexpected "
                                                        f"{type(e).__name__}: {e}"))
    opted = [r for r in results if r["outcome"] == SKIP_DISABLED]
    bad = [r for r in results if not r["armed"] and r["outcome"] != SKIP_DISABLED]
    return {
        "ok": not bad,
        "dry_run": dry_run,
        "enabled": enabled,
        "config_error": cfg.get("config_error") or "",
        "target": {"dropin": DROPIN_PATH,
                   "RuntimeWatchdogSec": f"{RUNTIME_WATCHDOG_SEC}s",
                   "RebootWatchdogSec": REBOOT_WATCHDOG_SEC},
        "nodes": results,
        "protected": [r["node"] for r in results if r["armed"]],
        "opted_out": [r["node"] for r in opted],
        "needing_attention": [{"node": r["node"], "outcome": r["outcome"],
                               "message": r["message"]} for r in bad],
    }


# ── Production transport + CLI ──────────────────────────────────────────────

def _bridge():
    """The sparkrun bridge, imported lazily (tests must not need sparkrun)."""
    try:
        from . import sparkrun_bridge  # package context (runtime)
    except ImportError:
        import sparkrun_bridge  # direct import (deployed flat dir / tests)
    return sparkrun_bridge


def _default_ssh_factory() -> Callable[[str], Runners]:
    """Production transport: sparkrun's own remote execution, bound per node.

    sparkrun owns host resolution and the ssh kwargs — this module must not
    hand-roll ssh (cf. hscc-cluster/sparkrun_bridge.py).
    """
    bridge = _bridge()

    def factory(node: str) -> Runners:
        def run(command: str, timeout: int) -> Dict[str, Any]:
            return bridge.remote_cmd(node, command, timeout=timeout)
        return run
    return factory


def _topology_hosts() -> List[str]:
    """Orchestrator + worker IPs via the ONE topology module; [] if unresolvable."""
    try:
        try:
            from . import discovery  # package context (runtime)
        except ImportError:
            import discovery  # direct import context
        topo = discovery.discover()
    except Exception:
        return []
    return [topo.orchestrator.ip] + [w.ip for w in topo.workers]


_MARK = {OK_APPLIED: "ok", OK_UNCHANGED: "ok", SKIP_DISABLED: "--",
         DRIFT: "XX", REFUSED_FOREIGN: "XX", ERROR: "XX"}


def format_report(summary: Dict[str, Any]) -> List[str]:
    """Plain-text per-node report (bootstrap embeds these lines; no Rich art)."""
    t = summary["target"]
    lines = [f"node-recovery watchdog — "
             f"{'DRY RUN (writes nothing)' if summary['dry_run'] else 'APPLY'}",
             f"  target: {t['dropin']} RuntimeWatchdogSec={t['RuntimeWatchdogSec']} "
             f"RebootWatchdogSec={t['RebootWatchdogSec']}"]
    if not summary["enabled"]:
        lines.append(f"  {NODE_RECOVERY_KEY}.{WATCHDOG_SUBKEY}=false in "
                     f"{cluster_config_path()} — skipped by request")
    if summary["config_error"]:
        lines.append(f"  WARN config: {summary['config_error']}")
    for r in summary["nodes"]:
        lines.append(f"  [{_MARK.get(r['outcome'], 'XX')}] {r['node']} "
                     f"{r['outcome']}: {r['message']}")
    if summary["needing_attention"]:
        lines.append(f"  {len(summary['needing_attention'])} node(s) are NOT "
                     f"protected and need attention")
    lines.append(f"  protected: {len(summary['protected'])}/{len(summary['nodes'])}"
                 f"; opted out: {len(summary['opted_out'])}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="node_recovery.py",
        description="Arm the SBSA hardware watchdog on cluster nodes via a "
                    "systemd RuntimeWatchdogSec drop-in (idempotent).")
    ap.add_argument("--hosts", default="",
                    help="comma-separated nodes; default: live topology "
                         "(orchestrator + workers)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact per-node plan; write nothing")
    ap.add_argument("--disarm", action="store_true",
                    help="remove HSCC's own drop-in (refuses a foreign file)")
    ap.add_argument("--probe-only", action="store_true",
                    help="read-only per-node state (what `hscc doctor` uses)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--config", default=None,
                    help=f"cluster config path to read {NODE_RECOVERY_KEY} from")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if not hosts:
        hosts = _topology_hosts()
    if not hosts:
        print("no cluster nodes resolved: `sparkrun cluster list` failed and "
              "~/.hscc/cluster.json is absent — pass --hosts", file=sys.stderr)
        return 2

    if args.probe_only:
        rows = [probe_node(h, ssh_cmd=_default_ssh_factory()(h), timeout=args.timeout)
                for h in hosts]
        print(json.dumps({"nodes": rows}, indent=2))
        return 0 if all(r["armed"] for r in rows) else 1

    summary = arm_fleet(hosts, ssh_factory=_default_ssh_factory(),
                        dry_run=args.dry_run, timeout=args.timeout,
                        config_path=args.config, disarm=args.disarm)
    print(json.dumps(summary, indent=2) if args.json
          else "\n".join(format_report(summary)))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
