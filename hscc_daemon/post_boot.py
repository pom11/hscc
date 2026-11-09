"""Post-reboot relaunch for cluster-up-owned units when the HOST vanished.

WHY THIS EXISTS (t_576964d5, operator ask)
------------------------------------------
After a watchdog-triggered or panic reboot the node must come back SERVING on
its own. It did not: the watchdog's auto-restart refuses every registered
unit — ``lifecycle.restart_vllm`` rule 2 (lifecycle.py:342) — because the
correct relaunch surface for a serving.json unit is ``hscc cluster up``, and
the watchdog must never become a second start surface (the 2026-10-18 race).
So after the .244 power-cycle the operator had to run ``hscc cluster up`` by
hand. This module closes that gap WITHOUT weakening any intentional-down gate.

THE DISTINCTION (the whole card)
--------------------------------
  * "the node itself went away and came back" (unexpected reboot) → relaunch
    the units that were serving pre-crash;
  * "the unit was intentionally stopped" (cluster down / autodown teardown /
    node power-off) → stay down, forever, unless a human or a wake brings it
    back.

The evidence for the first, per node, is a BOOT-ID CHANGE of a node that is
reachable and was seen running before. The marker file ``~/.hscc/expected-up.
json`` records the fleet-up footprint (set by ``hscc cluster up`` / an
autodown wake, CLEARED by ``hscc cluster down`` / an autodown teardown), so
"was it supposed to be up?" and "did the host reboot since?" are two facts a
human can read off disk — and a unit that was intentionally stopped has no
marker at all.

THE STAY-DOWN RECORD (t_ab75e987)
---------------------------------
An empty ``units`` list alone cannot tell "the operator said stay down" from
"nobody ever ran cluster up", so the SAME marker file also carries the
operator's intent: a top-level ``deliberate_down`` key —
``{"set_at": <now_iso()>, "reason": <str>}`` — written by
``clear_expected_up(reason)`` and removed (``pop``) by ``record_expected_up``,
which is what makes ``cluster up`` the end of a stay-down. Read it with the
module-level predicate ``deliberately_down(path=None) -> dict | None``, the
one accessor for this fact (callers read ``reason``/``set_at`` off the record;
it is durable on disk only — nothing here is cached in memory). Fail-safe in
the SAME convention as ``health._intentional_autodown_down`` and
``trigger._intentional_autodown``: a missing file, an unreadable file, corrupt
JSON, a non-dict marker, or a non-dict/empty record all return ``None`` and
the predicate NEVER raises — with no verifiable evidence every consumer
supervises exactly as it did before this key existed.

THREE states, not two, leave gate 2 with empty ``units``: the record present
(stay-down), the marker file present with an empty footprint and NO record
("nothing is expected up and nobody holds a standing stay-down order" — what an
empty-plan autodown wake leaves behind, t_9c437f03 step 1b), and no readable
marker at all. All three are the same verdict (`no-marker`, ok, no launch);
``run_post_boot_pass`` prints which one it found, and prints the marker's own
``reason`` in the middle state because it is the only breadcrumb that a wake
tried. Printing is not branching: nothing reads that prose.

ESCAPE HATCH (the only one): a stay-down is lifted by ``hscc cluster up``, or
by an autodown wake, which records the footprint pre-launch — nothing else is
sanctioned. Deleting ``~/.hscc/expected-up.json`` is NOT the override: that
file is also the post-boot pass's own record of boot baselines and launch
attempts, so hand-deleting it destroys evidence the relaunch logic needs and
still does not lift the intent. Because ``deliberately_down()`` returns
``None`` for a lost file, a hand-deleted marker fails toward "supervision
proceeds", never toward silence.

GATE ORDER (every pre-existing gate stays UPSTREAM of the single launch)
------------------------------------------------------------------------
  1. env kill-switch ``HSCC_POST_BOOT_RELAUNCH=0`` — operator off-switch.
  2. marker present + non-empty — cluster down/teardown cleared it.
  3. intentional-autodown window — ``autodown.classify``/``intentional_window``,
     the SAME single decision table the watchdog fork (lifecycle.py) and
     ``check_workers`` (health._intentional_autodown_down) consult. An
     intentional teardown is NEVER resurrected, latched block or not.
  4. per-node boot baseline: relaunch a unit only when a node in its span
     reports a boot id DIFFERENT from the recorded baseline (a unit that was
     down before the reboot has an unchanged baseline → stays down). First
     sight of a node records the baseline without acting, so a fresh marker
     can never self-trigger.
  5. the unit's head endpoint must answer NOT serving (http_check, same probe
     autodown's readiness wait uses).
  6. autodown O_EXCL lock (never race a teardown/wake) + heal lease (never
     race engine-wedge recovery / auto-heal / worker relaunch — HEAL_CASCADE).
  7. per-(node, boot) attempt cap — a launch that fails retries only within
     the cap, then gives up for THIS boot (next boot resets).
  8. launch cooldown between attempts.

EXACTLY-ONCE: the observed boot id is written into the baseline BEFORE the
launch is issued, and the autodown lock serialises the whole decide+record
window, so a same-boot repeat pass sees an unchanged baseline and never fires
again. A FAILED launch restores the pre-boot baseline for the units it covers
(so the retry can re-decide) and consumes one attempt of the cap.

THE START SURFACE (rule: registered units launch ONLY via cluster-up)
---------------------------------------------------------------------
The launch calls ``hscc-cluster/hscc.py::cmd_cluster_up(nodes=<rebooted
nodes>)`` — the exact fleet-up path (fleet_up_plan, the A2 launch guard, the
already-serving skip, the launch-phase timeout, ``--no-follow --ensure``
intent dedup). This module never builds a sparkrun argv itself.

THE FLEET TRANSPORT (sparkrun owns the fleet)
---------------------------------------------
Boot ids are read THROUGH sparkrun: ``resolve_hosts`` + ``run_remote_command``
under sparkrun's own venv python, the same sanctioned pattern as
``health._SPARKRUN_STATUS_SCRIPT``. No hand-rolled ssh, no docker.

External side-effects are injectable (boot-id fetcher, probe, up_fn, locks,
notifier, marker path) so the suite runs with fakes, zero subprocess/HTTP.
"""

from __future__ import annotations

import json
import os

from .daemon_ops import log
from .state import now_iso, write_state

# Marker file. Same directory as every other durable daemon artefact
# (watchdog-block.json, autodown.json, heal-lease.json). ONE file so tests and
# operators have exactly one surface to inspect for "was the fleet expected
# to be up?" — conftest redirects this constant for the whole suite.
EXPECTED_UP_FILE = os.path.expanduser("~/.hscc/expected-up.json")

# The stay-down record inside that marker (D1): {"set_at": iso, "reason": str}.
# Written by clear_expected_up, removed by record_expected_up, read via
# deliberately_down(). See THE STAY-DOWN RECORD in the module docstring.
DELIBERATE_DOWN_KEY = "deliberate_down"


# Stream name (hscc check post_boot / verify daemon_streams).
POST_BOOT_STREAM = "post_boot"

# Operator off-switch: HSCC_POST_BOOT_RELAUNCH=0 disables the pass entirely
# (stream reads "disabled (operator off-switch)", zero I/O).
POST_BOOT_ENABLED = os.environ.get("HSCC_POST_BOOT_RELAUNCH", "1") != "0"

# Cooldown between post-boot launch attempts (seconds). A fleet-up takes
# minutes; retrying sooner than this just stacks launches.
POST_BOOT_COOLDOWN_S = float(os.environ.get("HSCC_POST_BOOT_COOLDOWN_S", "600"))

# Max relaunch attempts per (node, boot id). After the cap the node's new
# boot is ACCEPTED (baseline advances, no further action THIS boot) and the
# stream reports gave-up — a broken node must not be hammered forever. The
# next boot id resets the budget.
POST_BOOT_MAX_ATTEMPTS = int(os.environ.get("HSCC_POST_BOOT_MAX_ATTEMPTS", "3"))

# Boot-id fetch runs under sparkrun's own venv python (see
# health._sparkrun_venv_python); bound the whole sweep like the status sweep.
BOOT_FETCH_TIMEOUT_S = float(os.environ.get("HSCC_POST_BOOT_FETCH_TIMEOUT_S", "45"))

# One ssh per host; keep each bounded so one cold host cannot stall the pass.
BOOT_SSH_TIMEOUT_S = 10
BOOT_SSH_CONNECT_TIMEOUT_S = 5

# The read-only, per-node command: sparkrun's own boot identity convention
# (sparkrun/proxy/_supervisor.py::_boot_id — /proc/sys/kernel/random/boot_id,
# regenerated every boot). Nothing on the remote side is written or mutated.
_BOOT_ID_CMD = "cat /proc/sys/kernel/random/boot_id 2>/dev/null"

# Executed under sparkrun's venv python (same invocation shape as
# health._SPARKRUN_STATUS_SCRIPT): resolve the fleet hosts through sparkrun,
# then read the boot id per host through sparkrun's ssh primitive. Never a
# hand-rolled transport (sparkrun owns the fleet).
_BOOT_ID_SCRIPT = (
    "import json,sys\n"
    "from sparkrun.core.config import SparkrunConfig,get_config_root\n"
    "from sparkrun.core.cluster_manager import ClusterManager\n"
    "from sparkrun.core.hosts import resolve_hosts\n"
    "from sparkrun.orchestration.primitives import build_ssh_kwargs\n"
    "from sparkrun.orchestration.ssh import run_remote_command\n"
    "config=SparkrunConfig()\n"
    "mgr=ClusterManager(get_config_root())\n"
    "hosts=resolve_hosts(None,None,None,mgr,config.default_hosts)\n"
    "wanted=set(json.loads(sys.argv[1]))\n"
    "ssh_kwargs=build_ssh_kwargs(config)\n"
    "out={}\n"
    "for h in hosts:\n"
    "    if wanted and h not in wanted:\n"
    "        continue\n"
    "    try:\n"
    "        r=run_remote_command(h,%r,connect_timeout=%d,timeout=%d,**ssh_kwargs)\n"
    "        out[h]=r.stdout.strip() if r.returncode==0 else None\n"
    "    except Exception:\n"
    "        out[h]=None\n"
    "print(json.dumps(out))\n"
) % (_BOOT_ID_CMD, BOOT_SSH_CONNECT_TIMEOUT_S, BOOT_SSH_TIMEOUT_S)


# ── Marker file (the expected_up record) ─────────────────────────────────
def load_marker(path=None):
    """Read the marker; a missing/corrupt file reads as an EMPTY marker.

    Fail-safe direction for the whole module: an unreadable marker means "no
    evidence the fleet should be up" → the pass takes NO action. A lost marker
    can only ever cause a missed relaunch (operator-visible, `hscc cluster
    up` fixes it), never an unwanted one.
    """
    path = EXPECTED_UP_FILE if path is None else path
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("units", [])
            data.setdefault("boot_baseline", {})
            data.setdefault("boot_attempts", {})
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"units": [], "boot_baseline": {}, "boot_attempts": {}}


def _save_marker(data, path=None):
    """Atomic tmp+replace write. Best-effort; returns True on success."""
    path = EXPECTED_UP_FILE if path is None else path
    data["updated_at"] = now_iso()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"post_boot: could not write marker {path}: {e}", "ERROR")
        return False


def record_expected_up(plan, reason, path=None):
    """Record the fleet-up footprint into the marker (set by cluster up / wake).

    ``plan`` is a ``serving.fleet_up_plan()`` list (unit_id, nodes, port,
    kind, keepalive, cmd) — the EXACT set the launch surface will (re)issue,
    so what post-boot restores equals what fleet-up brings up. Existing boot
    baselines/attempts are PRESERVED (a wake while the fleet keeps serving
    must not re-arm a relaunch for a node we already adjudicated).
    """
    marker = load_marker(path)
    units = []
    for e in (plan or []):
        if not isinstance(e, dict):
            continue
        nodes = [str(n) for n in (e.get("nodes") or []) if n]
        if not nodes:
            continue
        units.append({
            "unit_id": str(e.get("unit_id") or "?"),
            "role": str(e.get("kind") or e.get("role") or "worker"),
            "nodes": nodes,
            "port": e.get("port"),
        })
    marker["units"] = units
    marker["reason"] = reason
    marker["set_at"] = now_iso()
    # A fleet-up (or an autodown wake) ENDS the stay-down: the deliberate-down
    # record is dropped so supervision resumes against a positively-asserted
    # "expected up" instead of a stale "operator said stay down".
    marker.pop(DELIBERATE_DOWN_KEY, None)
    ok = _save_marker(marker, path)
    log(f"post_boot: recorded expected_up for {len(units)} unit(s) "
        f"({reason}) — marker {'written' if ok else 'WRITE FAILED'}")
    return ok


def clear_expected_up(reason, path=None):
    """Clear the expected_up units (set by cluster down / autodown teardown).

    Baselines/attempts are kept (cheap history, no action authority without
    units); ``cleared_at``/``reason`` make the deliberate stop visible on
    disk. After this the pass is inert until the next fleet-up re-arms it.

    Also stamps the ``deliberate_down`` record (D1) so this state is
    distinguishable from "nobody ever ran cluster up": an empty ``units`` list
    is an absence of evidence, the record is positive evidence of the
    operator's instruction. Read it back with ``deliberately_down()``.
    """
    marker = load_marker(path)
    n = len(marker.get("units") or [])
    marker["units"] = []
    marker["cleared_at"] = now_iso()
    marker["reason"] = reason
    marker[DELIBERATE_DOWN_KEY] = {"set_at": now_iso(), "reason": reason}
    ok = _save_marker(marker, path)
    log(f"post_boot: cleared expected_up for {n} unit(s) ({reason}) — "
        f"post-reboot relaunch will NOT fire until the next cluster up")
    return ok


def deliberately_down(path=None):
    """The stay-down record {set_at, reason}, or None when there is none.

    ONE accessor for "the operator deliberately brought the fleet down"
    (D1) — the fact an empty ``units`` list cannot express, since it is equally
    consistent with a fleet that was never fleet-upped. Callers read
    ``reason``/``set_at`` off the returned dict; the record is durable on disk
    only, never mirrored in memory, so a daemon restart cannot lose or invent
    it.

    Fail-safe, and it NEVER raises (same convention as
    ``health._intentional_autodown_down`` and ``trigger._intentional_autodown``
    — no verifiable evidence means supervision proceeds exactly as it did
    before this key existed, never that it goes quiet): a missing file, an
    unreadable file, corrupt JSON, a non-dict marker, or a non-dict/empty
    ``deliberate_down`` value all read as None.

    Read-only and silent by design — no side effects, no logging, no marker
    write — so it is safe to call from a stream check or a per-tick gate.
    """
    path = EXPECTED_UP_FILE if path is None else path
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        # FileNotFoundError / corrupt JSON / an unreadable or unopenable path:
        # no evidence, not an error.
        return None
    if not isinstance(data, dict):
        return None
    record = data.get(DELIBERATE_DOWN_KEY)
    if not isinstance(record, dict) or not record:
        return None
    return record


def marker_file_readable(path=None):
    """True when the marker FILE exists and parses as a JSON object.

    DISPLAY-ONLY (t_c2b5fb13) — it exists so gate 2 can name which of its
    no-units states the fleet is in, and nothing may ever branch on it to
    decide what to do. The read-only distinction it draws is between facts an
    operator can check on disk: ``False`` means there is no marker evidence at
    all (absent / unreadable / corrupt / not an object — the same absence
    ``load_marker`` reads as an empty marker and ``deliberately_down`` reads as
    ``None``), ``True`` means the file is there with a footprint nobody has
    filled in. Fail-safe in the same direction as everything else here: an
    exception reading the file answers ``False``, which is the wording that
    promises nothing.

    Silent by design (no log, no write), same as ``deliberately_down``.
    """
    path = EXPECTED_UP_FILE if path is None else path
    try:
        with open(path) as f:
            return isinstance(json.load(f), dict)
    except Exception:
        # FileNotFoundError / corrupt JSON / unreadable path: no marker to
        # speak of, which is exactly what the caller's other wording says.
        return False



# ── Boot-id evidence (through sparkrun) ──────────────────────────────────
def fetch_boot_ids(nodes, run_fn=None, venv_python_fn=None):
    """{node: boot_id|None} for the requested nodes, or None when unavailable.

    The sanctioned fleet transport: sparkrun's own venv python runs
    ``_BOOT_ID_SCRIPT`` (resolve_hosts + run_remote_command). ``None`` (as
    opposed to an all-None map) means the sweep itself failed — the caller
    must treat that as NO EVIDENCE and act on nothing.
    """
    from .util import run_cmd
    from . import health as _health
    nodes = [str(n) for n in (nodes or []) if n]
    if not nodes:
        return {}
    if venv_python_fn is None:
        venv_python_fn = _health._sparkrun_venv_python
    if run_fn is None:
        run_fn = run_cmd
    venv_py = venv_python_fn()
    if not venv_py:
        log("post_boot: sparkrun venv python unresolvable — no boot evidence",
            "WARN")
        return None
    import json as _json
    try:
        res = run_fn([venv_py, "-c", _BOOT_ID_SCRIPT, _json.dumps(nodes)],
                     timeout=int(BOOT_FETCH_TIMEOUT_S))
    except Exception as e:
        log(f"post_boot: boot-id sweep raised: {e}", "WARN")
        return None
    if not (res or {}).get("ok") or not (res or {}).get("output"):
        log("post_boot: boot-id sweep failed — no evidence this pass", "WARN")
        return None
    try:
        data = json.loads(res["output"])
    except (TypeError, ValueError):
        log("post_boot: boot-id sweep output unparsable — no evidence", "WARN")
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): (v if isinstance(v, str) and v.strip() else None)
            for k, v in data.items()}


def _head_probe(node, port, probe_fn):
    """True when the unit's head endpoint serves (GET /health, util-style).

    Same probe shape autodown's readiness wait and teardown verify-down use.
    """
    from .util import http_check
    fn = probe_fn or http_check
    try:
        res = fn(f"http://{node}:{int(port)}/health", timeout=5) or {}
        return bool(res.get("ok"))
    except Exception:
        return False


# ── The guarded pass ─────────────────────────────────────────────────────
def _intentional_window_active():
    """True in an intentional-autodown window (down OR waking).

    Reuses health._intentional_window → autodown.classify, the SAME decision
    table the watchdog fork and check_workers consult. Never re-derive.
    """
    try:
        from . import health as _health
        return bool(_health._intentional_window())
    except Exception:
        # Fail-safe: an unverifiable intentional signal must not enable a
        # launch that a deliberate teardown would have suppressed.
        return True


def _candidate_units(marker, changed_nodes, probe_fn=None):
    """Marker units whose span touches a rebooted node, head not serving.

    Returns [(unit, changed_nodes_in_span), ...]. A unit down BEFORE the
    reboot is not a candidate: its nodes' baselines did not change (the
    record covers the whole fleet-up footprint regardless of current health).
    """
    out = []
    for u in (marker.get("units") or []):
        if not isinstance(u, dict):
            continue
        span = [str(n) for n in (u.get("nodes") or [])]
        hit = [n for n in span if n in changed_nodes]
        if not hit:
            continue
        port = u.get("port")
        head = span[0] if span else None
        if head is None or port in (None, ""):
            continue
        if _head_probe(head, port, probe_fn):
            continue  # serving → nothing to relaunch
        out.append((u, hit))
    return out


def run_post_boot_pass(boot_fn=None, probe_fn=None, up_fn=None,
                       lock_acquire=None, lock_release=None,
                       intentional_fn=None, notify_fn=None,
                       path=None, now=None):
    """One guarded post-boot evaluation. Returns a dict, never raises.

    ``result`` is one of:
      ``disabled``     — operator off-switch; zero I/O.
      ``no-marker``    — nothing is expected up and nothing may be launched.
                         ONE label for three states, told apart by the message
                         alone (t_c2b5fb13): the operator deliberately brought
                         the fleet down (message names the stay-down record,
                         read via ``deliberately_down()``); the marker file is
                         present with an EMPTY footprint (message names its
                         ``reason``/``cleared_at``); or there is no readable
                         marker at all (fleet never fleet-upped).
      ``suppressed``   — intentional-autodown window in effect (checked pre-
                         lock AND re-checked under the lock).
      ``boot-unavailable`` — sparkrun could not answer the boot sweep: no
                         evidence, no action (fail-closed).
      ``steady``       — no rebooted node owns a not-serving expected unit
                         (baselines for first-seen nodes are recorded here).
      ``cooldown``   — a launch was attempted too recently.
      ``skipped``      — autodown lock / heal lease held by another repair.
      ``launched``     — cluster up issued for the rebooted units (actions
                         names the units; the boot baselines were advanced
                         BEFORE the issue, so this cannot refire this boot).
      ``launch-failed`` — cluster up returned not-ok; baselines were
                         restored so the next pass may retry within the cap.
      ``gave-up``      — the per-(node, boot) attempt cap was reached; no
                         further action until the next boot.

    Injectable hooks default to the real surfaces (sparkrun boot sweep,
    util.http_check, the hscc-cluster wrappers, autodown's lock) — tests pass
    fakes and touch neither the live fleet nor the operator's ~/.hscc.
    """
    result = {"result": "steady", "actions": [], "observed": {}}
    if not POST_BOOT_ENABLED:
        result.update(result="disabled",
                      message="post-boot relaunch disabled by operator "
                              "off-switch (HSCC_POST_BOOT_RELAUNCH=0)")
        return result

    path = EXPECTED_UP_FILE if path is None else path
    marker = load_marker(path)
    if not (marker.get("units") or []):
        # THREE different states read the same here (no units) and all three
        # are the same verdict: `no-marker`, ok=True, no action, no launch —
        # launch authority needs NON-EMPTY units, so no wording here can arm
        # one. Only the WORDING distinguishes them, so an operator reading
        # `hscc check post_boot` is told which state the fleet is actually in
        # instead of being told "not fleet-upped" in two states that aren't:
        #   * stay-down record present (D1) — somebody holds a standing
        #     stay-down order, and its reason + set_at are the answer;
        #   * marker FILE present with an EMPTY footprint (t_c2b5fb13) —
        #     nothing is expected up and nobody holds a stay-down order. This
        #     is what an empty-plan autodown wake leaves behind (t_9c437f03
        #     step 1b records `units: []` and pops the record): the marker's
        #     own `reason` is then the only breadcrumb that a wake tried and
        #     could not describe the fleet, so it is printed here;
        #   * no readable marker at all — the fleet was never fleet-upped, or
        #     the file was deleted/corrupted (absence of evidence, which is
        #     how `load_marker` and `deliberately_down` both fail-safe).
        # `reason` / `cleared_at` are PRINTED, never matched: no branch here
        # reads prose, and no verdict depends on it (the PR #243 rule —
        # pinned by test_wording_is_never_a_behavioural_input).
        record = deliberately_down(path)
        if record:
            message = (f"fleet deliberately down since "
                       f"{record.get('set_at') or '?'} "
                       f"({record.get('reason') or 'no reason given'}) "
                       f"— nothing to do")
        elif marker_file_readable(path):
            bits = [f"reason: \"{marker.get('reason') or 'no reason given'}\""]
            if marker.get("cleared_at"):
                bits.append(f"cleared_at: {marker['cleared_at']}")
            message = ("expected_up marker present with an EMPTY footprint "
                       "(nothing is expected up and nobody holds a standing "
                       "stay-down order; " + "; ".join(bits) + ") "
                       "— nothing to do")
        else:
            message = ("no expected_up marker (fleet not fleet-upped, "
                       "or brought down on purpose) — nothing to do")
        result.update(result="no-marker", message=message)
        return result

    if intentional_fn is None:
        intentional_fn = _intentional_window_active
    if intentional_fn():
        result.update(result="suppressed",
                      message="intentional-autodown window in effect — "
                              "serving layer is operator-managed, not "
                              "relaunching")
        return result

    nodes = sorted({str(n) for u in (marker.get("units") or [])
                    for n in (u.get("nodes") or []) if n})
    if boot_fn is None:
        boot_fn = fetch_boot_ids
    boots = boot_fn(nodes)
    if boots is None:
        result.update(result="boot-unavailable",
                      message="sparkrun boot sweep unavailable — no evidence, "
                              "no action")
        return result

    baseline = dict(marker.get("boot_baseline") or {})
    fresh_baselines = {}   # first-sight nodes → observed boot (record, no act)
    changed = {}           # node → (old_boot, new_boot)
    for n in nodes:
        observed = boots.get(n)
        result["observed"][n] = observed
        if observed is None:
            continue       # unreachable node: NO evidence → never act on it
        base = baseline.get(n)
        if base is None:
            fresh_baselines[n] = observed
        elif base != observed:
            changed[n] = (base, observed)

    def _persist():
        m = load_marker(path)
        m.setdefault("boot_baseline", {}).update(fresh_baselines)
        if changed_final:
            for n, (old, new) in changed_final.items():
                if m["boot_baseline"].get(n) == old:
                    m["boot_baseline"][n] = new
        return _save_marker(m, path)

    # First-sight nodes: record the baseline, act on nothing (a fresh marker
    # must never self-trigger — the operator may have fleet-upped while the
    # node was already running this boot).
    changed_final = {}
    if fresh_baselines and not changed:
        _persist()
        result.update(message=f"recorded boot baseline for "
                              f"{len(fresh_baselines)} node(s)")
        return result
    if not changed:
        if fresh_baselines:
            _persist()
        result.update(message="no rebooted node owns a not-serving "
                              "expected_up unit")
        return result

    candidates = _candidate_units(marker, set(changed), probe_fn=probe_fn)
    if not candidates:
        # Rebooted nodes, but every unit on them already serves (or no unit
        # on them is in the marker) → advance baselines, stay quiet.
        changed_final = dict(changed)
        _persist()
        result.update(message="rebooted nodes carry no not-serving "
                              "expected_up unit")
        return result

    # Cooldown: measured from the last attempted launch (persisted).
    last = marker.get("last_launch_attempt_iso")
    if last:
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(last)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            import time
            age = time.time() - dt.timestamp()
            if age < POST_BOOT_COOLDOWN_S:
                result.update(result="cooldown", message=(
                    f"last post-boot launch attempt {int(age)}s ago "
                    f"(cooldown {int(POST_BOOT_COOLDOWN_S)}s) — waiting"))
                return result
        except (ValueError, TypeError):
            pass  # unparseable stamp must not latch the pass forever

    attempts = dict(marker.get("boot_attempts") or {})

    def _capped(node_new):
        n, new = node_new
        rec = attempts.get(n)
        return bool(rec and rec.get("boot_id") == new
                    and int(rec.get("count", 0)) >= POST_BOOT_MAX_ATTEMPTS)

    # Drop candidates whose (node, boot) budget is exhausted for THIS boot.
    live = []
    exhausted_nodes = set()
    for u, hit in candidates:
        capped_nodes = [n for n in hit if _capped((n, changed[n][1]))]
        if len(capped_nodes) == len(hit):
            exhausted_nodes.update(hit)
            continue
        live.append((u, [n for n in hit if n not in capped_nodes]))
    if not live:
        changed_final = {n: changed[n] for n in exhausted_nodes}
        _persist()  # ACCEPT the new boot: no more action THIS boot
        result.update(result="gave-up", message=(
            f"post-boot relaunch gave up after {POST_BOOT_MAX_ATTEMPTS} "
            f"attempt(s) for boot on {sorted(exhausted_nodes)} — "
            f"next boot id resets the budget"))
        return result

    # ── Exclusivity: the autodown lock FIRST (never race teardown/wake),
    # then the heal lease (never race engine-wedge/auto-heal/worker relaunch).
    # Same order as recover_engine_wedge: the blocking lock before the
    # non-blocking lease, so no acquirer can be starved by this path.
    if lock_acquire is None or lock_release is None:
        from . import autodown as _ad
        lock_acquire = lock_acquire or _ad._acquire_lock
        lock_release = lock_release or _ad._release_lock
    if not lock_acquire(now=now):
        result.update(result="skipped",
                      message="autodown lock held (teardown/wake in flight) "
                              "— skipped this pass")
        return result
    try:
        from . import heal_lease
        lease_ok, holder = heal_lease.acquire(
            "post-boot-relaunch",
            ",".join(sorted({n for _, hit in live for n in hit})))
        if not lease_ok:
            result.update(result="skipped", message=(
                f"heal in progress — {heal_lease.describe(holder)}"))
            return result
        try:
            # Re-read BOTH operator-intent signals UNDER the lock: a cluster
            # down / autodown teardown between the pre-lock gate and here
            # must win — its clear/intent is authoritative.
            marker = load_marker(path)
            if not (marker.get("units") or []):
                result.update(result="no-marker", message=(
                    "marker cleared under the lock (cluster down?) — "
                    "no launch"))
                return result
            if intentional_fn():
                result.update(result="suppressed", message=(
                    "intentional-autodown window opened under the lock — "
                    "no launch"))
                return result
            # Re-derive candidates from the FRESH marker (same evidence; a
            # unit that came up since the pre-lock probe is not relaunched).
            live = []
            for u in (marker.get("units") or []):
                if not isinstance(u, dict):
                    continue
                span = [str(n) for n in (u.get("nodes") or [])]
                hit = [n for n in span if n in changed
                       and not _capped((n, changed[n][1]))]
                if not hit:
                    continue
                port = u.get("port")
                head = span[0] if span else None
                if head is None or port in (None, ""):
                    continue
                if _head_probe(head, port, probe_fn):
                    continue
                live.append((u, hit))
            if not live:
                changed_final = dict(changed)
                _persist()
                result.update(message=(
                    "no candidate survived the under-lock re-check"))
                return result

            # EXACTLY-ONCE: advance baselines (and stamp the attempt) BEFORE
            # launching, under the lock. A same-boot repeat — including one
            # racing this very process — now sees an unchanged baseline.
            baseline = marker.setdefault("boot_baseline", {})
            baseline.update(fresh_baselines)
            touched = sorted({n for _, hit in live for n in hit})
            for n in touched:
                baseline[n] = changed[n][1]
                rec = attempts.get(n)
                if not (rec and rec.get("boot_id") == changed[n][1]):
                    attempts[n] = {"boot_id": changed[n][1], "count": 0}
                attempts[n]["count"] = int(attempts[n].get("count", 0)) + 1
            marker["boot_attempts"] = attempts
            marker["last_launch_attempt_iso"] = now_iso()
            marker["last_pass"] = {
                "ran_at": now_iso(), "result": "launching",
                "units": [u.get("unit_id") for u, _ in live]}
            _save_marker(marker, path)

            if up_fn is None:
                up_fn = _default_up_fn()
            if up_fn is None:
                # No cluster-up surface → restore baselines so a later pass
                # (once sparkrun/wrappers are back) can still act.
                _restore_baselines(path, touched, fresh_baselines, changed)
                result.update(result="launch-failed", message=(
                    "hscc-cluster wrappers unavailable — no launch issued"))
                return result

            issued_nodes = sorted({n for _, hit in live for n in hit})
            units = [u.get("unit_id") for u, _ in live]
            try:
                up_res = up_fn(issued_nodes) or {}
            except Exception as e:
                up_res = {"success": False,
                          "output": f"cluster up raised {type(e).__name__}: {e}"}
            ok = bool(up_res.get("success", up_res.get("ok", False)))
            changed_final = dict(changed)  # keep the advances either way below
            if not ok:
                # Failed attempt: restore baselines for the touched nodes so
                # the retry can re-decide on the next pass (the attempt cap
                # bounds the loop), and reset fresh_baselines we just wrote —
                # those nodes were merely first-seen, we acted ON them, so
                # their evidence stays recorded ONLY on success.
                _restore_baselines(path, touched, fresh_baselines, changed)
                changed_final = {}
                result.update(result="launch-failed", actions=units, message=(
                    f"cluster up (nodes={','.join(issued_nodes)}) for "
                    f"{units} FAILED: {str(up_res.get('output', ''))[:300]}"))
                _notify(notify_fn, "HSCC post-boot relaunch FAILED",
                        f"cluster up for {units} did not succeed: "
                        f"{str(up_res.get('output', ''))[:120]}", "high")
                return result
            result.update(result="launched", actions=units, message=(
                f"host reboot detected on {issued_nodes} — relaunched "
                f"{units} via hscc cluster up (fleet-up path)"))
            _notify(notify_fn, "HSCC post-boot relaunch",
                    f"Node(s) {issued_nodes} rebooted; relaunched {units} "
                    f"via cluster up", "high")
            return result
        finally:
            heal_lease.release("post-boot-relaunch")
    finally:
        lock_release()


def _restore_baselines(path, touched, fresh_baselines, changed):
    """Undo the pre-launch baseline advance for the touched nodes.

    A failed launch must remain re-decidable on the next pass; the attempt
    counter (kept) is what bounds the retries.
    """
    try:
        m = load_marker(path)
        base = m.setdefault("boot_baseline", {})
        for n in touched:
            if n in fresh_baselines:
                base.pop(n, None)
            elif n in changed:
                base[n] = changed[n][0]
        _save_marker(m, path)
    except Exception as e:
        log(f"post_boot: baseline restore failed ({e}) — retry for the "
            f"failed boot is suppressed until the next boot", "WARN")


def _default_up_fn():
    """The launch surface: ``hscc-cluster cmd_cluster_up(nodes=<nodes>)``.

    Returns None when the wrappers cannot be loaded (caller fails closed —
    there is NO second start path). Reuses recover's cached loader; the A2
    guard, already-serving skip and ``--ensure`` live inside cmd_cluster_up.
    """
    try:
        from .recover import _load_cluster_engine
        cl = _load_cluster_engine()
    except Exception:
        cl = None
    if cl is None:
        log("post_boot: hscc-cluster wrappers unavailable — cannot launch",
            "ERROR")
        return None

    def _up(nodes):
        return cl.cmd_cluster_up(dry_run=False, nodes=list(nodes))
    return _up


def _notify(notify_fn, title, msg, priority):
    if notify_fn is None:
        try:
            from .desktop import send_macos_notification as notify_fn  # noqa
        except Exception:
            return
    try:
        notify_fn(title, msg, priority=priority)
    except Exception:
        pass


# ── Daemon stream wrapper ────────────────────────────────────────────────
# Streams that read ok=False make `hscc verify` RED and the trigger engine
# fire. The pass writes ok=False ONLY when a relaunch it owns genuinely
# failed (launch-failed / gave-up). Every no-action outcome — including a
# suppressed intentional window, a held lock/lease (another repair owns the
# moment), and an unavailable boot sweep (dgx's fleet-visibility stream
# already cries the sparkrun-blind wolf) — is ok=True with the reason,
# because nothing is wrong in those states.
_STREAM_OK_RESULTS = {"disabled", "no-marker", "steady", "cooldown",
                      "boot-unavailable", "launched", "suppressed",
                      "skipped"}


def check_post_boot_relaunch(pass_fn=None):
    """Daemon check (every post_boot tick): guarded pass + stream write."""
    if pass_fn is None:
        pass_fn = run_post_boot_pass
    try:
        res = pass_fn() or {}
    except Exception as e:  # the supervisor restarts the thread; be honest
        log(f"post_boot pass raised: {e}", "ERROR")
        write_state(POST_BOOT_STREAM, {
            "ok": False, "last_check": now_iso(),
            "message": f"post-boot pass raised: {e}"})
        return False
    outcome = res.get("result", "steady")
    ok = outcome in _STREAM_OK_RESULTS
    write_state(POST_BOOT_STREAM, {
        "ok": ok,
        "result": outcome,
        "actions": res.get("actions") or [],
        "observed": res.get("observed") or {},
        "last_check": now_iso(),
        "message": res.get("message", outcome),
    })
    if outcome != "steady":
        log(f"Post-boot check: {outcome} — {res.get('message', '')}")
    return ok
