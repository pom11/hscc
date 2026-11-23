"""Agent-tool surface for the heal breaker (card t_14b86753, audit D4).

One read-only tool — ``heal_status`` — and deliberately NO clear tool. The
audit (docs/audits/2026-11-23-agent-surface.md §2.1, §10 D4) found the breaker
was the one alert whose own text told a worker to run a command the worker
could not reach: ``hscc heal-clear <node>``. D4's answer is the split this
module implements: the READ joins the agent surface, the WRITE does not. An
agent can see which nodes the daemon has stopped healing, for how long, and
hand the operator the exact verb — it cannot re-arm the repair paths itself,
because clearing a breaker is an operator decision after investigating the
hardware, not a per-turn recovery step.

No second source of truth: the handler calls :func:`heal_breaker.snapshot`,
the same module the daemon writes and ``hscc heal-status`` reads, and that
function shares its window and suspension arithmetic with
:func:`heal_breaker.suspended` (the predicate every heal path consults). The
tool therefore cannot disagree with the CLI about the strike count, the age of
the oldest strike, whether a node is suspended, or when it resumes. It also
does not render prose of its own: the caller gets numbers and the path of the
ledger, plus the CLI verb it is deliberately not exposing.

The import of ``hscc_daemon.heal_breaker`` is lazy and inside the handler, the
same rule as ``autodown_tools._cli``: importing a daemon module at plugin load
time would make the plugin's import depend on the daemon's import graph.
"""

#: No arguments: the breaker ledger is a single per-cluster fact, there is
#: nothing to select and nothing to confirm. `additionalProperties: False`
#: keeps a model from inventing a `node` or a `confirm` — a `confirm` in
#: particular must never become an accepted key on a surface that is read-only
#: by contract, because on every neighbouring heal tool it means "execute".
HEAL_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The clear verb this tool deliberately does NOT expose. Named in the result
#: (and per node) because the breaker's own alert tells a worker to run it.
CLEAR_VERB = "hscc heal-clear <node>"

_CLEAR_NOTE = (
    "heal-clear is deliberately not an agent tool (audit D4): clearing a "
    "node's strikes re-arms the daemon's repair paths immediately, which is "
    "an operator decision after investigating the hardware. Read the ledger "
    "here, then hand `hscc heal-clear <node>` to the operator. A node also "
    "self-clears when its strikes age out of the window."
)


def heal_status(args, **kwargs):
    """Breaker strikes per node: count, age, suspension and the CLI clear verb.

    Returns the ledger view ``hscc heal-status`` prints, as numbers instead of
    a panel: ``file``/``file_age_s`` (the durable ledger), ``window_s`` and
    ``max_strikes`` (the breaker's own thresholds), and one row per node with
    ``strikes`` (inside the window), ``recorded`` (raw ledger length),
    ``strike_ages_s``/``oldest_age_s``/``newest_age_s``, ``suspended`` and
    ``suspended_s``. ``suspended_nodes`` is the short list of nodes the daemon
    is refusing to heal right now.

    Read-only: nothing here writes ``~/.hscc/heal-breaker.json``, resets an
    alert, touches a node or starts/stops a unit. ``clear_tool`` is always
    ``None`` and ``clear_verb`` always names the CLI verb that stays
    operator-only.
    """
    from hscc_daemon import heal_breaker

    snap = heal_breaker.snapshot()
    nodes = []
    for row in snap["nodes"]:
        row = dict(row)
        row["clear_verb"] = f"hscc heal-clear {row['node']}"
        nodes.append(row)
    return {
        "ok": True,
        "source": snap["file"],
        "file_age_s": snap["file_age_s"],
        "window_s": snap["window_s"],
        "max_strikes": snap["max_strikes"],
        "count": snap["count"],
        "nodes": nodes,
        "suspended_nodes": [r["node"] for r in nodes if r["suspended"]],
        "clear_verb": CLEAR_VERB,
        "clear_tool": None,
        "clear_note": _CLEAR_NOTE,
    }
