"""Doctor coverage for stale sparkrun reservations (t_7dd19947, t_5f419e01).

The defect this row exists for: a killed sparkrun client leaves a reservation
row in sparkrun's operations ledger whose owner pid is dead, and the NEXT
launch/proxy start then fails ``Execution resources are busy or require
reconciliation after an interrupted operation`` (what blocked the .244
orchestrator launch during the template switch). Nothing in hscc could see
those reservations, so a stale one read as real contention and the only
recovery was operator archaeology.

TWO RULES DECIDE THIS FILE
--------------------------
**Ask liveness where the OWNER PROCESS LIVES — and a ledger is only readable
from the machine that owns it.** There is no single "fleet" place to ask:
there are as many ledgers as there are machines running a sparkrun client,
and each one is judged by the process table of ITS own host.

  * A NODE's ledger is asked ON the node, under the node's OWN sparkrun venv
    python (resolved from the node's ``sparkrun`` CLI shebang, the same shape
    ``hscc_daemon/health.py``'s ``_SPARKRUN_STATUS_SCRIPT`` resolves locally).
    A local pid probe of a node's ledger is false-empty — the processes are
    on the node — and a false-empty reads as "no reservations", worse than an
    error because it clears the way for a launch that then fails.
  * THIS machine's ledger is asked HERE, under hscc's own sparkrun venv
    python (``hscc_daemon.sparkrun_bin``, the interpreter hscc already
    resolves for local sparkrun work). That ledger is not an oddity — it is
    where a ``hscc apply`` that spawns its sparkrun client LOCALLY leaves its
    own claims, and it is exactly where the row that blocked the orchestrator
    launch lived: ``process {"pid": 71357, "platform": "darwin"}``, a darwin
    identity, in ``~/.config/sparkrun/operations.sqlite3`` on the gateway Mac.
    Probing it from a node would be worse than false-empty: it would be
    FALSE-DEAD. ``process_identity`` compares an identity dict field by
    field, so a ``platform: "darwin"`` row compared against a node's
    ``/proc`` identity can never be equal — a LIVE local owner would read as
    dead, and the only action that follows from "dead" is a delete.

t_5f419e01 is the correction of the first version's premise, which said "the
ledger and the owner process both live on the node" and shipped only the
remote half. That premise is what let a node-only probe look like full
coverage while reporting the incident's own host clean.

**The ledger is sparkrun's.** hscc never opens ``operations.sqlite3``, on
either leg. It calls the two documented sparkrun functions — the SAME two,
the same embedded payload, just run there vs run here; the remote half
through sparkrun's own remote-execution bridge (``clusterlib.ssh_cmd`` ->
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

TRANSPORT: two doors, each the sanctioned one for its leg. The REMOTE leg
resolves the sibling ``hscc-cluster/clusterlib`` (install_autoreboot's
precedent) and never hand-rolls ssh. The LOCAL leg is not a second transport
at all — it is ``subprocess`` exec'ing the resolved sparkrun venv interpreter
with the SAME payload, the ``health.py`` ``_SPARKRUN_STATUS_SCRIPT`` shape
("run a script under sparkrun's own interpreter"), differing only in that the
interpreter and the ledger are already here.

Inside a pytest process BOTH doors REFUSE to exist — a suite that forgot to
inject its transport gets an honest UNVERIFIABLE row, never a live ssh and
never a subprocess reading the OPERATOR'S OWN ledger. Tests inject fakes at
``ssh_factory``/``hosts`` (remote) and ``local_runner`` (local); no test ever
opens a socket, spawns a process, or names a real ledger path.

Address discipline: fixtures use the documented placeholders (192.0.2.x for a
LAN node); no real host string appears in this file or its tests.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess  # the LOCAL leg only: exec sparkrun's own interpreter, here
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

#: Runs under sparkrun's own venv python — the node's there, hscc's own here.
#: argv: mode, owner. Only the two documented coordination calls are named
#: here; the sqlite schema is never touched, and never needs to be, because
#: sparkrun answers ``alive`` for us wherever the payload runs.
_PROBE_PY = (
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
#: it as shell. (The REMOTE leg needs this; the local leg passes ``_PROBE_PY``
#: as an ordinary argv element and needs no encoding at all.)
_PROBE_PY_B64 = base64.b64encode(_PROBE_PY.encode()).decode()

#: Label for the local leg in every report and every detail line. It is a
#: TARGET NAME, not a host: the row's ``resource`` may well name a cluster
#: node while the LEDGER holding the claim — and the owner process — are
#: right here. "local" is what an operator reads in the doctor output.
LOCAL_TARGET = "local"


def _checked_mode_owner(mode: str, owner: str) -> None:
    """The ONE validation gate every command builder passes through.

    Mode is a closed set; an owner is a bare hex token. Both legs interpolate
    these into an executed command (the remote one into a shell string), so
    there is no general string path into either command — a bad value raises
    here, before anything is built, let alone run.
    """
    if mode not in (PROBE_MODE, RECONCILE_MODE):
        raise ValueError(f"unknown mode {mode!r}")
    if owner and not all(ch in "0123456789abcdef" for ch in owner):
        raise ValueError(f"refusing to interpolate a non-hex owner {owner!r}")


def remote_command(mode: str, owner: str = "") -> str:
    """The exact remote command for *mode*: the script, piped to bash.

    Positional args to the remote bash: ``$1`` = mode, ``$2`` = owner,
    ``$4`` = the base64'd embedded python. ``owner`` is only meaningful for
    ``reconcile``; the probe passes ``""``.

    Mode and owner are the ONLY interpolated values, and each is validated
    against a closed set/pattern FIRST — there is no general string path
    into the command at all.
    """
    _checked_mode_owner(mode, owner)
    args = f"'{mode}' '{owner}' '' '{_PROBE_PY_B64}'"
    script = _REMOTE_TEMPLATE + " " + args + "\n"
    b64 = base64.b64encode(script.encode()).decode()
    return f"printf '%s' '{b64}' | base64 -d | bash -s"


def local_argv(mode: str, owner: str = "") -> List[str]:
    """The exact local payload argv for *mode*: the SAME embedded python, run
    HERE.

    No shell and no base64 — a ``subprocess`` exec takes an argv list, so the
    payload needs no quoting at all. Same validation as the remote builder:
    one gate, both doors.

    The interpreter is deliberately ABSENT. "Which python can
    ``import sparkrun``" is a machine fact, and it is resolved by exactly one
    resolver in this repo (``resolve_local_python``) at EXEC time — see
    ``_default_local_runner``. Keeping it out of the argv is also what lets a
    test inject a fake runner without ever touching the filesystem.
    """
    _checked_mode_owner(mode, owner)
    return ["-c", _PROBE_PY, mode, owner]


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


def _refused_transport(reason: str) -> Callable[[Any, int], Dict[str, Any]]:
    """A transport that only ever reports the fault — never an ok.

    Command-agnostic on purpose: the remote door hands it a shell string and
    the local door an argv list, and a refusal answers either the same way.
    """
    def run(_command: Any, _timeout: int) -> Dict[str, Any]:
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


# ── the LOCAL leg: the same two calls, run here (t_5f419e01) ───────────────

def _load_sparkrun_bin():
    """Import ``hscc_daemon/sparkrun_bin.py`` — the ONE interpreter resolver
    hscc already uses for local sparkrun work.

    Deferred + guarded, the ``doctor._load_hermes_python`` / ``_theme.theme``
    precedent: a bootstrap-only install without hscc_daemon gets None, and the
    caller reports the local ledger UNVERIFIABLE rather than hand-rolling a
    second resolver. A second resolver is a second source of drift — and this
    one would be a PATH guess, the exact ``shutil.which`` mistake
    ``sparkrun_bin`` exists to remove (t_b543e530: a supervised PATH has no
    ``~/.local/bin``, so a PATH guess is None, and None here would read as
    "no local reservations").
    """
    try:
        from hscc_daemon import sparkrun_bin
        return sparkrun_bin
    except ImportError:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import sparkrun_bin
        return sparkrun_bin
    except ImportError:
        return None


def resolve_local_python() -> Optional[str]:
    """hscc's own sparkrun venv interpreter, or None when genuinely absent.

    Never a PATH-default ``python3``: only sparkrun's own venv can
    ``import sparkrun``, and an unresolvable interpreter is reported as an
    error by the caller — never as an empty ledger.
    """
    sb = _load_sparkrun_bin()
    if sb is None:
        return None
    try:
        return sb.sparkrun_venv_python()
    except OSError:
        return None


def _default_local_runner(resolve_local: Optional[Callable[[], Optional[str]]] = None
                          ) -> Callable[[List[str], int], Dict[str, Any]]:
    """Production local runner: exec the payload HERE, under hscc's OWN
    sparkrun venv python — the ``health.py`` ``_SPARKRUN_STATUS_SCRIPT``
    shape ("run the script under sparkrun's own interpreter"), with the
    interpreter and the ledger already on this machine.

    Resolution happens per CALL, never at import: ``sparkrun_bin`` is
    filesystem-based and caches the resolved CLI on purpose (a CLI move is a
    deploy event), so re-asking is cheap and stays correct across a
    mid-process install.

    Refused outright inside a pytest process, exactly like
    ``_default_ssh_factory`` — but this door is the more dangerous one to
    leave open, because "here" is the operator's own machine: an unfenced
    suite would run ``reservations()`` against the REAL
    ``~/.config/sparkrun/operations.sqlite3``, and on the ``--fix`` half it
    would be one step from ``reconcile_reservation`` against it. A suite that
    forgets to inject ``local_runner`` gets an error for every command.

    ``resolve_local`` is the resolver seam (a test pins the unresolvable-venv
    path without installing a fake sparkrun).
    """
    if doctor_rows.under_pytest():
        return _refused_transport("production local runner refused inside a "
                                  "pytest process — inject local_runner")

    resolve = resolve_local or resolve_local_python

    def run(payload_argv: List[str], timeout: int) -> Dict[str, Any]:
        py = resolve()
        if not py:
            # An unresolvable venv is an ENVIRONMENT fault, reported as one —
            # never a subprocess against a PATH-default python that cannot
            # import sparkrun, and never the false-empty that would clear the
            # way for a launch that then fails.
            return {"ok": False, "stdout": "", "code": None,
                    "stderr": "hscc's sparkrun venv python is unresolvable — "
                              "the local ledger was NOT read"}
        try:
            proc = subprocess.run([py, *payload_argv], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "stdout": "", "code": None,
                    "stderr": f"local sparkrun probe timed out after {timeout}s"}
        except OSError as e:
            return {"ok": False, "stdout": "", "code": None,
                    "stderr": f"local sparkrun probe could not exec: {e}"}
        return {"ok": proc.returncode == 0, "stdout": proc.stdout or "",
                "stderr": proc.stderr or "", "code": proc.returncode}
    return run


def check_local(*, run=None, timeout: int = 30) -> Dict[str, Any]:
    """THIS machine's reservation report: ``{verdict, stale, live, error}``.

    The same two documented sparkrun calls as the node leg, run under hscc's
    own sparkrun venv python — so ``alive`` is sparkrun's own
    ``process_identity`` verdict made HERE, against the process table that
    actually holds these owner pids. That is the whole point of this leg: a
    ``platform: "darwin"`` identity is only comparable on a darwin host.

    ``run`` is the local runner seam (``(payload_argv, timeout) -> dict``;
    production = ``subprocess``, which refuses to exist in-suite). A fake
    answers rows whose ``alive`` is judged against THIS host's pids — the
    same contract the node fake has, read on the other side of the wire.
    """
    runner = run if run is not None else _default_local_runner()
    return _report_from(runner(local_argv(PROBE_MODE), timeout),
                        what="local probe")


def _report_from(answer: Any, *, what: str) -> Dict[str, Any]:
    """The ONE classification of an answered probe — shared by BOTH legs.

    Shared deliberately: two copies of this is how one leg ends up reporting
    the other's rows with the other's rules. ANY answer we cannot trust —
    transport failure, no JSON, a non-list payload, an unreadable liveness
    field — is ``error``, which renders ok=False. An empty list is the only
    answer that means "no reservations", and it requires the probe to have
    actually answered.
    """
    res = _normalize(answer)
    if not res["ok"]:
        return {"verdict": V_ERROR,
                "error": f"{what}: {(res['stderr'] or 'probe failed').strip()}",
                "stale": [], "live": []}
    data = _last_json(res["stdout"])
    if data is None or not isinstance(data.get("reservations"), list):
        return {"verdict": V_ERROR,
                "error": f"{what} answered without a reservations list "
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
        # The identity's platform rides along ONLY when sparkrun answered one,
        # so a row from a local (darwin) ledger reads differently from a node
        # row in the handoff — which is exactly the distinction this card is.
        if isinstance(identity, dict) and identity.get("platform"):
            entry["platform"] = identity["platform"]
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


def check_host(host: str, *, run: Callable[[str, int], Dict[str, Any]],
               timeout: int = 30) -> Dict[str, Any]:
    """One node's reservation report: ``{verdict, stale, live, error}``.

    ``run`` is the host-bound transport (``(command, timeout) -> dict``).
    Classification is the shared ``_report_from`` — the node leg and the local
    leg differ ONLY in which machine answers, never in what an answer means.
    """
    return _report_from(run(remote_command(PROBE_MODE), timeout),
                        what="node probe")


def check(hosts: Optional[List[str]], *, ssh_factory=None,
          local_runner=None, timeout: int = 30) -> Dict[str, Any]:
    """The doctor row: the LOCAL ledger plus every cluster node's ledger.

    Two legs, always both. The local leg is NOT a convenience — a locally
    spawned sparkrun client holds its claims in THIS machine's ledger, and a
    node-only probe reports the incident's own host clean (t_5f419e01).

    Never fatal — a stale reservation blocks a launch, it does not condemn an
    install, and a FATAL row here would hard-stop bootstrap over state the
    operator releases deliberately. An empty/unresolvable host list is an
    honest error for the NODE leg and never a vacuous pass; it does NOT
    suppress the local answer, which stands on its own.
    """
    hosts = list(hosts or [])
    targets = [{"target": LOCAL_TARGET,
                **check_local(run=local_runner, timeout=timeout)}]
    if hosts:
        factory = _resolve_factory(ssh_factory)
        for host in hosts:
            targets.append({"target": host,
                            **check_host(host, run=factory(host),
                                         timeout=timeout)})
    else:
        # The node leg went unanswered — but "unanswered" is a VERDICT about
        # the nodes, not a licence to skip them, and never a "clean".
        targets.append({"target": "nodes", "verdict": V_ERROR, "stale": [],
                        "live": [],
                        "error": "cluster hosts unresolvable — node ledgers "
                                 "NOT verified"})

    stale_all, live_all, errors = [], [], []
    for t in targets:
        for s in t["stale"]:
            stale_all.append({**s, "target": t["target"]})
        live_all.extend({**l, "target": t["target"]} for l in t["live"])
        if t["verdict"] == V_ERROR:
            errors.append(f"{t['target']}: {t['error']}")

    extra = {"targets": targets, "stale": stale_all, "live": live_all,
             "errors": errors}
    if stale_all:
        # A CONFIRMED stale row outranks an unanswered neighbour: naming a
        # dead owner is the whole point of the row, and hiding it behind a
        # node we could not reach would re-create the miss this card is about.
        named = ", ".join(_named(s) for s in stale_all)
        detail = (f"{len(stale_all)} stale reservation(s) — owner pid dead: "
                  f"{named}")
        if errors:
            detail += (" — AND " + str(len(errors)) + " target(s) UNVERIFIABLE: "
                       + ", ".join(errors) + " (state there NOT verified)")
        return _verdict(False, detail,
                        fix="Release them with `python3 doctor.py --fix` "
                            "(runs sparkrun's own reconcile_reservation per "
                            "dead owner, on the machine that owns that "
                            "ledger; live owners are never touched).",
                        fatal=False, **extra)
    if errors:
        return _verdict(False,
                        "UNVERIFIABLE on " + ", ".join(errors)
                        + " (state unknown, NOT verified)",
                        fix="Restore reachability to the named target(s); "
                            "this row never deletes on an unanswered probe.",
                        fatal=False, **extra)
    if live_all:
        return _verdict(True,
                        f"{len(live_all)} reservation(s) live — owners "
                        f"verified where they live",
                        fatal=False, **extra)
    return _verdict(True, "no reservations outstanding — local ledger and "
                          "every node answered clean", fatal=False, **extra)


def _named(row: Dict[str, Any]) -> str:
    """One stale row, stated so an operator can act: what it claims, who owns
    it, the pid that is gone, and WHICH MACHINE holds that ledger."""
    return (f"{row['resource']} owner {row['owner']} pid {row['pid']} "
            f"on {row['target']}")


def _verdict(ok: bool, detail: str, *, fix: str = "", fatal: bool = False,
             **extra) -> Dict[str, Any]:
    out = {"name": CHECK_NAME, "ok": ok, "detail": detail, "fix": fix,
           "fatal": fatal}
    out.update(extra)
    return out


def release_stale(hosts: List[str], *, ssh_factory=None,
                  local_runner=None, timeout: int = 30) -> List[str]:
    """``--fix`` half: reconcile ONLY dead-owner reservations. Returns
    human-readable fix lines (empty when there is nothing to release).

    Order is deliberate: PROBE first (fresh verdict, this call), then release
    exactly the dead owners, one ``reconcile_reservation`` per owner, ON THE
    MACHINE THAT OWNS THAT LEDGER. sparkrun re-checks liveness inside its own
    transaction and raises ``ResourceBusy`` if the owner came back alive —
    such an owner is reported as left alone, never forced. Nothing here
    deletes an alive reservation, a whole table, or anything on a machine
    whose probe did not answer.

    Local and remote obey the SAME fail-closed rule: an unanswered probe is
    never a licence to delete, on either half. The local half is the one this
    card adds, and it is the half that must be released HERE — a
    ``platform: "darwin"`` owner cannot be liveness-checked on a node.
    """
    lines: List[str] = []
    lrun = local_runner if local_runner is not None else _default_local_runner()
    rep = _report_from(lrun(local_argv(PROBE_MODE), timeout),
                       what="local probe")
    lines.extend(_release_target(LOCAL_TARGET, rep, lrun,
                                 lambda owner: local_argv(RECONCILE_MODE,
                                                          owner),
                                 timeout))

    factory = _resolve_factory(ssh_factory)
    for host in list(hosts or []):
        rep = check_host(host, run=factory(host), timeout=timeout)
        lines.extend(_release_target(host, rep, factory(host),
                                     lambda owner: remote_command(
                                         RECONCILE_MODE, owner),
                                     timeout))
    return lines


def _release_target(target: str, rep: Dict[str, Any],
                    run: Callable[[Any, int], Dict[str, Any]],
                    command_for: Callable[[str], Any],
                    timeout: int) -> List[str]:
    """Release ONE target's dead owners, fail-closed, one call per owner.

    Shared by both legs so the local ledger can never inherit a weaker rule
    than a node's: an errored probe returns a NOT touched line and issues no
    reconcile, an unreconcilable answer is reported UNVERIFIED rather than
    assumed released, and a sparkrun refusal is reported as LEFT.
    """
    if rep["verdict"] == V_ERROR:
        return [f"{target}: NOT touched — probe failed ({rep['error']})"]
    lines: List[str] = []
    for stale in rep["stale"]:
        res = _normalize(run(command_for(str(stale["owner"])), timeout))
        data = _last_json(res["stdout"]) if res["ok"] else None
        if data is None:
            lines.append(f"{target}: owner {stale['owner']} "
                         f"({stale['resource']}) release UNVERIFIED — "
                         f"{(res['stderr'] or 'no answer').strip()[:120]}")
        elif data.get("released"):
            lines.append(f"{target}: released owner {stale['owner']} "
                         f"({stale['resource']}, pid {stale['pid']} dead)")
        else:
            lines.append(f"{target}: owner {stale['owner']} LEFT — "
                         f"sparkrun refused: {data.get('error')}")
    return lines
