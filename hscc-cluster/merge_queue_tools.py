"""Agent-tool surface for the local merge queue (card t_96134d32).

One read-only tool — ``merge_queue_status`` — and deliberately NO mutation
tool. The audit (docs/audits/2026-11-23-agent-surface.md §2.5 / D8) found the
merge-queue READ is not CLI-only (``GET /v1/merge-queue`` already exists) and
decided the split: the read may join the agent surface, the write may not.
D8 is the explicit verdict — "``merge-queue`` mutations — keep the door closed
(no route/tool)? **Yes, closed**", because the queue force-pushes and
squash-merges, so an agent-writable merge door is a self-merge path. So
``add``/``remove``/``pause``/``resume``/``enable``/``disable`` stay CLI-only
(the daemon tick is the only thing that ever runs git/gh); an agent can ask
what is queued, in flight, paused or blocked — and cannot enqueue, drop, hold
or land anything. That is the whole design of this module.

Like ``autodown_tools`` and ``node_pause_tools``, this handler adds no policy
and no second door: it calls the CLI's own read-only verb
(``merge_queue_cli.cmd_merge_queue(["status", "--json"])``) in-process with the
verb's machine flag and reports what it printed. One door means one source of
truth — the verb is the operator's own ``hscc merge-queue status --json``, and
the per-project projection it prints (``enabled`` / ``enabled_source`` /
``default_reason`` / ``paused`` / ``queue`` / ``waiting`` / ``in_flight`` /
``last``) is the same ``merge_queue._status_doc`` the CLI renders, so CLI,
route and tool cannot disagree about whether a project is enabled, what is
queued or waiting, what is in flight or what last happened, because none of
them computes it a second time. The verb — not ``merge_queue.load_state`` or
``merge_queue.view`` — is the door this plugin uses, for the same reason the
other tool modules use the verb rather than the builder: whatever gate or
enrichment the CLI adds next to ``status`` is automatically the tool's too, and
a tool that read the ledger directly would be a second door that can be
widened without anyone touching the CLI.

Read-only is not a convention here, it is the shape of the call. The tool hands
the dispatcher a fixed ``["status", "--json"]`` argv — no caller input reaches
it — and ``status`` is the only branch of ``cmd_merge_queue`` that never calls
``add_to_queue``/``remove_from_queue``/``set_paused``/``set_enabled``/
``save_state`` (every mutating verb funnels through ``save_state``; the status
verb only reads). It also never touches the fleet and never runs a repo
command: the queue's own git/gh work happens in the daemon tick, in the
queue's own worktree, never here. The only ambient reads a status touch
performs are ``load_state`` (the queue ledger) and ``load_projects`` /
``project_config`` (the flightdeck registry — to explain *why* a project is
off); both are read-only and are exactly what the CLI already does when the
operator runs ``hscc merge-queue status``.

The import of ``hscc_daemon.merge_queue_cli`` is lazy and inside the handler,
the same rule as ``autodown_tools._cli``: importing a daemon module at plugin
import time would make the plugin's load depend on the daemon's import graph.
"""

import io
import json
from contextlib import redirect_stdout

#: No arguments: the queue is a single per-cluster ledger, there is nothing to
#: select and nothing to confirm. `additionalProperties: False` keeps a model
#: from inventing a `project` or a `confirm` — neither has a meaning here, and a
#: `confirm` in particular must never become an accepted key on a surface that
#: is read-only by contract. Narrowing to one project is what `list` is for, and
#: `list` stays CLI-only too (the whole group is a single read; the tool returns
#: every project rather than re-deriving a filtered view the CLI does not print).
MERGE_QUEUE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The tool's own envelope keys — the only keys this handler adds on top of the
#: CLI payload. The CLI payload already carries its own ``ok`` (which the verb
#: sets True for a successful status), so it is passed through, not re-set.
#: Pinned against the payload by
#: test_the_envelope_shadows_no_field_of_the_payload — the payload is spread in,
#: so a name collision here would silently win or lose a field.
_ENVELOPE = ("rc", "report")


def _cli():
    """The merge-queue verb module, imported on use (see the module docstring)."""
    from hscc_daemon import merge_queue_cli
    return merge_queue_cli


def _report_status():
    """Ask the CLI's own verb. -> (rc, raw stdout text).

    ``["status", "--json"]`` is the argv the operator types for this answer
    (``hscc merge-queue status --json``): ``--json`` is the verb's documented
    machine flag and ``status`` is the one subcommand of the group that never
    mutates. The argv is a literal — a caller cannot steer it to a mutating
    subcommand — so the read-only guarantee is the shape of this call.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli().cmd_merge_queue(["status", "--json"])
    return rc, buf.getvalue().strip()


def merge_queue_status(args, **kwargs):
    """What is in the local merge queue, per project — enabled/queued/waiting/in-flight/last.

    Returns the payload ``hscc merge-queue status --json`` prints, verbatim
    (a ``projects`` map keyed by project name; each row ``enabled`` and
    ``enabled_source``, ``default_enabled``/``default_reason`` — why the
    default-on rule did not apply: ``no repo`` / ``no GitHub remote`` / ``no
    gates`` — ``paused``, ``queue_len``/``queue``, ``waiting_len``/``waiting``,
    ``in_flight`` and ``last``), plus this tool's ``rc`` envelope. Read-only:
    nothing here writes ``~/.hscc/merge-queue-state.json``, and nothing here can
    enqueue, drop, pause, resume, enable or disable a project, or land a PR.
    """
    rc, report = _report_status()
    if rc != 0:
        # A non-zero verb is a refusal/failure, and the text is the only
        # explanation there is — report it rather than an empty payload that
        # would read as "the queue is empty" (it may not be; we just could not
        # read it).
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
    out["rc"] = rc
    # The payload carries its own ``ok`` from the verb; keep it authoritative
    # (True on a zero rc) rather than overwriting it with the tool's own view.
    out.setdefault("ok", True)
    return out
