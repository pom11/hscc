"""Cap the GPU graphics clock on every cluster node (node-recovery layer).

Context: a GB10 Spark left on auto clocks runs its GPU at ~2400-2500 MHz under
load (max 3003) and spends long stretches power- and thermally-capped
(measured on the fleet 2026-10-29: ~85 h of cumulative SW power capping on one
node, GPUs at 71-76 C, CPUs at 77-87 C). A public two-Spark sweep (MiaAI-Lab,
DeepSeek V4.1 Flash, 2026-10-29) put the knee at **2200 MHz**: decode 100.2 %,
uncached prefill 95.3 %, overall 99.6 % of uncapped, for -32 % GPU power and
about -5 C mean / -7 C peak. Below 1900 MHz decode itself starts to drop. Decode
on GB10 is memory-bandwidth bound, which is why the clock barely moves it.

The cap is ``nvidia-smi -lgc 0,<mhz>`` (lock graphics clocks to [0, mhz]); undo
is ``nvidia-smi -rgc``. A lock does NOT survive a reboot, so the layer installs
one tiny systemd oneshot unit that re-applies it at boot:

    /etc/systemd/system/hscc-gpu-clock-cap.service
      ExecStart=/usr/bin/nvidia-smi -lgc 0,<mhz>
      ExecStop=/usr/bin/nvidia-smi -rgc

``RemainAfterExit=yes`` keeps the unit ``active`` after the oneshot ran, so
``is-active`` is the read-back that the cap was applied this boot, and
``systemctl stop`` is a clean undo (ExecStop resets the clocks). No running
model is restarted: a clock lock applies live.

Root: privileged steps use ``sudo -n`` (the install_autoreboot idiom — a
password prompt would hang the ssh session). A refused ``sudo -n`` fails THAT
node with a message saying so; it never reads as capped.

Read-back: a node counts as capped only when the unit file is present with the
wanted value, enabled, active, AND the GPU's current SM clock is at or under the
cap (plus a small tolerance — the lock lands on the nearest supported step, e.g.
2184 MHz for 2200). An idle GPU sits well under any cap, so a low clock alone
proves nothing; it is only the last of the four conditions.

Opt-out / value: ``~/.hscc/cluster.json`` ->
``{"node_recovery": {"gpu_clock_cap_mhz": 2200}}``; ``false`` turns the layer
off (default 2200 when the block or key is absent). Turning it off does NOT
remove an installed cap — undo is explicit (``--uninstall``), the same rule the
sibling layers follow. A value that is not an integer in
[MIN_CAP_MHZ, MAX_CAP_MHZ] keeps the default and is named in ``config_error``.

Transport: the sparkrun bridge only (``install_notifier_mask._default_ssh`` →
clusterlib → sparkrun_bridge). No hand-rolled ssh; tests inject the ``ssh_cmd``
seam and never contact a node.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

UNIT_NAME = "hscc-gpu-clock-cap.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
NVIDIA_SMI = "/usr/bin/nvidia-smi"

DEFAULT_CAP_MHZ = 2200
#: Sanity bounds for the configured value. Below ~1900 decode starts to suffer
#: (the source sweep); 1000 still allows a deliberate thermal-first setting.
MIN_CAP_MHZ = 1000
MAX_CAP_MHZ = 3003
#: The lock lands on a supported clock step at or below the request (2200 →
#: 2184 measured); a small slack above the cap absorbs a step reported a few
#: MHz high without letting an uncapped ~2400+ MHz GPU pass.
CLOCK_TOLERANCE_MHZ = 30

NODE_RECOVERY_KEY = "node_recovery"
CAP_SUBKEY = "gpu_clock_cap_mhz"

OK_APPLIED = "applied"
OK_UNCHANGED = "unchanged"
SKIP_DISABLED = "skipped_disabled"
ERROR = "error"
CAPPED_OUTCOMES = frozenset({OK_APPLIED, OK_UNCHANGED})

Runners = Callable[[str, int], Dict[str, Any]]


# ── config ──────────────────────────────────────────────────────────────────

def hscc_dir() -> str:
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def cluster_config_path() -> str:
    return os.path.join(hscc_dir(), "cluster.json")


def clock_cap_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Read ``node_recovery.gpu_clock_cap_mhz``.

    Returns ``{"enabled": bool, "cap_mhz": int, "config_error": str}``. Absent
    block/key → enabled at the default. ``false`` → disabled. Anything else that
    is not an in-range integer keeps the default and is named, because a
    silently-ignored setting looks exactly like a working one.
    """
    out: Dict[str, Any] = {"enabled": True, "cap_mhz": DEFAULT_CAP_MHZ,
                           "config_error": ""}
    target = path or cluster_config_path()
    try:
        with open(target) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return out
    except (json.JSONDecodeError, OSError) as e:
        out["config_error"] = (f"could not read {target} ({e.__class__.__name__}: {e}) "
                               f"— default {DEFAULT_CAP_MHZ} MHz applied")
        return out
    block = cfg.get(NODE_RECOVERY_KEY) if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or CAP_SUBKEY not in block:
        return out
    val = block[CAP_SUBKEY]
    if val is False:
        out["enabled"] = False
    elif isinstance(val, int) and not isinstance(val, bool) \
            and MIN_CAP_MHZ <= val <= MAX_CAP_MHZ:
        out["cap_mhz"] = val
    else:
        out["config_error"] = (f"{NODE_RECOVERY_KEY}.{CAP_SUBKEY}={val!r} is not false "
                               f"or an integer in [{MIN_CAP_MHZ}, {MAX_CAP_MHZ}] — "
                               f"kept {DEFAULT_CAP_MHZ} MHz")
    return out


# ── the unit + node-side script ─────────────────────────────────────────────

def unit_text(cap_mhz: int) -> str:
    """The exact unit file content for *cap_mhz* (byte-compared on the node)."""
    return (
        "# GENERATED by hscc-bootstrap/install_gpu_clock_cap.py — edit the module, not this file.\n"
        "[Unit]\n"
        f"Description=HSCC GPU clock cap (nvidia-smi -lgc 0,{cap_mhz})\n"
        "After=nvidia-persistenced.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart={NVIDIA_SMI} -lgc 0,{cap_mhz}\n"
        f"ExecStop={NVIDIA_SMI} -rgc\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# Contract: stdout lines are `HSCC_<KEY>: value`; the LAST line is always
# `HSCC_RC: <n>` (our code). A missing HSCC_RC means the script died — a fault.
_SCRIPT = r"""#!/usr/bin/env bash
set -u
MODE="__MODE__"
UNIT="__UNIT__"
UNIT_PATH="__UNIT_PATH__"
SMI="__SMI__"
say() { printf '%s\n' "$1"; }
fail() { say "HSCC_MSG: $2"; say "HSCC_RC: $1"; exit "$1"; }

[ -x "$SMI" ] || fail 3 "$SMI not found — no NVIDIA driver on this node"

emit_state() {
  if [ -f "$UNIT_PATH" ]; then
    say "HSCC_UNIT_PRESENT: 1"
    say "HSCC_UNIT_CAP: $(sed -n 's/^ExecStart=.* -lgc 0,\([0-9]*\).*/\1/p' "$UNIT_PATH" | head -1)"
  else
    say "HSCC_UNIT_PRESENT: 0"
  fi
  say "HSCC_UNIT_ENABLED: $(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
  say "HSCC_UNIT_ACTIVE: $(systemctl is-active "$UNIT" 2>/dev/null || true)"
  say "HSCC_SM_CLOCK: $("$SMI" --query-gpu=clocks.sm --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
  say "HSCC_MAX_SM: $("$SMI" --query-gpu=clocks.max.sm --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
}

case "$MODE" in
  state) emit_state; say "HSCC_RC: 0"; exit 0 ;;
  install)
    WANT="$(printf '%s' '__UNIT_B64__' | base64 -d)"
    if [ -f "$UNIT_PATH" ] && [ "$(cat "$UNIT_PATH")" = "$WANT" ] \
       && [ "$(systemctl is-enabled "$UNIT" 2>/dev/null)" = "enabled" ] \
       && [ "$(systemctl is-active "$UNIT" 2>/dev/null)" = "active" ]; then
      say "HSCC_ACTION: unchanged"
    else
      TMP="$(mktemp /tmp/hscc-gpu-cap.XXXXXX)" || fail 4 "mktemp failed"
      printf '%s\n' "$WANT" > "$TMP"
      OUT="$(sudo -n install -m 0644 -o root -g root "$TMP" "${UNIT_PATH}.hscc-new" 2>&1)" \
        || { rm -f "$TMP"; fail 5 "sudo -n install refused: $OUT"; }
      rm -f "$TMP"
      OUT="$(sudo -n mv -f "${UNIT_PATH}.hscc-new" "$UNIT_PATH" 2>&1)" || fail 5 "sudo -n mv refused: $OUT"
      OUT="$(sudo -n systemctl daemon-reload 2>&1)" || fail 6 "daemon-reload failed: $OUT"
      OUT="$(sudo -n systemctl enable "$UNIT" 2>&1)" || fail 6 "enable failed: $OUT"
      # restart = ExecStop (-rgc) then ExecStart (-lgc) — applies a changed value live
      OUT="$(sudo -n systemctl restart "$UNIT" 2>&1)" || fail 6 "restart failed: $OUT"
      # A loaded GPU takes a moment to step down after the lock (measured: read
      # 1 s after restart still showed 2353-2431 MHz on 2 of 4 nodes, capped on
      # re-probe). Wait up to ~5 s so the read-back sees the settled clock.
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        C="$("$SMI" --query-gpu=clocks.sm --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
        [ -n "$C" ] && [ "$C" -le __SETTLE_MAX__ ] 2>/dev/null && break
        sleep 0.5
      done
      say "HSCC_ACTION: applied"
    fi
    emit_state; say "HSCC_RC: 0"; exit 0 ;;
  uninstall)
    if [ -f "$UNIT_PATH" ]; then
      OUT="$(sudo -n systemctl disable --now "$UNIT" 2>&1)" || fail 6 "disable --now failed: $OUT"
      OUT="$(sudo -n rm -f "$UNIT_PATH" 2>&1)" || fail 5 "sudo -n rm refused: $OUT"
      sudo -n systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    OUT="$(sudo -n "$SMI" -rgc 2>&1)" || fail 5 "nvidia-smi -rgc failed: $OUT"
    say "HSCC_ACTION: removed"
    emit_state; say "HSCC_RC: 0"; exit 0 ;;
  *) fail 2 "unknown mode '$MODE'" ;;
esac
"""


def install_script(mode: str, cap_mhz: int = DEFAULT_CAP_MHZ, *,
                   unit_path: str = UNIT_PATH, smi: str = NVIDIA_SMI) -> str:
    """Node-side bash for *mode*. ``unit_path``/``smi`` are a test seam only
    (the bash-backed tests point them into a sandbox); production uses the
    defaults, which the doctor shares."""
    if mode not in ("state", "install", "uninstall"):
        raise ValueError(f"unknown mode {mode!r}")
    unit_b64 = base64.b64encode(unit_text(cap_mhz).rstrip("\n").encode()).decode()
    return (_SCRIPT.replace("__MODE__", mode)
            .replace("__UNIT_PATH__", unit_path)
            .replace("__UNIT_B64__", unit_b64)
            .replace("__UNIT__", UNIT_NAME)
            .replace("__SMI__", smi)
            .replace("__SETTLE_MAX__", str(cap_mhz + CLOCK_TOLERANCE_MHZ)))


def command_for(mode: str, cap_mhz: int = DEFAULT_CAP_MHZ) -> str:
    """The remote command: the script base64'd and piped to bash (byte-exact)."""
    b64 = base64.b64encode(install_script(mode, cap_mhz).encode()).decode()
    return f"printf '%s' '{b64}' | base64 -d | bash -s"


# ── parsing + verdicts ──────────────────────────────────────────────────────

def _hscc_lines(stdout: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if line.startswith("HSCC_") and ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _int(text: str) -> Optional[int]:
    t = (text or "").strip()
    return int(t) if t.isdigit() else None


def _state_from(lines: Dict[str, str]) -> Dict[str, Any]:
    return {
        "unit_present": lines.get("HSCC_UNIT_PRESENT") == "1",
        "unit_cap_mhz": _int(lines.get("HSCC_UNIT_CAP", "")),
        "enabled": lines.get("HSCC_UNIT_ENABLED", ""),
        "active": lines.get("HSCC_UNIT_ACTIVE", ""),
        "sm_clock_mhz": _int(lines.get("HSCC_SM_CLOCK", "")),
        "max_sm_mhz": _int(lines.get("HSCC_MAX_SM", "")),
    }


def cap_problems(state: Dict[str, Any], cap_mhz: int) -> List[str]:
    """Why *state* is NOT a node capped at *cap_mhz* ([] = capped).

    The one definition of "capped", shared by the installer and the doctor.
    """
    probs: List[str] = []
    if not state.get("unit_present"):
        probs.append(f"{UNIT_PATH} missing (cap would not survive a reboot)")
    elif state.get("unit_cap_mhz") != cap_mhz:
        probs.append(f"unit caps at {state.get('unit_cap_mhz')} MHz, want {cap_mhz}")
    if state.get("enabled") != "enabled":
        probs.append(f"unit is-enabled={state.get('enabled') or '?'}")
    if state.get("active") != "active":
        probs.append(f"unit is-active={state.get('active') or '?'} (cap not applied this boot)")
    clk = state.get("sm_clock_mhz")
    if clk is None:
        probs.append("SM clock unreadable")
    elif clk > cap_mhz + CLOCK_TOLERANCE_MHZ:
        probs.append(f"SM clock {clk} MHz is above the {cap_mhz} MHz cap")
    return probs


def _result(node: str, outcome: str, message: str,
            state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"node": node, "outcome": outcome, "capped": outcome in CAPPED_OUTCOMES,
            "message": message, "state": state or {}}


def _run(node: str, mode: str, cap_mhz: int, ssh_cmd: Runners,
         timeout: int) -> Dict[str, Any]:
    """One round trip → ``{"ok", "lines", "state", "error"}`` (never raises)."""
    try:
        res = ssh_cmd(command_for(mode, cap_mhz), timeout)
    except Exception as e:  # a per-node fault must not abort a fleet pass
        return {"ok": False, "error": f"unexpected {type(e).__name__}: {e}"}
    if not isinstance(res, dict):
        return {"ok": False, "error": f"transport returned {type(res).__name__}"}
    lines = _hscc_lines(res.get("stdout") or "")
    rc = lines.get("HSCC_RC")
    if rc != "0":
        why = lines.get("HSCC_MSG") or (res.get("stderr") or "").strip() \
            or res.get("error") or f"probe incomplete (rc={rc!r})"
        return {"ok": False, "error": why, "lines": lines}
    return {"ok": True, "lines": lines, "state": _state_from(lines)}


def probe_node(node: str, cap_mhz: int, *, ssh_cmd: Runners,
               timeout: int = 30) -> Dict[str, Any]:
    """Read-only per-node verdict (the doctor's surface). Never writes."""
    r = _run(node, "state", cap_mhz, ssh_cmd, timeout)
    if not r["ok"]:
        return _result(node, ERROR, f"{node}: UNVERIFIABLE — {r['error']}")
    probs = cap_problems(r["state"], cap_mhz)
    if probs:
        return _result(node, ERROR, f"{node}: NOT CAPPED — {'; '.join(probs)}", r["state"])
    return _result(node, OK_UNCHANGED,
                   f"capped at {cap_mhz} MHz (SM now {r['state']['sm_clock_mhz']} MHz, "
                   f"unit enabled+active)", r["state"])


def cap_node(node: str, cap_mhz: int, *, ssh_cmd: Runners, dry_run: bool = False,
             timeout: int = 60) -> Dict[str, Any]:
    """Ensure *node* is capped at *cap_mhz*; read-back verified. Never raises."""
    if dry_run:
        r = _run(node, "state", cap_mhz, ssh_cmd, timeout)
        if not r["ok"]:
            return _result(node, ERROR, f"{node}: UNVERIFIABLE — {r['error']}")
        probs = cap_problems(r["state"], cap_mhz)
        return _result(node, OK_APPLIED if probs else OK_UNCHANGED,
                       (f"would install {UNIT_PATH} (nvidia-smi -lgc 0,{cap_mhz}) — "
                        f"now: {'; '.join(probs)}" if probs
                        else "already capped — would be a no-op"), r["state"])
    r = _run(node, "install", cap_mhz, ssh_cmd, timeout)
    if not r["ok"]:
        return _result(node, ERROR, f"{node}: {r['error']}")
    action = r["lines"].get("HSCC_ACTION") or "applied"
    probs = cap_problems(r["state"], cap_mhz)
    if probs:
        return _result(node, ERROR,
                       f"{node}: install reported {action} but read-back says: "
                       f"{'; '.join(probs)}", r["state"])
    return _result(node, OK_UNCHANGED if action == "unchanged" else OK_APPLIED,
                   f"capped at {cap_mhz} MHz (SM now {r['state']['sm_clock_mhz']} MHz, "
                   f"read-back verified)", r["state"])


def uncap_node(node: str, *, ssh_cmd: Runners, timeout: int = 60) -> Dict[str, Any]:
    """Explicit undo: disable + remove the unit, ``nvidia-smi -rgc``."""
    r = _run(node, "uninstall", DEFAULT_CAP_MHZ, ssh_cmd, timeout)
    if not r["ok"]:
        return _result(node, ERROR, f"{node}: {r['error']}")
    return _result(node, OK_APPLIED,
                   f"cap removed, clocks reset (SM now {r['state']['sm_clock_mhz']} MHz)",
                   r["state"])


def cap_fleet(nodes: List[str], *, ssh_factory: Callable[[str], Runners],
              dry_run: bool = False, uninstall: bool = False,
              config_path: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    """Apply the policy across *nodes*. ``ok`` only when every node is capped
    (or the layer is opted out) — "3 of 4 capped" is not a success."""
    cfg = clock_cap_config(config_path)
    results: List[Dict[str, Any]] = []
    for node in nodes:
        if uninstall:
            results.append(uncap_node(node, ssh_cmd=ssh_factory(node), timeout=timeout))
        elif not cfg["enabled"]:
            results.append(_result(node, SKIP_DISABLED,
                                   f"{NODE_RECOVERY_KEY}.{CAP_SUBKEY}=false — skipped; an "
                                   f"earlier cap stays until `install_gpu_clock_cap.py "
                                   f"--uninstall --hosts {node}`"))
        else:
            results.append(cap_node(node, cfg["cap_mhz"], ssh_cmd=ssh_factory(node),
                                    dry_run=dry_run, timeout=timeout))
    if uninstall:
        bad = [r for r in results if r["outcome"] == ERROR]
    else:
        bad = [r for r in results if not r["capped"] and r["outcome"] != SKIP_DISABLED]
    return {"ok": not bad, "dry_run": dry_run, "uninstall": uninstall,
            "enabled": cfg["enabled"], "cap_mhz": cfg["cap_mhz"],
            "config_error": cfg["config_error"], "nodes": results,
            "capped": [r["node"] for r in results if r["capped"]],
            "needing_attention": [{"node": r["node"], "message": r["message"]} for r in bad]}


def bootstrap_lines(summary: Dict[str, Any]) -> List[str]:
    """``OK|`` / ``WARN|`` lines for bootstrap.sh (first line = stage verdict)."""
    n = len(summary["nodes"])
    if not summary["enabled"]:
        return [f"OK|GPU clock cap skipped — {NODE_RECOVERY_KEY}.{CAP_SUBKEY} is false"]
    if not n:
        return ["WARN|no nodes were checked — is the sparkrun cluster configured?"]
    verb = "planned (dry-run, nothing written)" if summary["dry_run"] \
        else "capped + read-back verified"
    out = [f"{'OK' if summary['ok'] else 'WARN'}|GPU clock cap {summary['cap_mhz']} MHz "
           f"{verb} on {len(summary['capped']) if not summary['dry_run'] else n}/{n} node(s)"]
    for r in summary["nodes"]:
        out.append(f"{'OK' if r['outcome'] != ERROR else 'WARN'}|{r['node']}: {r['message']}")
    if summary["config_error"]:
        out.append(f"WARN|config: {summary['config_error']}")
    return out


# ── doctor check ────────────────────────────────────────────────────────────

CHECK_NAME = "GPU clock cap (node-recovery)"


def doctor_check(hosts: List[str], *, ssh_factory: Optional[Callable[[str], Runners]] = None,
                 config_path: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """Check-shaped dict (name/ok/detail/fix/fatal). Non-fatal; read-only."""
    cfg = clock_cap_config(config_path)
    if not cfg["enabled"]:
        return {"name": CHECK_NAME, "ok": True, "fatal": False, "fix": "",
                "detail": f"opted out ({NODE_RECOVERY_KEY}.{CAP_SUBKEY}=false)"}
    if not hosts:
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "no cluster hosts resolved — cap cannot be verified",
                "fix": "Resolve the cluster topology (sparkrun cluster list)."}
    factory = ssh_factory or _default_ssh_factory()
    rows = [probe_node(h, cfg["cap_mhz"], ssh_cmd=factory(h), timeout=timeout) for h in hosts]
    bad = [r for r in rows if not r["capped"]]
    detail = (f"{len(rows) - len(bad)}/{len(rows)} node(s) capped at {cfg['cap_mhz']} MHz"
              + ("; " + "; ".join(r["message"] for r in bad) if bad else ""))
    if cfg["config_error"]:
        detail += f"; config: {cfg['config_error']}"
    return {"name": CHECK_NAME, "ok": not bad, "fatal": False, "detail": detail,
            "fix": ("" if not bad else
                    "Re-run bootstrap, or: python3 hscc-bootstrap/install_gpu_clock_cap.py "
                    "--hosts <node,...> (needs NOPASSWD sudo on the node)")}


# ── production transport + CLI ──────────────────────────────────────────────

def _refused(_cmd: str, _timeout: int) -> Dict[str, Any]:
    return {"ok": False, "stdout": "", "stderr": "production ssh transport refused "
            "inside a pytest process — inject ssh_factory", "code": None}


def _default_ssh_factory() -> Callable[[str], Runners]:
    """The installers' sanctioned sparkrun route, bound per node; refused under pytest."""
    if "pytest" in sys.modules:
        return lambda _host: _refused
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import install_notifier_mask as nm  # noqa: PLC0415 - shares its bridge resolution
    return lambda host: (lambda cmd, to: nm._default_ssh(host, cmd, to))


def parse_hosts(argv: List[str]) -> List[str]:
    """``--hosts a,b c`` → [a, b, c] (bootstrap passes one comma-joined arg)."""
    if "--hosts" not in argv:
        return []
    i = argv.index("--hosts")
    hosts: List[str] = []
    for tok in argv[i + 1:]:
        if tok.startswith("--"):
            break
        hosts.extend(h for h in tok.split(",") if h)
    return hosts


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    hosts = parse_hosts(argv)
    if not hosts:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import install_notifier_mask as nm  # noqa: PLC0415
        hosts = nm._hosts_from_detect()
    if not hosts:
        print(json.dumps({"ok": False, "error": "no hosts: pass --hosts or configure a "
                                                "sparkrun cluster"}))
        return 2
    config_path = argv[argv.index("--config") + 1] if "--config" in argv else None
    summary = cap_fleet(hosts, ssh_factory=_default_ssh_factory(),
                        dry_run="--dry-run" in argv, uninstall="--uninstall" in argv,
                        config_path=config_path)
    if "--bootstrap-lines" in argv:
        print("\n".join(bootstrap_lines(summary)))
    else:
        print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
