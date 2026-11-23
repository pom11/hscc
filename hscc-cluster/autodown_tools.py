"""Agent-tool surface for the idle autodown/autoup state (card t_c036808c).

One read-only tool — ``autodown_status`` — and deliberately NO mutation tool.
The audit (docs/audits/2026-11-23-agent-surface.md §2.4/§3) found ``autodown``
is not CLI-only (it already has full route parity) and decided the split: the
READ may join the agent surface, the WRITE may not ("🔒 for the tool —
fleet-power policy is daemon/operator scope, not a per-turn agent call"). So
``enable``/``disable``/``wake``/``cancel`` stay CLI (+ route, confirm-gated)
only; an agent can ask whether the serving layer is armed to power down, and
cannot arm or disarm it. That is the whole design of this module.

Like ``node_pause_tools``, this handler adds no policy and no second door: it
calls the CLI's own read-only verb (``autodown_cli._cmd_status``) in-process
with the verb's machine flag and reports what it printed. One door means one
source of truth — the verb is the operator's own ``hscc autodown status``, the
verb builds its payload through the ONE shared builder
``autodown.status_snapshot()`` (t_8743f98c), and ``GET /v1/autodown/status``
calls that same builder. CLI, route and tool therefore cannot disagree about
enabled/disabled, about the state, or about which interlock is holding a
teardown off, because none of them computes it a second time. The verb — not
the builder — is the door this plugin uses, for the same reason
``node_pause_tools`` uses the verb rather than the executor: whatever gate or
enrichment the CLI adds next is automatically the tool's too, and a tool that
imported the builder directly would be a second door that can be widened
without anyone touching the CLI.

Read-only is not a convention here, it is the shape of the call.
``_cmd_status`` is the one verb in the group that never calls
``record_activity``/``save_config``/``autoup``/``teardown`` (the docstring in
autodown_cli.py:12 states it, and the "status — read-only, never mutates"
section of ``hscc_daemon/tests/test_autodown_cli.py`` pins it), so this handler
has no path to a mutation — it cannot even advance the idle timer, which every
mutating verb does first. It also never touches the fleet: it does not accept
a host, does not shell out, and does not resolve a node. The one ambient read
inside the builder (the ``_serving_actually_up`` probe, taken only when the
record says ``down``) belongs to the verb, is read-only, and is exactly what
the CLI already does when the operator runs it.

The import of ``hscc_daemon.autodown_cli`` is lazy and inside the handler, the
same rule as ``node_pause_tools._cli``: importing a daemon module at plugin
import time would make the plugin's load depend on the daemon's import graph.
"""

import io
import json
from contextlib import redirect_stdout

#: No arguments: the state is a single per-cluster fact, there is nothing to
#: select and nothing to confirm. `additionalProperties: False` keeps a model
#: from inventing a `host` or a `confirm` — neither has a meaning here, and a
#: `confirm` in particular must never become an accepted key on a surface that
#: is read-only by contract.
AUTODOWN_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The tool's own envelope keys. Pinned against the payload by
#: test_the_cli_payload_has_no_key_the_envelope_would_shadow — the payload is
#: spread in, so a name collision would silently win or lose a field.
_ENVELOPE = ("ok", "rc", "report")


def _cli():
    """The autodown verb module, imported on use (see the module docstring)."""
    from hscc_daemon import autodown_cli
    return autodown_cli


def _report_status():
    """Ask the CLI's own verb. -> (rc, raw stdout text).

    ``json_mode=True`` is the verb's documented machine flag — the same one
    ``hscc autodown status --json`` sets — and it is the only argument the verb
    takes besides the unused positional rest, so there is no flag combination
    here that a caller could steer.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli()._cmd_status([], True, None)
    return rc, buf.getvalue().strip()


def autodown_status(args, **kwargs):
    """Is idle autodown armed, what state is it in, and what blocks a teardown?

    Returns the payload ``hscc autodown status --json`` prints, verbatim
    (``enabled``, ``state``/``state_recorded``/``state_source``,
    ``idle_minutes``, ``last_activity_iso``, ``down_since``, the watchdog block
    and the kanban/PR-CI interlocks), plus this tool's ``ok``/``rc`` envelope.
    Read-only: nothing here writes ``~/.hscc/autodown.json`` or the watchdog
    block, and nothing here can start or stop the serving layer.
    """
    rc, report = _report_status()
    if rc != 0:
        # A non-zero verb is a refusal/failure, and the text is the only
        # explanation there is — report it rather than an empty payload that
        # would read as "autodown is disabled".
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
    out["ok"] = True
    out["rc"] = rc
    return out
