"""Agent-tool surface for the node-pause overlay (card t_c28f4f14).

Parity with `hscc node pause|resume|paused`, and deliberately NO second
implementation: every handler here calls the unified CLI's own verb function
in-process and reports what it printed. The reasons are the fleet's own rules:

  * one source of truth for the safety policy. The orchestrator guard, the
    ``--span`` opt-in, "stop the units BEFORE recording the overlay", and the
    "keep the pause when a restart fails" rule all live behind the unified CLI
    (the orchestrator guard itself lives one level down, in the executor's
    ``orchestrator_pause_guard`` — ONE function, so the verb and this surface
    cannot disagree). Copying them here would make the
    agent a second policy to keep in sync — and the copy is the one nobody tests
    against the fleet.
  * no agent-only escape hatch from the #478 dry-run rule. There is literally
    no other path to the executor from this plugin, so a tool call cannot
    mutate what the CLI would refuse to mutate.
  * the agent sees the operator's own report, so a screenshot of a chat and a
    terminal transcript describe the same decision.

``confirm`` is this plugin's established spelling of the CLI's ``--apply``
(:func:`clusterlib.confirm_gate`); both default to "print, do not touch".

The import of ``hscc_daemon.hscc`` is lazy and inside the handlers: the
unified CLI locates this plugin as a sibling directory, so importing it back
at module load would make the two halves depend on each other at import time.
"""

import io
import json
from contextlib import redirect_stdout

# Schemas live with the tool (the `template_tools` / `orch_message` precedent)
# so the tool, its contract and its guards cannot drift apart across files.
NODE_PAUSE_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string",
                 "description": "Node to park (the address the cluster "
                                "resolves it by)."},
        "reason": {"type": "string",
                   "description": "Why the node is being parked — recorded on "
                                  "the pause and shown by node_paused."},
        "until": {"type": "string",
                  "description": "Auto-resume deadline: duration (90m, 2h, "
                                 "1d3h, 45 = seconds) or an ISO timestamp. "
                                 "Omit for open-ended (only node_resume ends "
                                 "it)."},
        "span": {"type": "boolean", "default": False,
                 "description": "Accept stopping a multi-node (tensor-parallel) "
                                "unit across EVERY node it spans. Required "
                                "with confirm when the node hosts such a unit."},
        "force": {"type": "boolean", "default": False,
                  "description": "Override the orchestrator guard: park the "
                                 "applied template's declared orchestrator "
                                 "node, or park a node whose pause would "
                                 "leave no healthy unit serving the "
                                 "orchestrator model. Both are refused "
                                 "without this. Any other node is allowed "
                                 "either way, with a note naming the replicas "
                                 "that keep serving the alias."},
        "confirm": {"type": "boolean", "default": False,
                    "description": "Execute. Without it this is a DRY RUN: the "
                                   "plan is returned, nothing stops."},
    },
    "required": ["host"], "additionalProperties": False,
}
NODE_RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string",
                 "description": "Node whose recorded pause should end. A node "
                                "named only in another pause's span is not the "
                                "owner — resume the owner."},
        "confirm": {"type": "boolean", "default": False,
                    "description": "Execute. Without it this is a DRY RUN: the "
                                   "restart commands are returned, nothing "
                                   "starts."},
    },
    "required": ["host"], "additionalProperties": False,
}
NODE_PAUSED_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _cli():
    """The unified CLI module, imported on use (see the module docstring)."""
    from hscc_daemon import hscc as unified
    return unified


def _run(fn, argv):
    """Call a CLI verb function, capturing its report. -> (rc, text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(list(argv))
    return rc, buf.getvalue().strip()


def _outcome(rc, report):
    """The tool-side shape of one CLI run.

    ``executed`` is the plugin-wide key the mutating tools already use
    (``stop_model`` etc.); ``preview`` is ``confirm_gate``'s key, kept so an
    agent that learned the guard vocabulary anywhere else reads this the same
    way. ``refused`` is the CLI's own REFUSED line — the WHY is in ``report``.
    """
    refused = "REFUSED" in report
    out = {"executed": rc == 0 and "Dry run" not in report,
           "ok": rc == 0,
           "rc": rc,
           "refused": refused,
           "report": report}
    if not out["executed"]:
        out["preview"] = True
        out["note"] = ("Nothing was changed. Re-call with confirm=true to "
                       "execute (this is the dry-run default).")
    if refused:
        out["note"] = "The CLI refused this operation; the reason is in report."
    return out


def node_pause(args, **kwargs):
    """Park one node: stop its units and record the pause."""
    argv = [args["host"]]
    if args.get("reason"):
        argv += ["--reason", str(args["reason"])]
    if args.get("until"):
        argv += ["--until", str(args["until"])]
    if args.get("span"):
        argv.append("--span")
    if args.get("force"):
        argv.append("--force")
    if args.get("confirm"):
        argv.append("--apply")
    rc, report = _run(_cli()._node_pause, argv)
    return _outcome(rc, report)


def node_resume(args, **kwargs):
    """Restart the units a pause recorded, then clear it and the heal strikes."""
    argv = [args["host"]]
    if args.get("confirm"):
        argv.append("--apply")
    rc, report = _run(_cli()._node_resume, argv)
    return _outcome(rc, report)


def node_paused(args, **kwargs):
    """Every paused node with its units, reason, since and until (read-only)."""
    rc, report = _run(_cli()._node_paused, ["--json"])
    if rc != 0:
        return {"ok": False, "rc": rc, "report": report}
    try:
        payload = json.loads(report)
    except ValueError:
        # The CLI's JSON is the contract; if it ever stops being parseable the
        # agent still gets the text rather than a crash or a silent empty list.
        return {"ok": True, "rc": 0, "report": report}
    return {"ok": True, "rc": 0,
            "count": payload.get("count", 0),
            "pauses": payload.get("pauses", [])}
