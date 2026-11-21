"""Autoscale decision logic based on vLLM queue depth.

Decides whether to add or remove workers based on throughput data
from hscc_daemon.throughput.compute_throughput().

Actually spinning workers via sparkrun is a separate later card —
this module only produces the decision.

PAUSED NODES ARE NOT IN THE FLEET
---------------------------------
A node the operator parked through ``node_pauses`` is out of the fleet for as
long as its pause record holds it, so this decision treats it as neither
capacity nor a candidate:

* **not capacity** — a parked node can still answer ``/metrics``: the CLI
  records the overlay after *issuing* the stop (``stopped=False`` means
  "not confirmed down yet"), and a unit that is draining still reports a
  queue. Left alone, its numbers would be summed into the fleet totals and
  its slot counted as a running worker, so the decision would believe the
  fleet is bigger and busier than the operator's fleet actually is. Both are
  removed here: the host leaves the worker count and its per-node counters
  leave the totals.
* **not a candidate** — ``max_workers`` is a static config ceiling that says
  nothing about which nodes exist right now. Every worker slot a parked GPU
  could have held is unreachable until the operator resumes it, so the
  ceiling is lowered by the number of parked nodes. With no reachable slot
  left the answer is "none", never a scale-up onto a parked node.
* **not by way of** — a pause is keyed to one host but a unit can span
  several (tensor-parallel), and the span members are just as parked. This
  consults the overlay's :func:`node_pauses.paused_nodes` (keyed hosts ∪ span
  nodes), which is exactly what that module's own skip-seam contract asks of
  autoscale: a seam that asked ``paused(host)`` alone would happily place a
  worker on the other half of a paused span.

The overlay is consulted INSIDE :func:`decide_scale` rather than by the
callers, so the seam cannot be forgotten: every caller (the ``hscc autoscale``
CLI, the ``/v1/autoscale`` route, ``hscc-cluster``'s advice) derives
``current_workers`` from ``fleet.nodes_ok``, which counts a reachable-but-
paused node as a worker. A caller that has already read the overlay may pass
``paused=`` and skip the read; tests may point ``directory=`` at a temp dir so
the operator's real ``~/.hscc`` is never consulted.

Stdlib-only, and the read is node_pauses' own non-raising ``load()``, so a
missing or malformed overlay degrades to "nothing is paused" the same way it
does for every other consumer of that file.
"""

from __future__ import annotations

import contextlib
import sys
from urllib.parse import urlsplit


def host_of(endpoint):
    """The host a metrics endpoint (or bare host) points at; ``None`` if not one.

    ``by_node`` keys are ``http://{node}:{port}/metrics`` URLs, while pause
    records are keyed by host, so both sides go through here to get one
    comparable key. Hostnames are case-insensitive, hence lowercased; a
    trailing ``:port`` on a scheme-less value is dropped for the same reason.
    """
    if not isinstance(endpoint, str):
        return None
    raw = endpoint.strip()
    if not raw:
        return None
    if "://" in raw:
        try:
            raw = urlsplit(raw).hostname or ""
        except ValueError:  # unparseable URL — not a host we can match
            return None
    else:
        head, _, tail = raw.rpartition(":")
        if tail.isdigit():  # "192.0.2.11:8000" -> "192.0.2.11"
            raw = head
    raw = raw.strip().strip("[]").strip()
    return raw.lower() or None


def _host_set(value):
    """A caller-supplied pause set, normalised (``None`` passes through).

    Accepts the set :func:`node_pauses.paused_nodes` returns, an iterable of
    hosts, a mapping keyed by host, or a single host string — the shapes a
    caller has to hand over are not one type, and none of them should turn a
    decision into a TypeError.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return {h for h in (host_of(value),) if h}
    items = value.keys() if isinstance(value, dict) else value
    if not isinstance(items, (list, tuple, set, frozenset)) and not hasattr(
            items, "__iter__"):
        items = [items]
    out = set()
    for item in items:
        host = host_of(item)
        if host:
            out.add(host)
    return out


def _warn_unreadable(exc):
    """Say out loud that the overlay could not be read; never raise.

    stderr, never stdout — stdout is ``hscc autoscale --json``'s channel — and
    if even stderr is unusable, fall to the interpreter's original one. A WARN
    that itself raises would become the very failure it reports.
    """
    message = (f"[autoscale] WARN: pause overlay unreadable "
               f"({type(exc).__name__}: {exc}); deciding with no paused nodes")
    for stream in (sys.stderr, sys.__stderr__):
        if stream is None:  # pythonw / a stripped interpreter has no stderr
            continue
        with contextlib.suppress(Exception):
            # suppress, not except/pass: the loop IS the fallback, and a WARN
            # must not be able to raise out of a read-only decision.
            stream.write(message + "\n")
            return


def _overlay_nodes(directory=None):
    """Every node the pause overlay holds (keyed hosts ∪ span members).

    Empty when there is no overlay — which genuinely means "nothing is
    paused". ``node_pauses.load()`` is already non-raising (a corrupt overlay
    is its own loud WARN), so the only way to land in the handler below is an
    overlay that cannot be reached at all; there the advisory decision falls
    back to its pre-overlay behaviour rather than 500-ing a read-only route,
    and :func:`_warn_unreadable` says so.
    """
    try:
        from hscc_daemon import node_pauses
        return _host_set(node_pauses.paused_nodes(directory)) or set()
    except Exception as exc:  # noqa: BLE001 - a decision must not die here
        _warn_unreadable(exc)
    return set()


def _snapshot_hosts(throughput):
    """Every host a throughput snapshot measured, normalised."""
    by_node = throughput.get("by_node") if isinstance(throughput, dict) else None
    if not isinstance(by_node, dict):
        return set()
    return {host for host in map(host_of, by_node) if host}


def _paused_overlay(paused, directory):
    """The paused hosts to work with: the caller's ``paused``, else the overlay.

    Read ONCE per decision and shared by both the capacity count and the
    ceiling: two reads could disagree across a pause the CLI wrote in between,
    and a decision built from two different overlays is worse than one built
    from a stale one.
    """
    if paused is None:
        return _overlay_nodes(directory)
    return _host_set(paused) or set()


def paused_in(throughput, *, paused=None, directory=None):
    """The hosts a pause is holding, out of the hosts a snapshot measured.

    ``paused`` — the paused hosts, already known (see :func:`_host_set`);
    when it is ``None`` the overlay at ``directory`` is read here.
    """
    return _snapshot_hosts(throughput) & _paused_overlay(paused, directory)


def decide_scale(throughput, *, current_workers, min_workers=1, max_workers=8,
                 high_waiting=4, low_waiting=0, paused=None, directory=None):
    """Decide whether to scale the fleet up, down, or keep steady.

    Args:
        throughput: dict from compute_throughput() with 'fleet' and 'by_node'.
                    May also be None or an empty dict.
        current_workers: number of workers currently running. Paused nodes are
                    removed from this below — a caller that counted one still
                    gets the decision for the fleet the operator has.
        min_workers: lower bound for fleet size.
        max_workers: upper bound for fleet size. Lowered by every node the
                    pause overlay holds: a parked GPU is a slot no worker can
                    be placed on until the operator resumes it.
        high_waiting: queue depth threshold to trigger scale-up.
        low_waiting: queue depth threshold for considering scale-down.
        paused: paused hosts the caller already knows (a set, an iterable, a
                    host-keyed mapping or one host string). ``None`` reads the
                    overlay; an explicit empty set means "nothing is paused"
                    and suppresses the read.
        directory: the ``~/.hscc`` root to read the overlay from (the same
                    seam every ``node_pauses`` reader takes; tests pass a temp
                    dir).

    Returns:
        dict with 'action' ('scale_up', 'scale_down', 'none'),
        'target' (desired worker count), and 'reason' (human-readable).
        Always also carries 'capacity' (the worker count the decision used,
        paused nodes excluded) and 'paused_excluded' (the snapshot hosts the
        overlay took out of it) so an operator reading ``hscc autoscale`` can
        see that a pause, not the fleet, is what shaped the answer.
    """
    # Robust to None or non-dict throughput
    if not throughput or not isinstance(throughput, dict):
        return {"action": "none", "reason": "no throughput data"}

    fleet = throughput.get("fleet")
    if not fleet or not isinstance(fleet, dict):
        return {"action": "none", "reason": "no throughput data"}

    # The paused hosts this snapshot actually measured (capacity the caller
    # may have counted) and every host the overlay holds (the slots that
    # cannot be filled). Span members are in both, per the module docstring.
    overlay_held = _paused_overlay(paused, directory)
    held_in_snapshot = _snapshot_hosts(throughput) & overlay_held

    waiting = fleet.get("waiting", 0) or 0
    running = fleet.get("running", 0) or 0

    # A parked node's leftover counters are not fleet load: subtract them so
    # a draining engine neither trips a scale-up nor blocks a scale-down.
    by_node = throughput.get("by_node")
    if held_in_snapshot and isinstance(by_node, dict):
        for endpoint, metrics in by_node.items():
            if host_of(endpoint) not in held_in_snapshot:
                continue
            if not isinstance(metrics, dict):
                continue
            waiting = max(0, waiting - (metrics.get("waiting", 0) or 0))
            running = max(0, running - (metrics.get("running", 0) or 0))

    # Neither capacity nor ceiling includes a paused node. Clamped at 0 from
    # below: this may only ever REDUCE what the caller counted, because the
    # overlay reports what is parked, not what is free.
    capacity = max(0, (current_workers or 0) - len(held_in_snapshot))
    ceiling = max(0, (max_workers or 0) - len(overlay_held))
    excluded = sorted(held_in_snapshot)
    paused_note = (f"; {len(excluded)} paused node"
                   f"{'s' if len(excluded) != 1 else ''} excluded"
                   if excluded else "")

    # Scale UP: queue is backed up and we have room. `ceiling` already folds
    # in max_workers, so this can never aim at a parked node.
    queue_backed = waiting >= high_waiting
    room_by_config = capacity < (max_workers or 0)
    if queue_backed and capacity < ceiling:
        target = min(capacity + 1, ceiling)
        return {
            "action": "scale_up",
            "target": target,
            "reason": f"queue depth {waiting} >= {high_waiting}{paused_note}",
            "capacity": capacity,
            "paused_excluded": excluded,
        }

    # Scale DOWN: fully idle and we can reduce (counting only real workers).
    if waiting <= low_waiting and running == 0 and capacity > min_workers:
        target = max(capacity - 1, min_workers)
        return {
            "action": "scale_down",
            "target": target,
            "reason": f"fleet idle (no running/queued requests){paused_note}",
            "capacity": capacity,
            "paused_excluded": excluded,
        }

    # Which "none"? A backlog that the overlay (not the config) left without a
    # node is not the fleet being healthy — say so, or the operator reads a
    # healthy-band answer while their parked GPU is what starves the queue.
    if queue_backed and room_by_config:
        reason = (f"queue depth {waiting} >= {high_waiting}, but no unpaused "
                  f"node is left to place a worker on "
                  f"({len(overlay_held)} paused)")
    else:
        reason = f"within healthy band{paused_note}"
    return {
        "action": "none",
        "reason": reason,
        "capacity": capacity,
        "paused_excluded": excluded,
    }
