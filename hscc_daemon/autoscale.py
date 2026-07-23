"""Autoscale decision logic based on vLLM queue depth.

Pure functions — no I/O, no side effects.
Decides whether to add or remove workers based on throughput data
from hscc_daemon.throughput.compute_throughput().

Actually spinning workers via sparkrun is a separate later card —
this module only produces the decision.
"""


def decide_scale(throughput, *, current_workers, min_workers=1, max_workers=8, high_waiting=4, low_waiting=0):
    """Decide whether to scale the fleet up, down, or keep steady.

    Args:
        throughput: dict from compute_throughput() with 'fleet' and 'by_node'.
                    May also be None or an empty dict.
        current_workers: number of workers currently running.
        min_workers: lower bound for fleet size.
        max_workers: upper bound for fleet size.
        high_waiting: queue depth threshold to trigger scale-up.
        low_waiting: queue depth threshold for considering scale-down.

    Returns:
        dict with 'action' ('scale_up', 'scale_down', 'none'),
        'target' (desired worker count), and 'reason' (human-readable).
    """
    # Robust to None or non-dict throughput
    if not throughput or not isinstance(throughput, dict):
        return {"action": "none", "reason": "no throughput data"}

    fleet = throughput.get("fleet")
    if not fleet or not isinstance(fleet, dict):
        return {"action": "none", "reason": "no throughput data"}

    waiting = fleet.get("waiting", 0) or 0
    running = fleet.get("running", 0) or 0

    # Scale UP: queue is backed up and we have room
    if waiting >= high_waiting and current_workers < max_workers:
        target = min(current_workers + 1, max_workers)
        return {
            "action": "scale_up",
            "target": target,
            "reason": f"queue depth {waiting} >= {high_waiting}",
        }

    # Scale DOWN: fully idle and we can reduce
    if waiting <= low_waiting and running == 0 and current_workers > min_workers:
        target = max(current_workers - 1, min_workers)
        return {
            "action": "scale_down",
            "target": target,
            "reason": "fleet idle (no running/queued requests)",
        }

    return {"action": "none", "reason": "within healthy band"}
