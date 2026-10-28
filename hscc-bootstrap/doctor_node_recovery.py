"""Doctor coverage for the node-recovery layers (t_94598b3a).

Node-recovery batch part 3: the read-only check that answers, per cluster node,
"would this node come back on its own?" — the question the two install cards
cannot answer after they have run:

  * hardware watchdog (t_ebbe38d1, `hscc-cluster/node_recovery.py`): PID1 must
    run the SBSA watchdog at the installed target AND actually hold
    ``/dev/watchdog0`` open. A configured-but-not-petted device is NOT
    protection, so this check reuses the parent's ``probe_node`` — the one
    definition of "armed" — rather than inventing a second one.
  * panic-to-reboot sysctls (t_1fc3fe83, `install_autoreboot.py`): every knob
    the managed drop-in declares must read back at its expected value on the
    live node, and the drop-in on disk must still be the canonical content for
    that node's knob set. This reuses the parent's ``probe_command`` /
    ``MANAGED_KEYS`` / ``HARDLOCKUP_KEY`` / ``build_dropin`` — no re-derived
    key set, no re-typed timeout value, so the check and the installer can
    never disagree about what "in sync" means.

HONEST SIGNAL (the fleet rule this card exists to enforce)
----------------------------------------------------------
An environment fault is never rendered as a data fact. A node that is
unreachable, whose ``/proc/sys`` cannot be read, whose probe prints no
``HSCC_RC``, or whose ``sudo -n`` was refused comes back as a check **error**
naming the node and the reason — never ``armed``/``ok``, never a silent skip
(cf. ``hscc_daemon/health.py``'s ``HermesRuntimeUnavailable``: an unreadable
runtime raises, it does not report "healthy"). The same rule applies to the
opt-out: a node that opted out of a layer is reported as **opted out**, not as
green, because "nothing enforces this" and "this is enforced" must never share
a rendering.

READ-ONLY BY CONSTRUCTION
-------------------------
The only transport is the injected ``ssh_factory`` (production: sparkrun's own
remote execution through ``sparkrun_bridge``, resolved the same way
``install_autoreboot._default_ssh`` resolves it). Every command this module
sends is a probe the parents already expose — ``node_recovery``'s ``state``
mode and ``install_autoreboot.probe_command()`` — both of which only read.
There is no write path in this file, and nothing here escalates.

Address discipline: no real LAN/tailnet address appears here. Host strings come
from the live topology at run time; fixtures use the documented placeholders
(10.0.0.x for a LAN node).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional

# ── The shared surfaces, imported from the modules that own them ────────────
# Neither constant is re-declared here: RUNTIME_WATCHDOG_SEC, MANAGED_KEYS,
# HARDLOCKUP_KEY and the drop-in builder all stay single-sourced in the install
# cards. `install_autoreboot` sits in this dir (bare import); `node_recovery`
# lives in the sibling hscc-cluster dir, resolved exactly the way
# install_autoreboot._default_ssh resolves clusterlib — true in the work repo
# and in the deployed ~/.hermes/plugins layout.


def _cluster_dir() -> str:
    """The sibling hscc-cluster dir, APPENDED to sys.path when needed.

    Appended, never prepended: hscc-bootstrap has its own ``_theme.py`` and so
    does hscc-cluster, and a prepend would let a later ``import _theme`` in this
    leg silently pick up the cluster plugin's copy. Appending keeps every
    same-named bootstrap module authoritative while still resolving
    ``node_recovery`` / ``clusterlib``, which no bootstrap module shadows.
    """
    cluster_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "hscc-cluster")
    cluster_dir = os.path.abspath(cluster_dir)
    if cluster_dir not in sys.path:
        sys.path.append(cluster_dir)
    return cluster_dir


def _nr():
    """The watchdog module (node_recovery), or None when the layout lacks it."""
    _cluster_dir()
    try:
        import node_recovery  # noqa: PLC0415 - path set above
        return node_recovery
    except ImportError:
        return None


def _ar():
    """The sysctl installer (install_autoreboot), or None when unavailable."""
    try:
        import install_autoreboot  # noqa: PLC0415 - same dir / sys.path[0]
        return install_autoreboot
    except ImportError:
        return None


CHECK_NAME = "node recovery (watchdog + panic-to-reboot)"

#: Verdicts this check can give ONE layer on ONE node. Kept separate from the
#: parents' outcome vocabularies because the doctor's question is narrower: it
#: never installs, so there is no "applied"; and opt-out is its own answer, not
#: a flavour of ok.
V_OK = "ok"                # verified protected / verified in sync
V_FAIL = "fail"            # verified NOT protected — a real red line
V_ERROR = "error"          # cannot tell: env fault (never ok, never skipped)
V_OPTED_OUT = "opted_out"  # the operator turned this layer off (not green)

_MARK = {V_OK: "ok", V_FAIL: "XX", V_ERROR: "XX", V_OPTED_OUT: "--"}


# ── the sysctl layer ────────────────────────────────────────────────────────


def sysctl_layer(host: str, *, ssh_cmd: Callable[[str, int], Dict[str, Any]],
                 enabled: bool, timeout: int = 60) -> Dict[str, Any]:
    """Live drift report for the panic-to-reboot drop-in on ONE node.

    ``ssh_cmd(command, timeout)`` is the injected per-node transport. Returns
    {verdict, problems, notes} where ``problems`` are per-key red lines naming
    the node, the key, and actual-vs-expected.

    The expected set is whatever the parent's own probe says the node exposes
    (``KNOB`` lines), so kernel variance is the parent's decision, not a second
    opinion here. ``hardlockup_panic`` is expected only where the knob exists —
    exactly the installer's rule.
    """
    ar = _ar()
    if ar is None:
        return {"verdict": V_ERROR,
                "problems": [f"{host}: install_autoreboot.py unavailable — "
                             f"cannot verify the panic-to-reboot drop-in"],
                "notes": []}
    if not enabled:
        return {"verdict": V_OPTED_OUT, "problems": [],
                "notes": [f"{host}: node_recovery.autoreboot=false — nothing "
                          f"enforces panic-to-reboot on this node"]}

    p = ssh_cmd(ar.probe_command(), timeout)
    if not isinstance(p, dict) or not p.get("ok"):
        why = (p.get("stderr") if isinstance(p, dict) else None) or \
              f"probe rc={p.get('code') if isinstance(p, dict) else '?'}"
        # Unreachable / unreadable is an ERROR, not "drift-free".
        return {"verdict": V_ERROR,
                "problems": [f"{host}: sysctl probe failed — {why} (state unknown, "
                             f"NOT verified)"],
                "notes": []}

    state = ar.parse_probe(p.get("stdout") or "")
    knobs: Dict[str, str] = state["knobs"]
    file_content = state["file"]
    # The probe names every knob it can see (`KNOB <key>`) and prints a value
    # only for the ones it could read (`VAL <key> <value>`). parse_probe keeps
    # the values; the KNOB lines are what distinguishes "this kernel does not
    # expose the knob" (the parent skips it) from "the knob is there and the
    # node would not say what it holds" (a read fault, never a pass).
    head = (p.get("stdout") or "").partition("---FILE---")[0]
    exposed = [ln[len("KNOB "):].strip() for ln in head.splitlines()
               if ln.startswith("KNOB ")]
    seen = set(exposed) | set(knobs)
    if not seen:
        # The node answered but named NO managed knob: /proc/sys was not
        # readable (or the probe died mid-loop). That is a read failure — it
        # must never collapse into "nothing to check, so nothing is wrong".
        return {"verdict": V_ERROR,
                "problems": [f"{host}: sysctl probe reported no knobs at all — "
                             f"/proc/sys unreadable on this node (state unknown, "
                             f"NOT verified)"],
                "notes": []}

    # The installer's own expectation, rebuilt from the node's knob set.
    has_hl = ar.HARDLOCKUP_KEY in seen
    skipped = [k for k in ar.MANAGED_KEYS if k not in seen]
    if not has_hl:
        skipped.append(ar.HARDLOCKUP_KEY)
    wanted: Dict[str, str] = {k: v for k, v in ar.MANAGED_KEYS.items() if k in seen}
    if has_hl:
        wanted[ar.HARDLOCKUP_KEY] = ar.HARDLOCKUP_VALUE

    problems: List[str] = []
    notes: List[str] = []
    for key, exp in sorted(wanted.items()):
        actual = knobs.get(key)
        if not actual:
            # The existence probe named the knob but returned no value: the node
            # would not tell us what it holds. That is a read failure, so it is
            # a fault — never "matches nothing, therefore nothing is wrong".
            problems.append(f"{host}/{key}: value unreadable, expected {exp!r} "
                            f"(state unknown, NOT verified)")
            continue
        if actual != exp:
            problems.append(f"{host}/{key}: live {actual!r}, expected {exp!r}")

    for key in skipped:
        # The parent's own decision: a knob this kernel does not expose is
        # skipped, not failed. It still has to be VISIBLE — a layer that is
        # not enforced on a node must never render as plain green.
        notes.append(f"{host}/{key}: knob absent on this kernel — not enforced "
                     f"here (parent's conditional rule)")

    # The drop-in itself: the parent leaves an operator-edited file alone and
    # calls that drift, so the doctor must keep calling it red until resolved.
    expected_file = ar.build_dropin(has_hl, skip=tuple(k for k in skipped
                                                       if k != ar.HARDLOCKUP_KEY))
    if file_content is None:
        problems.append(f"{host}: {ar.DROPIN_PATH} absent — panic-to-reboot is "
                        f"NOT configured on this node")
    elif file_content != expected_file:
        if file_content in (ar.build_dropin(True), ar.build_dropin(False)):
            # One of OUR canonical variants, written for a different knob set
            # (the kernel later gained/lost the conditional knob). The
            # installer treats this as ours and upgrades it.
            problems.append(f"{host}: {ar.DROPIN_PATH} holds a stale managed "
                            f"key set for this kernel — re-run the bootstrap "
                            f"sysctl stage (it will upgrade the file)")
        else:
            problems.append(f"{host}: {ar.DROPIN_PATH} differs from the managed "
                            f"content (operator edit) — the installer reports "
                            f"drift and will not clobber it")

    if problems:
        return {"verdict": V_FAIL, "problems": problems, "notes": notes}
    return {"verdict": V_OK, "problems": [],
            "notes": notes + [f"{host}: {len(wanted)} panic knob(s) verified live"]}


# ── the watchdog layer ──────────────────────────────────────────────────────


def watchdog_layer(host: str, *, ssh_cmd: Callable[[str, int], Dict[str, Any]],
                   enabled: bool, timeout: int = 30) -> Dict[str, Any]:
    """Live "is anyone petting the watchdog" report for ONE node.

    Delegates the whole probe to the installer's ``probe_node`` (the shared
    definition of armed, including the sysfs-state / wdctl legs) and only maps
    its outcome onto the doctor's verdicts. An outcome outside the parent's
    ARMED_OUTCOMES is never promoted to ok.
    """
    nr = _nr()
    if nr is None:
        return {"verdict": V_ERROR,
                "problems": [f"{host}: hscc-cluster/node_recovery.py unavailable "
                             f"— cannot verify the hardware watchdog"],
                "notes": []}
    if not enabled:
        return {"verdict": V_OPTED_OUT, "problems": [],
                "notes": [f"{host}: node_recovery.watchdog=false — the hardware "
                          f"watchdog is NOT armed on this node"]}

    res = nr.probe_node(host, ssh_cmd=ssh_cmd, timeout=timeout)
    outcome = res.get("outcome")
    message = res.get("message") or ""
    state = res.get("state") or {}
    if outcome in nr.ARMED_OUTCOMES:
        # The parent already refuses an armed-looking node whose sysfs state is
        # `inactive`. One leg it treats as advisory is worth a red line here: if
        # `wdctl` is installed and prints no Timeout for the device, nothing has
        # a timeout set, whatever the config says.
        st = state
        usec = st.get("runtime_watchdog_usec") or "unset"
        identity = st.get("watchdog0_identity") or "the watchdog device"
        if st.get("wdctl") == "present" and not (st.get("wdctl_timeout") or "").strip():
            # wdctl IS installed and shows no Timeout for the device: nothing
            # has a timeout set, whatever the config value reads.
            return {"verdict": V_ERROR,
                    "problems": [f"{host}: wdctl reports no Timeout for {identity} "
                                 f"although RuntimeWatchdogUSec reads {usec!r} "
                                 f"(state unknown, NOT verified)"],
                    "notes": []}
        if st.get("wdctl") == "absent" and not (st.get("device_timeout") or "").strip():
            return {"verdict": V_ERROR,
                    "problems": [f"{host}: no timeout readable from wdctl or "
                                 f"sysfs for {identity} although "
                                 f"RuntimeWatchdogUSec reads {usec!r} "
                                 f"(state unknown, NOT verified)"],
                    "notes": []}
        return {"verdict": V_OK, "problems": [],
                "notes": [f"{host}: watchdog {message}"]}
    if outcome == nr.SKIP_DISABLED:
        return {"verdict": V_OPTED_OUT, "problems": [],
                "notes": [f"{host}: watchdog opted out ({message})"]}
    # The parent answers ERROR in two very different situations, and the doctor
    # must not blur them: (a) the probe reached the node and READ a state that
    # is not armed (RuntimeWatchdogUSec != target, no watchdog core, no
    # systemctl) — that is a DATA FACT, so the card's red line; (b) the probe
    # never got state at all (node unreachable, no HSCC_RC, sudo refused) —
    # that is an ENVIRONMENT FAULT, rendered as an error naming the reason and
    # never as armed/ok and never as a plain "not armed".
    if outcome == nr.ERROR and not state.get("transport_ok"):
        return {"verdict": V_ERROR,
                "problems": [f"{host}: watchdog UNVERIFIABLE — {message} "
                             f"(state unknown, NOT verified; target "
                             f"RuntimeWatchdogSec={nr.RUNTIME_WATCHDOG_SEC}s)"],
                "notes": []}
    verdict = V_FAIL
    target = f"{nr.RUNTIME_WATCHDOG_SEC}s"
    return {"verdict": verdict,
            "problems": [f"{host}: watchdog {outcome or 'unknown'} — {message} "
                         f"(target RuntimeWatchdogSec={target})"],
            "notes": []}


# ── the check ───────────────────────────────────────────────────────────────


def _refused_transport(reason: str) -> Callable[[str, int], Dict[str, Any]]:
    """A transport that only ever reports the fault — never an ok."""
    def run(_command: str, _timeout: int) -> Dict[str, Any]:
        return {"ok": False, "stdout": "", "code": None, "stderr": reason}
    return run


def _default_ssh_factory() -> Callable[[str], Callable[[str, int], Dict[str, Any]]]:
    """Production transport: sparkrun's own remote execution, bound per node.

    Resolves clusterlib the same way the sysctl card does (sparkrun owns host
    resolution and the ssh kwargs — nothing here hand-rolls ssh). If the bridge
    cannot be imported, every node comes back as an ERROR: an unreachable
    transport is reported, never assumed healthy.

    Refused outright inside a pytest process: a suite that forgot to inject its
    own transport must fail loudly rather than ssh to the live fleet.
    """
    if "pytest" in sys.modules:
        def refused_in_suite(_host: str):
            return _refused_transport("production ssh transport refused inside a "
                                      "pytest process — inject ssh_factory")
        return refused_in_suite
    cluster_dir = _cluster_dir()
    try:
        import clusterlib  # noqa: PLC0415 - path set above
    except ImportError as e:  # pragma: no cover - layout must provide it
        def broken(_host: str) -> Callable[[str, int], Dict[str, Any]]:
            def run(_cmd: str, _timeout: int) -> Dict[str, Any]:
                return {"ok": False, "stdout": "", "code": 127,
                        "stderr": f"clusterlib unavailable ({e}); sparkrun "
                                  f"bridge unreachable"}
            return run
        return broken

    def factory(host: str) -> Callable[[str, int], Dict[str, Any]]:
        def run(command: str, timeout: int) -> Dict[str, Any]:
            return clusterlib.ssh_cmd(host, command, timeout=timeout)
        return run
    return factory


def _topology_hosts() -> List[str]:
    """Cluster hosts via bootstrap's own detect path (sparkrun cluster list)."""
    boot_dir = os.path.dirname(os.path.abspath(__file__))
    if boot_dir not in sys.path:
        sys.path.insert(0, boot_dir)
    try:
        import detect  # noqa: PLC0415
    except ImportError:  # pragma: no cover - layout must provide it
        return []
    return list((detect.detect_cluster() or {}).get("hosts") or [])


def _opt_outs(hscc_dir: Optional[str]) -> Dict[str, Any]:
    """Both layer opt-outs + any config problem, from the parent's ONE reader.

    ``node_recovery_config`` defaults ON when the block or subkey is absent and
    keeps a non-boolean value ON while naming the problem, so a typo cannot
    silently disable a recovery layer and still read as green.
    """
    nr = _nr()
    if nr is None:
        return {"watchdog": True, "autoreboot": True,
                "config_error": "node_recovery.py unavailable — opt-outs unreadable"}
    path = os.path.join(hscc_dir or os.environ.get("HSCC_DIR")
                        or os.path.expanduser("~/.hscc"), "cluster.json")
    return nr.node_recovery_config(path)


def evaluate(hosts: List[str], *,
             ssh_factory: Optional[Callable[[str], Callable[[str, int], Dict[str, Any]]]] = None,
             hscc_dir: Optional[str] = None,
             timeout: int = 30) -> Dict[str, Any]:
    """Per-node verdicts for both layers. Returns rows + the check summary.

    One node failing never suppresses the others, and an empty host list is an
    honest ERROR (a doctor run that silently checks nothing is the false
    all-clear this check exists to prevent).
    """
    factory = ssh_factory or _default_ssh_factory()
    opt = _opt_outs(hscc_dir)
    rows: List[Dict[str, Any]] = []
    if not hosts:
        return {"rows": [], "error": "no cluster nodes resolved "
                                     "(`sparkrun cluster list` empty) — cannot "
                                     "verify node recovery on anything"}

    for host in hosts:
        ssh_cmd = factory(host)
        wd = watchdog_layer(host, ssh_cmd=ssh_cmd,
                            enabled=bool(opt["watchdog"]), timeout=timeout)
        sc = sysctl_layer(host, ssh_cmd=ssh_cmd,
                          enabled=bool(opt["autoreboot"]), timeout=max(timeout, 60))
        verdicts = [wd["verdict"], sc["verdict"]]
        if V_ERROR in verdicts:
            verdict = V_ERROR
        elif V_FAIL in verdicts:
            verdict = V_FAIL
        elif V_OPTED_OUT in verdicts:
            verdict = V_OPTED_OUT
        else:
            verdict = V_OK
        rows.append({"node": host, "verdict": verdict,
                     "watchdog": wd, "sysctl": sc,
                     "problems": wd["problems"] + sc["problems"],
                     "notes": wd["notes"] + sc["notes"]})

    return {"rows": rows, "error": None, "config_error": opt.get("config_error") or ""}


def check(hosts: Optional[List[str]] = None, *, ssh_factory=None,
          hscc_dir: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """The doctor check as a plain dict (Check-compatible fields).

    Non-fatal: an unprotected node is a loud red line, not a reason to abort an
    install — bootstrap's own stages are non-fatal for the same reason, and a
    node without a watchdog driver must never block the operator's install.

    The per-node ``rows`` ride along so a caller (or a test) can see exactly
    what each node reported instead of only the aggregated string; the doctor
    takes only the five Check fields (see ``run_doctor``).
    """
    target = hosts if hosts is not None else _topology_hosts()
    res = evaluate(target, ssh_factory=ssh_factory, hscc_dir=hscc_dir,
                   timeout=timeout)

    if res.get("error"):
        return {"name": CHECK_NAME, "ok": False, "fatal": False, "rows": [],
                "detail": res["error"],
                "fix": "Resolve the cluster topology (sparkrun cluster list) — "
                       "doctor cannot report node recovery for nodes it cannot "
                       "name."}

    rows: List[Dict[str, Any]] = res["rows"]
    protected = [r for r in rows if r["verdict"] == V_OK]
    errored = [r for r in rows if r["verdict"] == V_ERROR]
    failed = [r for r in rows if r["verdict"] == V_FAIL]
    opted = [r for r in rows if r["verdict"] == V_OPTED_OUT]

    summary = (f"{len(protected)}/{len(rows)} node(s) fully protected "
               f"(watchdog armed + panic sysctls in sync)")
    if errored:
        summary += f"; {len(errored)} unverifiable"
    if failed:
        summary += f"; {len(failed)} NOT protected"
    if opted:
        summary += f"; {len(opted)} opted out"
    if res.get("config_error"):
        summary += f"; CONFIG: {res['config_error']}"

    lines = [summary]
    for r in rows:
        # Every red line names node + key + actual-vs-expected (card rule). A
        # green node still prints its notes, so a knob this kernel does not
        # enforce is visible on an otherwise-ok row instead of vanishing.
        items = r["problems"] or r["notes"] or ["no detail"]
        mark = _MARK.get(r["verdict"], "XX")
        for i, item in enumerate(items):
            prefix = f"      [{mark}] {r['node']} {r['verdict']}: " if i == 0 \
                else f"      [{mark}] {r['node']} "
            lines.append(prefix + item)

    ok = not failed and not errored and not res.get("config_error")
    fix = ""
    if failed or errored:
        fix = ("Per-node: re-run `python3 <plugins>/hscc-cluster/node_recovery.py "
               "--probe-only` for the watchdog legs and the bootstrap sysctl "
               "stage's --dry-run for the knob legs. A node that reads XX here is "
               "NOT self-recovering: a hard hang wedges it until someone "
               "power-cycles it.")
    return {"name": CHECK_NAME, "ok": ok, "fatal": False, "rows": rows,
            "detail": "\n".join(lines), "fix": fix}


def main(argv: Optional[List[str]] = None, *, ssh_factory=None) -> int:
    """Standalone probe: `python3 doctor_node_recovery.py [--hosts a,b] [--json]`."""
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="doctor_node_recovery.py",
        description="Read-only per-node check of the node-recovery layers "
                    "(hardware watchdog + panic-to-reboot sysctls).")
    ap.add_argument("--hosts", default="",
                    help="comma-separated nodes; default: sparkrun cluster hosts")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()] or None
    res = check(hosts, ssh_factory=ssh_factory, timeout=args.timeout)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"  {'ok' if res['ok'] else 'XX'} {res['name']}: {res['detail']}")
        if res["fix"]:
            print(f"      -> {res['fix']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
