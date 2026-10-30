"""Doctor coverage for the snap firmware-notifier mask (t_61850b24).

Node-recovery batch part 5's OWN check (unlike the watchdog/sysctl layers, whose
doctor coverage is the sibling card t_94598b3a — this check deliberately does
not touch those layers, so the two never duplicate or contradict each other).

The question it answers per cluster node: **can snap firmware-updater's desktop
notifier still start, and is it still crash-looping there?** A masked unit
cannot load at all, so "masked" is the answer that means "this node's journal is
not being shredded ~5x a second by a notifier that can never succeed".

ONE SOURCE OF TRUTH
-------------------
Nothing is re-declared here. The unit names, the "is it masked" predicate, the
recognised-state vocabulary, the outcome vocabulary and the node-side probe are
all taken from ``install_notifier_mask.py`` (``probe_node`` /
``is_masked_state`` / ``MANAGED_STATE_WORDS`` / ``MASKED_OUTCOMES`` /
``NOTIFIER_UNITS`` / ``notifier_mask_config``) — which is itself the installer.
A check that re-typed ``snap.firmware-updater.firmware-notifier.service`` could
go green against a unit nobody installs any more; this one cannot.

HONEST SIGNAL (the fleet rule; the reason this check splits three verdicts)
--------------------------------------------------------------------------
An environment fault is never rendered as a data fact:

  * ``fail``   — the node ANSWERED and the answer is "the notifier can still
                 load" (or a foreign mask link sits in the way). Real red line.
  * ``error``  — we cannot tell: the node is unreachable, or ``systemctl
                 --user`` could not reach the session bus. The classic trap here
                 is the no-bus case: ``systemctl --user`` without
                 ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` prints
                 ``Failed to connect to bus: No medium found`` (measured on this
                 fleet via ``env -i``) and exits 1. Read naively, that is
                 "state != masked" -> red -> "not masked". It is not a statement
                 about the unit at all, so the parent refuses to parse it as a
                 state and this check renders ``UNVERIFIABLE … NOT verified``.
  * ``opted_out`` — the operator turned the layer off
                 (``node_recovery.firmware_notifier_mask=false``). NOT green:
                 "nothing enforces this" and "this is enforced" must not share a
                 rendering.

READ-ONLY BY CONSTRUCTION
-------------------------
The only transport is the injected ``ssh_factory``; the only command sent is the
parent's ``state`` mode, which prints and never writes (no mask, no unmask, no
daemon-reload, and no sudo anywhere in the parent). There is no write path in
this file.

Address discipline: no real LAN/tailnet address appears here. Host strings come
from the live topology at run time; fixtures use the documented placeholders
(10.0.0.x for a LAN node).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional

CHECK_NAME = "snap firmware-notifier mask (node-recovery)"

#: Verdicts for ONE node. Deliberately narrower than the installer's outcome
#: vocabulary: this check never installs, so there is no "applied".
V_OK = "ok"                # verified masked (read back from the node)
V_FAIL = "fail"            # verified NOT masked — a real red line
V_ERROR = "error"          # cannot tell: env fault (never ok, never a skip)
V_OPTED_OUT = "opted_out"  # operator turned the layer off (not green)

_MARK = {V_OK: "ok", V_FAIL: "XX", V_ERROR: "XX", V_OPTED_OUT: "--"}


def _installer():
    """The installer/probe module, or None when the layout lacks it.

    Same dir in the work repo and in the deployed ``~/.hermes/plugins`` layout,
    so a bare import is correct — but a missing sibling must surface as an
    ERROR naming itself, never as a silently-absent check.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import install_notifier_mask  # noqa: PLC0415 - path set above
        return install_notifier_mask
    except ImportError:
        return None


def _opt_out(hscc_dir: Optional[str]) -> Dict[str, Any]:
    """The layer's opt-out + any config problem, from the parent's ONE reader."""
    nm = _installer()
    if nm is None:
        return {"firmware_notifier_mask": True,
                "config_error": "install_notifier_mask.py unavailable "
                                "— opt-out unreadable"}
    base = hscc_dir or os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")
    return nm.notifier_mask_config(os.path.join(base, "cluster.json"))


def layer(host: str, *, ssh_cmd: Callable[[str, int], Dict[str, Any]],
          enabled: bool, timeout: int = 30) -> Dict[str, Any]:
    """Live "is the notifier quarantined" report for ONE node.

    Delegates the whole probe to the installer's :func:`probe_node` — the one
    definition of masked — and only maps its outcome onto the doctor's
    verdicts. An outcome outside the parent's ``MASKED_OUTCOMES`` is never
    promoted to ok.
    """
    nm = _installer()
    if nm is None:
        return {"verdict": V_ERROR,
                "problems": [f"{host}: hscc-bootstrap/install_notifier_mask.py "
                             f"unavailable — cannot verify the notifier mask"],
                "notes": []}
    if not enabled:
        return {"verdict": V_OPTED_OUT, "problems": [],
                "notes": [f"{host}: {nm.NODE_RECOVERY_KEY}.{nm.MASK_SUBKEY}=false — "
                          f"the notifier is NOT masked on this node (opt-out)"]}

    res = nm.probe_node(host, ssh_cmd=ssh_cmd, timeout=timeout)
    outcome = res.get("outcome")
    message = res.get("message") or ""
    state = res.get("state") or {}

    if outcome in nm.MASKED_OUTCOMES:
        return {"verdict": V_OK, "problems": [], "notes": [f"{host}: {message}"]}
    if outcome == nm.SKIP_DISABLED:
        return {"verdict": V_OPTED_OUT, "problems": [],
                "notes": [f"{host}: mask opted out ({message})"]}
    if outcome == nm.DRIFT:
        # A user unit override we did not write, sitting at the mask path.
        return {"verdict": V_FAIL,
                "problems": [f"{host}: {message}"],
                "notes": []}
    # The parent answers ERROR in two different situations and the doctor must
    # not blur them:
    #   (a) it reached the node and READ a state that is not a mask — a DATA
    #       FACT, so the card's red line ("red when the notifier is not masked");
    #   (b) it never got a state at all (node unreachable, no HSCC_RC, no bus) —
    #       an ENVIRONMENT FAULT: an error naming the reason, never "not masked"
    #       and never ok.
    if not state.get("transport_ok"):
        extra = ""
        bus = res.get("bus_text") or state.get("bus_text") or ""
        if bus:
            extra = f" [systemctl said: {bus!r}]"
        elif (res.get("code_name") or "") == "NO_BUS":
            extra = " [no user session bus reachable]"
        return {"verdict": V_ERROR,
                "problems": [f"{host}: notifier mask UNVERIFIABLE — {message} "
                             f"(state unknown, NOT verified{extra})"],
                "notes": []}
    return {"verdict": V_FAIL,
            "problems": [f"{host}: {message}"],
            "notes": []}


def _refused_transport(reason: str) -> Callable[[str, int], Dict[str, Any]]:
    """A transport that only ever reports the fault — never an ok."""
    def run(_command: str, _timeout: int) -> Dict[str, Any]:
        return {"ok": False, "stdout": "", "code": None, "stderr": reason}
    return run


def _default_ssh_factory() -> Callable[[str], Callable[[str, int], Dict[str, Any]]]:
    """Production transport: the installer's own sparkrun route, bound per node.

    Not a second transport — the parent's ``_default_ssh`` (clusterlib ->
    sparkrun_bridge -> sparkrun's ``run_remote_script``), so the doctor and the
    installer can never reach the fleet by different paths. Refused outright
    inside a pytest process: a suite that forgot to inject its transport must
    fail loudly rather than ssh to the live fleet.
    """
    if "pytest" in sys.modules:
        def refused_in_suite(_host: str):
            return _refused_transport("production ssh transport refused inside a "
                                      "pytest process — inject ssh_factory")
        return refused_in_suite
    nm = _installer()
    if nm is None:
        def broken(_host: str) -> Callable[[str, int], Dict[str, Any]]:
            return _refused_transport("install_notifier_mask.py unavailable; "
                                      "cannot reach the fleet")
        return broken
    return lambda host: (lambda cmd, to: nm._default_ssh(host, cmd, to))


def _topology_hosts() -> List[str]:
    """Cluster hosts via bootstrap's own detect path (sparkrun cluster list).

    Returns [] under pytest: a bare ``check()`` call in the suite must never
    shell out to the live sparkrun CLI.
    """
    if "pytest" in sys.modules:
        return []
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import detect  # noqa: PLC0415
    except ImportError:  # pragma: no cover - layout must provide it
        return []
    return list((detect.detect_cluster() or {}).get("hosts") or [])


def evaluate(hosts: List[str], *,
             ssh_factory: Optional[Callable[[str], Callable[[str, int], Dict[str, Any]]]] = None,
             hscc_dir: Optional[str] = None,
             timeout: int = 30) -> Dict[str, Any]:
    """Per-node verdicts. Returns rows + an error when nothing could be checked.

    One node failing never suppresses the others, and an empty host list is an
    honest error — a check that silently examined nothing is the false all-clear
    this check exists to prevent.
    """
    nm = _installer()
    factory = ssh_factory or _default_ssh_factory()
    opt = _opt_out(hscc_dir)
    enabled = bool(opt[nm.MASK_SUBKEY]) if nm is not None else True
    if not hosts:
        return {"rows": [], "error": "no cluster nodes resolved "
                                     "(`sparkrun cluster list` empty) — cannot "
                                     "verify the firmware-notifier mask on anything"}
    rows: List[Dict[str, Any]] = []
    for host in hosts:
        r = layer(host, ssh_cmd=factory(host), enabled=enabled, timeout=timeout)
        rows.append({"node": host, "verdict": r["verdict"],
                     "problems": r["problems"], "notes": r["notes"]})
    return {"rows": rows, "error": None, "config_error": opt.get("config_error") or ""}


def check(hosts: Optional[List[str]] = None, *, ssh_factory=None,
          hscc_dir: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """The doctor check as a plain dict (Check-compatible fields).

    Non-fatal: an unmasked node is a loud red line, not a reason to abort an
    install — the install stage is non-fatal for the same reason (a node whose
    user session is unreachable must never block the operator's install).

    The per-node ``rows`` ride along so a caller (or a test) can see what each
    node reported; ``run_doctor`` takes only the five Check fields.
    """
    target = hosts if hosts is not None else _topology_hosts()
    res = evaluate(target, ssh_factory=ssh_factory, hscc_dir=hscc_dir,
                   timeout=timeout)

    if res.get("error"):
        return {"name": CHECK_NAME, "ok": False, "fatal": False, "rows": [],
                "detail": res["error"],
                "fix": "Resolve the cluster topology (sparkrun cluster list) — "
                       "doctor cannot report a per-node mask for nodes it cannot "
                       "name."}

    rows: List[Dict[str, Any]] = res["rows"]
    ok_rows = [r for r in rows if r["verdict"] == V_OK]
    errored = [r for r in rows if r["verdict"] == V_ERROR]
    failed = [r for r in rows if r["verdict"] == V_FAIL]
    opted = [r for r in rows if r["verdict"] == V_OPTED_OUT]

    summary = (f"{len(ok_rows)}/{len(rows)} node(s) have the snap "
               f"firmware-notifier masked (read back from the node)")
    if errored:
        summary += f"; {len(errored)} unverifiable"
    if failed:
        summary += f"; {len(failed)} NOT masked"
    if opted:
        summary += f"; {len(opted)} opted out"
    if res.get("config_error"):
        summary += f"; CONFIG: {res['config_error']}"

    lines = [summary]
    for r in rows:
        # Every red line names the node and the reason (card rule). A green node
        # still prints its notes, so a --runtime mask or an opt-out stays
        # visible on an otherwise-ok row instead of vanishing.
        items = r["problems"] or r["notes"] or ["no detail"]
        mark = _MARK.get(r["verdict"], "XX")
        for i, item in enumerate(items):
            prefix = f"      [{mark}] {r['node']} {r['verdict']}: " if i == 0 \
                else f"      [{mark}] {r['node']} "
            lines.append(prefix + item)

    ok = not failed and not errored and not res.get("config_error")
    fix = ""
    if failed or errored:
        fix = ("Per-node: `python3 <plugins>/hscc-bootstrap/install_notifier_mask.py "
               "--hosts <node>` masks it (unprivileged; read-back verified), "
               "`--dry-run` shows the plan first. A node reading XX here still has "
               "snap firmware-updater's notifier crash-looping in the desktop "
               "user's session — log churn only, no firmware loss: the snap itself "
               "stays installed and `fwupd` is untouched.")
    return {"name": CHECK_NAME, "ok": ok, "fatal": False, "rows": rows,
            "detail": "\n".join(lines), "fix": fix}


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone read-only probe: `python3 doctor_notifier_mask.py [--hosts a,b] [--json]`."""
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="doctor_notifier_mask.py",
        description="Read-only per-node check that snap firmware-updater's "
                    "firmware-notifier is masked for the desktop user.")
    ap.add_argument("--hosts", default="",
                    help="comma-separated nodes; default: sparkrun cluster hosts")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()] or None
    res = check(hosts, timeout=args.timeout)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"  {'ok' if res['ok'] else 'XX'} {res['name']}: {res['detail']}")
        if res["fix"]:
            print(f"      -> {res['fix']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
