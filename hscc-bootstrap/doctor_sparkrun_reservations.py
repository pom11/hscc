"""Doctor coverage for stale sparkrun reservations (t_7dd19947).

The defect this row exists for: a killed sparkrun client leaves a reservation
row in sparkrun's operations ledger whose owner pid is dead, and the NEXT
launch/proxy start then fails ``Execution resources are busy or require
reconciliation after an interrupted operation`` (what blocked the .244
orchestrator launch during the template switch). Nothing in hscc could see
those reservations, so a stale one read as real contention and the only
recovery was operator archaeology.

TWO RULES DECIDE THIS FILE
--------------------------
**Liveness is a fleet fact.** The ledger and the owner process both live on
the node, so the question is asked ON the node. A local pid probe here would
be false-empty (the processes are on the DGX nodes), and a false-empty reads
as "no reservations" — worse than an error, because it clears the way for a
launch that then fails. So the probe ships as a script that runs on the node
under the node's OWN sparkrun venv python (resolved from the node's
``sparkrun`` CLI shebang, the same shape ``hscc_daemon/health.py``'s
``_SPARKRUN_STATUS_SCRIPT`` resolves locally) and prints one JSON document.

**The ledger is sparkrun's.** hscc never opens ``operations.sqlite3``. It
calls the two documented sparkrun functions, on the node, through sparkrun's
own remote-execution bridge (``clusterlib.ssh_cmd`` ->
``sparkrun_bridge.remote_cmd`` -> sparkrun's ``run_remote_script``):

* ``sparkrun.core.coordination.reservations(root)`` — every claim, each with
  ``{owner, resource, process, alive}``. ``alive`` is sparkrun's OWN verdict
  (``process_identity.alive``, a ``/proc`` identity compare on Linux), made by
  the node about its own processes — which is exactly why the query must run
  there and not here.
* ``sparkrun.core.coordination.reconcile_reservation(root, owner,
  acknowledge_effects=True)`` — releases ONE owner's claims and REFUSES
  (``ResourceBusy``) if that owner turns out alive; it re-checks liveness
  inside its own ``BEGIN IMMEDIATE`` transaction, so the delete is guarded by
  sparkrun's code, not by ours. The release is per-owner and idempotent.

``acknowledge_effects=True`` is passed only on the ``--fix`` path, where the
operator asked for the release with their own command line; the probe mode
writes nothing at all.

TRANSPORT: the one sanctioned one. ``_default_ssh_factory`` resolves the
sibling ``hscc-cluster/clusterlib`` (install_autoreboot's precedent) and never
hand-rolls ssh. Inside a pytest process the factory REFUSES to exist — a
suite that forgot to inject its transport gets an honest UNVERIFIABLE row,
never a live ssh. Tests inject fakes at ``ssh_factory`` and at ``hosts``; no
test ever opens a socket.

Address discipline: fixtures use the documented placeholders (192.0.2.x for a
LAN node); no real host string appears in this file or its tests.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

# The in-suite fence is ONE predicate, shared with the row registry: see
# ``doctor_rows.under_pytest`` for why the module-local test is not enough.
import doctor_rows  # flat sibling, next to this module (hscc-bootstrap/)

CHECK_NAME = "sparkrun reservations (stale execution claims)"

#: Per-host verdicts. Deliberately keep "answered, nothing stale" apart from
#: "never answered" — the second is never ok=True.
V_OK = "ok"              # answered: no dead-owner claim anywhere
V_STALE = "stale"        # answered: >=1 reservation whose owner is DEAD
V_LIVE = "live"          # answered: only reservations with LIVE owners
V_ERROR = "error"        # cannot tell: transport/script failure (never ok)

PROBE_MODE = "probe"
RECONCILE_MODE = "reconcile"

#: One bash script shipped to the node (itself base64'd through the
#: transport, the install_notifier_mask convention, so it is byte-exact on
#: arrival). It resolves the node's own sparkrun venv python from the node's
#: own ``sparkrun`` CLI shebang and runs ONE of sparkrun's documented calls.
#:
#: ``$4`` is the embedded python, ALREADY base64-encoded: the payload needs
#: no shell quoting at all, and mode/owner stay ordinary single-quoted argv.
#:
#: Exit-code discipline is what lets the caller tell "sparkrun answered a
#: typed refusal" (exit 0 + a JSON ``error`` field) from "the script itself
#: broke" (exit 3 + no JSON — an ENVIRONMENT fault, never a data fact).
_REMOTE_TEMPLATE = (
    "set -u\n"
    "cli=\"$(command -v sparkrun || true)\"\n"
    "[ -n \"$cli\" ] || cli=\"$HOME/.local/bin/sparkrun\"\n"
    "[ -x \"$cli\" ] || { echo \"sparkrun CLI not found on this node\" >&2; exit 3; }\n"
    "py=\"$(head -1 \"$cli\" | sed -e 's|^#!||' -e 's/[[:space:]]*$//')\"\n"
    "[ -x \"$py\" ] || { echo \"sparkrun venv python not executable\" >&2; exit 3; }\n"
    "payload=\"$(printf '%s' \"$4\" | base64 -d)\"\n"
    "\"$py\" -c \"$payload\" \"$1\" \"$2\"\n"
)

#: Runs under the node's sparkrun venv python. argv: mode, owner.
#: Only the two documented coordination calls are named here; the sqlite
#: schema is never touched.
_REMOTE_PY = (
    "import json,sys\n"
    "from sparkrun.core.config import SparkrunConfig\n"
    "from sparkrun.core.coordination import reservations,reconcile_reservation\n"
    "mode=sys.argv[1]\n"
    "root=str(SparkrunConfig().config_path.parent)\n"
    "if mode=='probe':\n"
    "    print(json.dumps({'mode':mode,'reservations':reservations(root)}))\n"
    "elif mode=='reconcile':\n"
    "    owner=sys.argv[2]\n"
    "    try:\n"
    "        reconcile_reservation(root,owner,acknowledge_effects=True)\n"
    "    except LookupError:\n"
    "        print(json.dumps({'mode':mode,'owner':owner,'released':False,'error':'not-found'}))\n"
    "    except (ValueError,RuntimeError) as exc:\n"
    "        print(json.dumps({'mode':mode,'owner':owner,'released':False,'error':str(exc)}))\n"
    "    else:\n"
    "        print(json.dumps({'mode':mode,'owner':owner,'released':True}))\n"
    "else:\n"
    "    raise SystemExit('unknown mode')\n"
)

#: Base64 of the embedded python, computed once at import. Traveling encoded
#: is what removes the quoting problem entirely — it is a plain
#: ``[A-Za-z0-9+/=]`` token and nothing on the command line can reinterpret
#: it as shell.
_REMOTE_PY_B64 = base64.b64encode(_REMOTE_PY.encode()).decode()


def remote_command(mode: str, owner: str = "") -> str:
    """The exact remote command for *mode*: the script, piped to bash.

    Positional args to the remote bash: ``$1`` = mode, ``$2`` = owner,
    ``$4`` = the base64'd embedded python. ``owner`` is only meaningful for
    ``reconcile``; the probe passes ``""``.

    Mode and owner are the ONLY interpolated values, and each is validated
    against a closed set/pattern FIRST — there is no general string path
    into the command at all.
    """
    if mode not in (PROBE_MODE, RECONCILE_MODE):
        raise ValueError(f"unknown remote mode {mode!r}")
    if owner and not all(ch in "0123456789abcdef" for ch in owner):
        raise ValueError(f"refusing to interpolate a non-hex owner {owner!r}")
    args = f"'{mode}' '{owner}' '' '{_REMOTE_PY_B64}'"
    script = _REMOTE_TEMPLATE + " " + args + "\n"
    b64 = base64.b64encode(script.encode()).decode()
    return f"printf '%s' '{b64}' | base64 -d | bash -s"


def _default_ssh_factory() -> Callable[[str], Callable[[str, int], Dict[str, Any]]]:
    """Production transport: sparkrun's own remote execution, bound per node.

    Resolves the sibling ``hscc-cluster`` dir (``install_autoreboot._default_ssh``
    precedent) and routes clusterlib -> sparkrun_bridge -> sparkrun's
    ``run_remote_script``. NO hand-rolled ssh anywhere.

    Refused outright inside a pytest process: a suite that forgot to inject its
    transport must fail loudly rather than reach the live fleet — and on this
    surface "reaching" could mean deleting a serving launch's reservation.
    """
    if doctor_rows.under_pytest():
        def refused_in_suite(_host: str):
            return _refused_transport("production transport refused inside a "
                                      "pytest process — inject ssh_factory")
        return refused_in_suite

    def factory(host: str) -> Callable[[str, int], Dict[str, Any]]:
        def run(command: str, timeout: int) -> Dict[str, Any]:
            try:
                # Path handling: the sibling hscc-cluster dir is appended,
                # never prepended (doctor_node_recovery._cluster_dir
                # reasoning — bootstrap owns same-named modules like
                # ``_theme``). clusterlib.ssh_cmd is the ONE sanctioned
                # fleet door.
                here = os.path.dirname(os.path.abspath(__file__))
                cluster_dir = os.path.abspath(os.path.join(
                    os.path.dirname(here), "hscc-cluster"))
                if cluster_dir not in sys.path:
                    sys.path.append(cluster_dir)
                import clusterlib  # noqa: PLC0415 - path set just above
            except ImportError as e:
                return {"ok": False, "stdout": "", "code": 127,
                        "stderr": f"clusterlib unavailable ({e}); sparkrun "
                                  f"bridge unreachable"}
            return clusterlib.ssh_cmd(host, command, timeout=timeout)
        return run
    return factory


def _refused_transport(reason: str) -> Callable[[str, int], Dict[str, Any]]:
    """A transport that only ever reports the fault — never an ok."""
    def run(_command: str, _timeout: int) -> Dict[str, Any]:
        return {"ok": False, "stdout": "", "code": None, "stderr": reason}
    return run


def _normalize(res: Any) -> Dict[str, Any]:
    """Coerce whatever a fake transport returned into the dict contract."""
    if not isinstance(res, dict):
        return {"ok": False, "stdout": "", "stderr": "", "code": None}
    return {"ok": bool(res.get("ok")), "stdout": res.get("stdout") or "",
            "stderr": res.get("stderr") or "", "code": res.get("code")}


def _last_json(stdout: str) -> Optional[dict]:
    """The JSON document the remote python printed (last JSON line wins).

    Remote motd/banner lines ride along on some shells; only a line that
    PARSES is considered, and the LAST such line is the script's answer.
    Nothing parseable -> None, which the caller must report as an error —
    never as "no reservations".
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _resolve_factory(ssh_factory) -> Callable[[str], Callable[[str, int], Dict[str, Any]]]:
    """Accept a host-bound factory (the repo contract, ``doctor_node_recovery``'s
    ``(host) -> (command, timeout) -> dict``), a host-keyed dict of runners, or
    None (= production).

    A host-keyed dict is additionally accepted so a test can give each fake
    node its own ledger without writing a factory. Nothing else: a
    zero-arg factory is NOT probed — a suite's fake must state its shape, not
    be guessed at.
    """
    if ssh_factory is None:
        return _default_ssh_factory()
    if isinstance(ssh_factory, dict):
        table = ssh_factory

        def from_table(host: str):
            return table.get(host, _refused_transport(
                f"no fake transport for host {host}"))
        return from_table
    if callable(ssh_factory):
        return ssh_factory
    raise TypeError(f"ssh_factory must be callable/dict/None, got "
                    f"{type(ssh_factory).__name__}")


def check_host(host: str, *, run: Callable[[str, int], Dict[str, Any]],
               timeout: int = 30) -> Dict[str, Any]:
    """One node's reservation report: ``{verdict, stale, live, error}``.

    ``run`` is the host-bound transport (``(command, timeout) -> dict``).
    ANY answer we cannot trust — transport failure, no JSON, a non-list
    payload — is ``error``, which renders ok=False. An empty list is the
    only answer that means "no reservations", and it requires the script to
    have actually answered.
    """
    res = _normalize(run(remote_command(PROBE_MODE), timeout))
    if not res["ok"]:
        return {"verdict": V_ERROR,
                "error": f"transport: {(res['stderr'] or 'ssh probe failed').strip()}",
                "stale": [], "live": []}
    data = _last_json(res["stdout"])
    if data is None or not isinstance(data.get("reservations"), list):
        return {"verdict": V_ERROR,
                "error": f"probe answered without a reservations list "
                         f"(stdout={(res['stdout'].strip()[:200] or 'empty')!r})",
                "stale": [], "live": []}
    stale, live, unknown = [], [], []
    for row in data["reservations"]:
        if not isinstance(row, dict):
            unknown.append({"owner": "?", "resource": "?", "process": {}})
            continue
        owner = row.get("owner", "?")
        resource = row.get("resource", "?")
        identity = row.get("process") or {}
        pid = identity.get("pid", "?") if isinstance(identity, dict) else "?"
        entry = {"owner": owner, "resource": resource, "pid": pid}
        if row.get("alive") is True:
            live.append(entry)
        elif row.get("alive") is False:
            stale.append(entry)
        else:
            unknown.append(entry)
    if unknown:
        # sparkrun's own alive field missing/odd: neither dead nor alive.
        # An unknown is NOT deletable and NOT clean — report it as an error
        # naming the rows rather than guessing either way.
        return {"verdict": V_ERROR,
                "error": "reservations with unreadable liveness: "
                         + ", ".join(f"{u['owner']}@{u['resource']}"
                                     for u in unknown),
                "stale": stale, "live": live}
    verdict = V_STALE if stale else (V_LIVE if live else V_OK)
    return {"verdict": verdict, "stale": stale, "live": live, "error": None}


def check(hosts: Optional[List[str]], *, ssh_factory=None,
          timeout: int = 30) -> Dict[str, Any]:
    """The doctor row: every cluster node's dead-owner reservations.

    Never fatal — a stale reservation blocks a launch, it does not condemn an
    install, and a FATAL row here would hard-stop bootstrap over state the
    operator releases deliberately. An empty/unresolvable host list is an
    honest error, never a vacuous pass.
    """
    hosts = list(hosts or [])
    if not hosts:
        return _verdict(False,
                        "cluster hosts unresolvable — reservations NOT verified",
                        fix="Check `sparkrun cluster list` reachability; "
                            "this row reads the default cluster's nodes.",
                        fatal=False, nodes=[])
    factory = _resolve_factory(ssh_factory)
    per_node, stale_all, live_all, errors = [], [], [], []
    for host in hosts:
        rep = check_host(host, run=factory(host), timeout=timeout)
        per_node.append({"host": host, **rep})
        for s in rep["stale"]:
            stale_all.append({**s, "host": host})
        live_all.extend({**l, "host": host} for l in rep["live"])
        if rep["verdict"] == V_ERROR:
            errors.append(f"{host}: {rep['error']}")
    result = {"nodes": per_node, "stale": stale_all, "live": live_all,
              "errors": errors}
    if errors:
        return _verdict(False,
                        "UNVERIFIABLE on " + ", ".join(errors)
                        + " (state unknown, NOT verified)",
                        fix="Restore reachability to the named node(s); this "
                            "row never deletes on an unanswered probe.",
                        fatal=False, **{k: result[k] for k in
                                        ("nodes", "stale", "live", "errors")})
    if stale_all:
        named = ", ".join(f"{s['resource']} owner {s['owner']} pid "
                          f"{s['pid']} on {s['host']}" for s in stale_all)
        return _verdict(False,
                        f"{len(stale_all)} stale reservation(s) — owner pid "
                        f"dead: {named}",
                        fix="Release them with `python3 doctor.py --fix` "
                            "(runs sparkrun's own reconcile_reservation per "
                            "dead owner; live owners are never touched).",
                        fatal=False, **{k: result[k] for k in
                                        ("nodes", "stale", "live", "errors")})
    if live_all:
        return _verdict(True,
                        f"{len(live_all)} reservation(s) live — owners "
                        f"verified on their nodes",
                        fatal=False, nodes=per_node)
    return _verdict(True, "no reservations outstanding on any node",
                    fatal=False, nodes=per_node)


def _verdict(ok: bool, detail: str, *, fix: str = "", fatal: bool = False,
             **extra) -> Dict[str, Any]:
    out = {"name": CHECK_NAME, "ok": ok, "detail": detail, "fix": fix,
           "fatal": fatal}
    out.update(extra)
    return out


def release_stale(hosts: List[str], *, ssh_factory=None,
                  timeout: int = 30) -> List[str]:
    """``--fix`` half: reconcile ONLY dead-owner reservations. Returns
    human-readable fix lines (empty when there is nothing to release).

    Order is deliberate: PROBE first (fresh verdict, this call), then release
    exactly the dead owners, one ``reconcile_reservation`` per owner, ON THE
    NODE. sparkrun re-checks liveness inside its own transaction and raises
    ``ResourceBusy`` if the owner came back alive — such an owner is reported
    as left alone, never forced. Nothing here deletes an alive reservation, a
    whole table, or anything from this machine.
    """
    lines: List[str] = []
    factory = _resolve_factory(ssh_factory)
    for host in list(hosts or []):
        rep = check_host(host, run=factory(host), timeout=timeout)
        if rep["verdict"] == V_ERROR:
            lines.append(f"{host}: NOT touched — probe failed "
                         f"({rep['error']})")
            continue
        for stale in rep["stale"]:
            run = factory(host)
            res = _normalize(run(remote_command(RECONCILE_MODE,
                                               str(stale["owner"])),
                                 timeout))
            data = _last_json(res["stdout"]) if res["ok"] else None
            if data is None:
                lines.append(f"{host}: owner {stale['owner']} "
                             f"({stale['resource']}) release UNVERIFIED — "
                             f"{(res['stderr'] or 'no answer').strip()[:120]}")
            elif data.get("released"):
                lines.append(f"{host}: released owner {stale['owner']} "
                             f"({stale['resource']}, pid {stale['pid']} dead)")
            else:
                lines.append(f"{host}: owner {stale['owner']} LEFT — "
                             f"sparkrun refused: {data.get('error')}")
    return lines
