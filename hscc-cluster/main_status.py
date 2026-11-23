"""Agent-tool surface for the configured main orchestrator (card t_c920b0ca).

One read-only tool — ``main_status`` — and deliberately NO lifecycle tool.
``hscc main start`` spawns/re-attaches the operator's orchestrator pane and
``hscc main wakeups`` rewrites the daemon's wake schedule, so both stay
CLI/operator-only. What an agent may ask is the read: who is the configured main
orchestrator, and what is its live shape? That is the same question the operator
asks with ``hscc main status``.

## One door: the CLI's own verb

The handler does not resolve the name, discover panes or read the waker itself.
It asks the unified CLI's own verb in-process —
``hscc_daemon.main_cli.cmd_main(["status", "--json"])`` — and reports what it
printed, exactly like the sibling read tools (``autodown_status``,
``merge_queue_status``, ``node_paused``). The verb in turn calls
``main_cli.status()``, which reads the identity through the ONE resolver
(``hscc-roles/main_orch.py``: ``main_name``/``main_profile``/``main_board``) and
the pane through ``repl_inject`` (the same discovery/busy marker ``hscc orch
inject`` uses). A renamed main therefore needs no edit here, and a pane the CLI
cannot see is a pane this tool cannot invent.

``main_cli.cmd_main`` prints the JSON receipt for ``status`` and returns an exit
code; the argv is a fixed literal, so no caller input can steer it to ``start``
or ``wakeups``. That is the read-only guarantee: the shape of the call, not a
promise in prose.

## Waker state

``hscc main status`` reports the pane; the daemon-side waker is the other half
of "is the main orchestrator actually being woken?" (``hscc orch waker status``).
The tool adds that read as a ``waker`` block, taken from
``hscc_daemon.orch_waker.waker_status()`` — the one function the CLI verb
renders, not a second implementation. Like ``main_cli.status`` it is pure file
reads (config, stream stamp, cursor ledger, audit tail) and never ticks the
waker or writes anything; an unavailable daemon module degrades to
``{"ok": false, "detail": ...}`` rather than hiding the main-orchestrator answer.

The daemon modules are imported lazily inside the handlers, the same rule as
``autodown_tools._cli``/``merge_queue_tools._cli``: importing ``hscc_daemon`` at
plugin import time would make the plugin's load depend on the daemon's import
graph.
"""

import io
import json
from contextlib import redirect_stdout

#: No arguments: the main orchestrator is a single cluster-wide identity, so
#: there is nothing to select and nothing to confirm. ``additionalProperties:
#: False`` keeps a model from inventing a ``profile``, ``confirm`` or ``wake``
#: key — none has a meaning on a surface that is read-only by contract, and a
#: ``confirm`` in particular must never advertise a lifecycle door here.
MAIN_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The tool's own envelope keys, pinned against the CLI payload by
#: test_the_envelope_shadows_no_field_of_the_payload: the payload is spread in,
#: so a name collision would silently win or lose a field.
_ENVELOPE = ("rc", "report", "waker")


def _cli():
    """The ``hscc main`` verb module, imported on use (see the module docstring)."""
    from hscc_daemon import main_cli
    return main_cli


def _waker_status():
    """The waker's own status function, or an honest unavailable marker."""
    try:
        from hscc_daemon import orch_waker
        status = orch_waker.waker_status()
    except Exception as exc:  # noqa: BLE001 — a read must not crash the answer
        return {"ok": False, "detail": f"orch_waker status unavailable: {exc}"}
    if not isinstance(status, dict):
        return {"ok": False, "detail": "orch_waker status returned no object"}
    return {"ok": True, **status}


def _report_status():
    """Ask the CLI's own verb. -> (rc, raw stdout text).

    ``["status", "--json"]`` is exactly what an operator types for this answer.
    The argv is a literal — a caller cannot reach ``start`` or ``wakeups`` — so
    the read-only guarantee is the shape of this call.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli().cmd_main(["status", "--json"])
    return rc, buf.getvalue().strip()


def main_status(args, **kwargs):
    """The configured main orchestrator and its live shape (pane + waker state).

    Returns the payload ``hscc main status --json`` prints (``name``,
    ``profile``, ``board``, ``state``, ``running``, ``busy``, ``pane``,
    ``target``, ``panes``, ``detail``) plus a ``waker`` block from
    ``hscc_daemon.orch_waker.waker_status()`` (enabled, stream freshness, boards,
    skipped panes) and this tool's ``rc``. The identity always comes from the one
    resolver (``hscc-roles/main_orch.py``), never from a name baked in here.
    Read-only: nothing here starts, re-attaches or renames the main
    orchestrator, and nothing here edits its wake schedule.
    """
    rc, report = _report_status()
    if rc != 0:
        # A non-zero verb is a refusal/failure, and the text is the only
        # explanation there is — report it rather than an empty payload that
        # would read as "no main orchestrator".
        return {"ok": False, "rc": rc, "report": report}
    try:
        payload = json.loads(report)
    except ValueError:
        # The verb's --json is the contract; if it ever stops being parseable
        # the agent still gets the operator's own text instead of a crash or a
        # silently empty dict.
        return {"ok": True, "rc": rc, "report": report}
    if not isinstance(payload, dict):
        return {"ok": True, "rc": rc, "report": report}
    out = dict(payload)
    out["waker"] = _waker_status()
    out["rc"] = rc
    # The payload carries its own ``ok`` from the verb; keep it authoritative
    # rather than overwriting it with the tool's own view.
    out.setdefault("ok", True)
    return out
