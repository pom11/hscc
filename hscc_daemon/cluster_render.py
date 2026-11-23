"""Human-view renderers for the `hscc cluster` / `hscc template` / `hscc profiles`
commands, replacing the raw ``_emit()`` JSON blob dump with themed Rich output.

The hscc-cluster engine (hscc-cluster/hscc.py, cluster_template_cli.py) still
returns plain dicts — rendering is the CLI's job. This module renders those
dicts through the shared ``cli_theme`` palette (Hermes-default gold, dark +
light). It is machine-output-agnostic: the ``--json`` hard invariant lives in
the callers (hscc.py), which print raw ``json.dumps`` on the ``--json`` path
and route the human path here.

Each ``render_*`` returns the process exit code (0/1) so the CLI can propagate
it, mirroring ``_emit``'s ``1 if result has an ``error`` key`` contract.
"""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Console

from hscc_daemon import cli_theme as theme
from hscc_daemon.cli_theme import esc

#: Microseconds, as ``datetime.isoformat()`` writes them, for display trimming.
_FRACTIONAL_SECONDS = re.compile(r"\.\d+")


# ── shared helpers ─────────────────────────────────────────────────────────

def _as_lines(value: Any) -> list[str]:
    """Collapse a scalar/blob into a list of display lines (never empty).

    Every line is Rich-markup ESCAPED here: this is the choke point through
    which raw sparkrun/ssh output and on-disk config blobs enter panel bodies,
    and a single ``[/bold]`` in that output would otherwise crash the human
    view with MarkupError (t_716cf37c). Callers still wrap the lines in their
    own ``[label]``/``[dim]`` markup, which stays live.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [esc(line) for line in value.split("\n")]
    if isinstance(value, (dict, list)):
        return [esc(json.dumps(value, indent=2, default=str))]
    return [esc(str(value))]


def _object_error(result: dict) -> str | None:
    """Return the user-facing error string, or None if the result is a success."""
    return result.get("error") if isinstance(result, dict) else None


# ── node-pause overlay: a parked node reads PAUSED, never down ────────────
#
# WHY THE RENDERER READS AN OVERLAY AT ALL. ``hscc node pause`` stops a node's
# units through sparkrun and records the intent in ``~/.hscc/node_pauses.json``
# (hscc_daemon/node_pauses.py, #560). Every read path downstream of that stop —
# sparkrun's status parse, the proxy's route table — then sees what a crash
# sees: no container, no answering engine. So the fleet views rendered a
# deliberate act as a fault: the parked machine sat in ``idle hosts`` with no
# reason attached, and (when the proxy still listed its stale route) the serving
# table spelled its model ``down``. The repair loops already skip parked nodes
# (watchdog auto-heal, keepalive, ``cluster up``, autoscale, the heal breaker);
# this is the same overlay on the VISIBILITY side, so the fleet reads the same
# way everywhere. The API edge got its twin projection in t_824b617f.
#
# WHY THE OVERLAY IS READ PER RENDER, NOT CACHED. It is one small local JSON
# file, and a pause or resume must be visible on the NEXT invocation rather than
# whenever a cache expires. Nothing here writes it — this module reads the
# overlay and nothing else, exactly as the overlay module writes only itself.
#
# WHY ``paused`` AND ``down`` CANNOT COLLIDE. A parked row keeps whatever
# diagnostic the frame carried (the error text, the idle listing) and gains the
# pause wording; a consumer that derives a fault from a missing sample must ask
# the pause overlay first, which is what these two renderers now do.
#
#: The word a parked node reads in the human fleet view. The API's machine
#: value for the same idea is ``PAUSE_STATUS`` in ``hscc-api/routes_cluster.py``
#: (t_824b617f) — the same literal, on purpose. This module cannot import it:
#: ``hscc-api`` imports ``hscc_daemon``, so that edge would be a cycle, and the
#: hyphenated dir is not an importable package name. The pairing is pinned from
#: the source text by ``tests/test_cluster_status_pause_render.py`` instead, so
#: the two surfaces cannot drift into two spellings of one state.
PAUSE_STATUS = "paused"


def _backing_node_pauses(directory=None) -> dict:
    """The durable node-pause overlay as ``{host: entry}`` — the test seam.

    A module-level function so a test can patch it wholesale (the role
    ``_backing_*`` plays on the API routes) while every normal read goes
    through :func:`node_pauses.list_pauses`, the overlay's own normalising
    reader. ``directory`` is threaded through rather than read from an
    environment variable because that module's documented test seam is an
    explicit root: an env-var root is per-process, so it is not a root the CLI
    that WROTE the overlay can ever have had (the root-split class). With
    ``directory=None`` it resolves HOME's ``~/.hscc`` — the root the CLI writes.
    """
    from hscc_daemon import node_pauses
    return node_pauses.list_pauses(directory)


def _node_key(value) -> str | None:
    """A comparable node identity, or ``None`` when this value names none.

    Trim + case-fold only — no address normalisation, no hostname-suffix
    stripping. The overlay is keyed by the host string the operator passed to
    ``hscc node pause``, which is the same string sparkrun and the proxy use.
    Guessing that ``node-3`` means a particular IP would invent a paused node on
    a machine nobody parked, which is a worse lie than an untagged row.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed.casefold() if trimmed else None


def pause_overlay(directory=None) -> dict:
    """``{node: {"reason": str, "since": str | None}}`` for parked nodes.

    Span-aware, because a tensor-parallel unit is parked as a whole: the machines
    named only in ``span_nodes`` are just as out of service as the keyed host, and
    painting them down (or merely idle) faults the operator's own action twice.
    That is :func:`node_pauses.covering`'s rule; this is its projection. A host's
    OWN record wins over a span record that happens to name it — the operator's
    own reason is the truthful one to show.

    Fails open to ``{}``. An unreadable overlay is already ``{"pauses": {}}``
    inside :func:`node_pauses.load`, and the only thing left that could raise is
    the import; a status screen must never go down because the overlay could not
    be parsed — the overlay WARNs about itself.
    """
    try:
        pauses = _backing_node_pauses(directory)
    except Exception:  # noqa: BLE001 - a display tag is never worth a traceback
        return {}
    overlay: dict = {}
    if not isinstance(pauses, dict):
        return overlay
    for host, entry in pauses.items():
        if not isinstance(entry, dict):
            continue
        row = {"reason": entry.get("reason") if isinstance(entry.get("reason"), str) else "",
               "since": entry.get("since") if isinstance(entry.get("since"), str) else None}
        keyed = _node_key(host)
        if keyed:
            overlay[keyed] = dict(row)
        for node in entry.get("span_nodes") or []:
            spanned = _node_key(node)
            if spanned:
                overlay.setdefault(spanned, dict(row))
    return overlay


def pause_display(reason, since) -> str:
    """``paused (reason, since 2026-…T…+00:00)`` — the whole human cell.

    Degrades rather than invents: no reason drops the parenthetical's first
    half, no ``since`` drops its second, neither leaves the bare word. An
    empty reason is what the overlay stores when the operator gave none
    (``node_pauses`` normalises it to ``""``), so it is the common case, not an
    error. The ``since`` stamp is the overlay's own ISO string with the
    fractional seconds trimmed — never reformatted into a local timezone,
    because this screen has no clock to convert with and a wrong-looking
    timestamp is worse than a verbose one.
    """
    bits = []
    if isinstance(reason, str) and reason.strip():
        bits.append(reason.strip())
    if isinstance(since, str) and since.strip():
        # Fractional seconds only. `isoformat()` writes microseconds when the
        # writer had any, and the whole point of the stamp is that a human can
        # read it; the offset must survive, because chopping it would silently
        # turn "+00:00" into a naive local-looking time.
        stamp = _FRACTIONAL_SECONDS.sub("", since.strip())
        bits.append(f"since {stamp}")
    text = PAUSE_STATUS
    if bits:
        text += f" ({', '.join(bits)})"
    return text


def pause_for_node(overlay, node):
    """The paused display for ``node`` if ``overlay`` holds it, else ``None``.

    The single place the CLI side asks "is THIS row's machine parked", so the
    identity rule (trim + case-fold, nothing clever) lives once rather than per
    renderer — a second spelling is how one surface calls a machine paused and
    another calls it down. Returns the finished display string, never a dict,
    because the caller wants a cell and not a decision of its own.
    """
    if not overlay:
        return None
    key = _node_key(node)
    fields = overlay.get(key) if key else None
    if not isinstance(fields, dict):
        return None
    return pause_display(fields.get("reason"), fields.get("since"))


def pause_lines(overlay: dict) -> list[str]:
    """One escaped, marked-up line per parked node: ``<node>  paused (…)``.

    Every node the overlay holds gets a line, including one the frame never
    listed: sparkrun does not report a machine whose workloads are stopped, and
    an unlisted node is exactly the one an operator must not conclude is
    healthy. A parked node that IS in the idle list therefore appears twice —
    once in sparkrun's own words (it genuinely is idle) and once in HSCC's
    (it is idle *because the operator parked it*, with the reason and the
    timestamp). Sorted by node so two invocations render identically.

    ``warn`` not ``error``: a parked node is out of service by deliberate
    choice, and colouring it red is the exact misreading this seam exists to
    remove. It is not ``dim`` either — a machine off the fleet is worth the
    operator's eye.
    """
    lines = []
    for node in sorted(overlay):
        fields = overlay[node]
        lines.append(f"{esc(node)}  [warn]"
                     f"{esc(pause_display(fields.get('reason'),
                                          fields.get('since')))}[/warn]")
    return lines


def _emit_error_panel(console: Console, title: str, result: dict) -> int:
    """Render a uniform red error panel; exit code is always 1."""
    msg = result.get("error", "command failed")
    usage = result.get("usage")
    body = esc(msg)
    if usage:
        body = f"{body}\n\nusage: {esc(usage)}"
    console.print(theme.make_status_panel(body, status="error", title=title))
    return 1


# ── cluster ────────────────────────────────────────────────────────────────

def render_cluster_status(console: Console, result: dict, pauses=None) -> int:
    """Running workloads + idle hosts -> a workload Table + a summary Panel.

    ``pauses`` is the pause overlay (:func:`pause_overlay`'s shape) and exists as
    the injection seam for tests and for a caller that already read the overlay;
    ``None`` — the production path — reads it from the root the CLI writes. The
    pre-existing lines are byte-identical when the overlay is empty, which is
    what lets the existing pins on this screen stay untouched: the pause block is
    ADDITIVE, so a fleet with no parked node renders exactly as before.
    """
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster status", result)

    table = theme.make_table("cluster status")
    table.add_column("workload", no_wrap=True)
    table.add_column("tp", justify="right")
    table.add_column("pp", justify="right")
    table.add_column("container", no_wrap=True)
    workloads = result.get("workloads") or []
    for w in workloads:
        # Workload/container names come from sparkrun over ssh — DATA.
        table.add_row(
            esc(w.get("name", "?")),
            esc(w.get("tp", "?")),
            esc(w.get("pp", "?")),
            esc(w.get("container_id", "?")),
        )
    if not workloads:
        table.add_row("[dim]no running workloads[/dim]", "", "", "")
    console.print(table)

    idle = result.get("idle_hosts") or []
    total = result.get("total_hosts", 0)
    lines = [f"[label]total hosts[/label]  {esc(total)}",
             f"[label]idle hosts[/label]   {len(idle)}"]
    if idle:
        lines.append("[dim]" + esc(", ".join(str(h) for h in idle)) + "[/dim]")
    overlay = pause_overlay() if pauses is None else pauses
    if overlay:
        lines.append(f"[label]node pauses[/label]  {len(overlay)}")
        lines.extend(pause_lines(overlay))
    console.print(theme.make_panel("cluster summary", "\n".join(lines)))
    return 0


def render_hosts(console: Console, result: dict) -> int:
    """All cluster hosts -> a host Table; saved clusters / live status -> Panels."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster hosts", result)

    hosts = result.get("hosts") or []
    table = theme.make_table("cluster hosts")
    table.add_column("name", no_wrap=True)
    table.add_column("ip", no_wrap=True)
    table.add_column("role", no_wrap=True)
    for h in hosts:
        table.add_row(
            esc(h.get("name") or h.get("id") or "?"),
            esc(h.get("ip", "?")),
            esc(h.get("role", "?")),
        )
    if not hosts:
        table.add_row("[dim]no hosts recorded in cluster.json[/dim]", "", "")
    console.print(table)

    saved = result.get("saved_clusters") or {}
    live = result.get("live_status") or {}
    out_lines = []
    for label, src in (("saved sparkrun clusters", saved),
                       ("live sparkrun status", live)):
        body = _as_lines(src.get("output")) if isinstance(src, dict) else _as_lines(src)
        if not body:
            body = [esc(str(src))]
        out_lines.append(f"[label]{label}[/label]")
        for line in body:
            out_lines.append(f"  {line}")
    console.print(theme.make_panel("cluster hosts", "\n".join(out_lines)))
    return 0


def render_monitor(console: Console, result: dict) -> int:
    """One CPU/RAM/GPU snapshot -> a metrics Table from the parsed JSON line."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster monitor", result)

    data = result.get("json")
    if isinstance(data, dict) and isinstance(data.get("hosts"), list):
        # Same view as `--live`, one frame. Token rates need two samples, so
        # the snapshot shows load (running/waiting/KV) and '-' for tok/s.
        from hscc_daemon import cluster_live
        poller = cluster_live.ServingPoller()
        try:
            rows = poller.poll()
        finally:
            poller.close()
        console.print(cluster_live.render(data, rows, None, poller.proxy_error,
                                          snapshot=True, width=console.width,
                                          pauses=pause_overlay()))
        return 0
    if isinstance(data, dict):
        table = theme.make_table("cluster monitor")
        table.add_column("metric")
        table.add_column("value")
        for k, v in data.items():
            table.add_row(esc(k), esc(v))
        console.print(table)
        return 0

    # Fall back to the raw output line when there was no parseable JSON.
    body = result.get("output") or (result if not isinstance(result, dict)
                                    else json.dumps(result, indent=2, default=str))
    console.print(theme.make_panel("cluster monitor", esc(body)))
    return 0


def render_jobs(console: Console, result: dict) -> int:
    """All sparkrun jobs -> a status Panel wrapping the raw status output."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster jobs", result)

    output = result.get("output") if isinstance(result, dict) else None
    if output:
        console.print(theme.make_panel("cluster jobs", esc(output)))
    else:
        console.print(theme.make_status_panel(
            "no sparkrun job output", status="ok", title="cluster jobs"))
    return 0


def render_info(console: Console, result: dict) -> int:
    """Resolved cluster configuration -> grouped Panels (config files + clusters)."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster info", result)

    sections = []
    cluster_config = result.get("cluster_config")
    if isinstance(cluster_config, dict):
        sections.append("[label]cluster.json[/label]")
        for line in _as_lines(cluster_config):
            sections.append(f"  {line}")
        sections.append("")

    default = result.get("default_cluster") or {}
    if isinstance(default, dict) and default.get("output"):
        sections.append("[label]default cluster (sparkrun cluster show hscc)[/label]")
        for line in _as_lines(default["output"]):
            sections.append(f"  {line}")
        sections.append("")

    files = result.get("cluster_files") or {}
    if files:
        sections.append(f"[label]saved cluster files[/label]  "
                        f"{esc(', '.join(sorted(files)) or '(none)')}")

    if not sections:
        sections.append("[dim]no cluster configuration found[/dim]")
    console.print(theme.make_panel("cluster info", "\n".join(sections)))
    return 0


# ── cluster stop / down / up (fleet ops) ──────────────────────────────────

def _fleet_op_verdict(result: dict) -> tuple[bool, bool]:
    """(shown_ok, benign) for a fleet-op result dict — the ONE verdict rule here.

    t_c23c5aef (the twin half of t_1244b3b4's D6). ``hscc cluster down`` has two
    human renderers, and the standalone one
    (``hscc-cluster/hscc.py::_human_lines``) already says OK for a run whose
    nonzero sparkrun exit only meant "the fleet was not running"; this renderer
    still said ERROR because it read ``success`` alone. It now applies the SAME
    two-line rule, so the unified CLI and the standalone view cannot disagree:

        ok     = no ``error`` key and ``success``
        benign = (not ok) and the engine said so
        shown  = ok or benign

    ``benign`` reads the ADDITIVE ``benign_no_workloads`` key that
    ``cmd_cluster_down`` sets, and nothing else. **This module deliberately
    holds no marker table of its own** — the classification of sparkrun's
    output lives in exactly one place (``_benign_no_workloads_note`` in
    ``hscc-cluster/hscc.py``). A second copy here is how the two renderers start
    disagreeing about whether a fleet is down with no test able to see it,
    because each copy looks self-consistent (the same shape as ``2f90f711``'s
    kinds-vs-labels split). The verdict below is a pure function of the KEYS:
    it never re-reads ``output``/``error`` text, which is what
    ``tests/test_cluster_down_benign_render.py`` pins.

    What did NOT move:
      * ``success`` still means "sparkrun exited 0" — the ``--json`` machine
        contract (daemon, scripts, the iOS console dump that dict raw) is
        untouched; the verdict arrives only in the additive keys.
      * A genuine failure stays red. The engine sets the benign keys only when
        the run carried no real-failure signature and did match an already-down
        shape, so anything it did not classify — ssh/docker/auth/timeout, a host
        it ``could not query``, unknown wording — arrives WITHOUT the keys and
        still rules by the exit code. Absence of a benign key is never evidence
        of success, and a NON-EMPTY ``error`` key on a result the engine did not
        classify (a structural failure, e.g. the missing-``serving`` dict) still
        vetoes.

    One asymmetry is deliberate and measured: the unified view is allowed to be
    STRICTER than the standalone one, never softer. ``_human_lines`` has no
    ``error``-key veto at all, so ``{success: True, error: "…"}`` reads OK there
    and ERROR here. Pinned that way round in
    ``tests/test_cluster_down_benign_render.py`` — a red panel the operator
    chases is a cheaper mistake than a green one they trust.
    """
    # ``not err``, NOT ``err is None``. ``_run_or_print`` always sets ``error``
    # (the captured stderr, ``""`` when there was none) and ``_object_error``
    # hands that ``""`` back, so the old ``is None`` read treated a CLEAN stop as
    # a failure: measured at f2cbae3f, ``{success: True, returncode: 0,
    # error: ""}`` rendered a red panel and returned 1 out of the unified CLI —
    # the operator's "ERROR on a clean stop", second half. Nothing upstream can
    # rescue that shape either: ``_benign_no_workloads_note`` returns None for
    # any rc-0 run by design, so no benign key ever exists on a clean stop. The
    # veto belongs on a NON-EMPTY error string, which is the only thing in that
    # key that has ever meant "error".
    err = _object_error(result)
    ok = (not err) and bool(result.get("success", True))
    benign = (not ok) and bool(result.get("benign_no_workloads"))
    return (ok or benign), benign


def _render_fleet_op(console: Console, title: str, result: dict) -> int:
    """stop/down/up share a shape: a status Panel + the raw command output."""
    dry_run = bool(result.get("dry_run"))
    shown_ok, benign = _fleet_op_verdict(result)
    status = "ok" if shown_ok else "error"

    lines = []
    if result.get("command"):
        cmd = result["command"]
        cmd_str = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        lines.append(f"[label]command[/label]  {esc(cmd_str)}")
    if dry_run:
        lines.append("[dim]--dry-run: nothing executed[/dim]")
    if benign and result.get("benign_note"):
        # Same note the standalone view prints, dimmed under the OK verdict.
        lines.append(f"[dim]{esc(str(result['benign_note']))}[/dim]")
    for key in ("output", "error"):
        if result.get(key):
            lines.append(f"[label]{key}[/label]")
            for line in _as_lines(result[key]):
                lines.append(f"  {line}")
    result_lines = list(lines) or ["[dim]command completed[/dim]"]
    console.print(theme.make_status_panel("\n".join(result_lines), status=status,
                                          title=title))
    return 0 if shown_ok else 1


def render_stop(console: Console, result: dict) -> int:
    if _object_error(result) is not None:
        return _emit_error_panel(console, "cluster stop", result)
    ok = bool(result.get("success"))
    if not ok:
        status = "error"
        body = ""
        for key in ("output", "error"):
            if result.get(key):
                body += f"{key}: {esc(result[key])}\n"
        console.print(theme.make_status_panel(body.strip() or "stop failed",
                                              status="error", title="cluster stop"))
        return 1
    body = result.get("output") or "workload stopped"
    console.print(theme.make_status_panel(esc(body), status="ok",
                                          title="cluster stop"))
    return 0


def render_down(console: Console, result: dict) -> int:
    return _render_fleet_op(console, "cluster down", result)


def render_up(console: Console, result: dict) -> int:
    if _object_error(result) is not None:
        return _emit_error_panel(console, "cluster up", result)
    dry_run = bool(result.get("dry_run"))
    if dry_run:
        console.print(theme.make_status_panel(
            f"dry-run: {result.get('units', 0)} unit(s) would start",
            status="ok", title="cluster up"))
        return 0
    ok = bool(result.get("success"))
    status = "ok" if ok else "error"
    issued = result.get("issued") or []
    failed = [i for i in issued if not i.get("success")]
    lines = [f"[label]units[/label]  {result.get('units', len(issued))}"]
    if failed:
        lines.append(f"[label]failed[/label]  {len(failed)}")
    for i in failed:
        lines.append(f"  [error]{esc(i.get('error', i.get('output', '?') or '?'))}[/error]")
    if not lines:
        lines = ["[dim]no units to start[/dim]"]
    console.print(theme.make_status_panel("\n".join(lines), status=status,
                                          title="cluster up"))
    return 0 if ok else 1


# ── profiles ───────────────────────────────────────────────────────────────

def render_profiles(console: Console, result: dict) -> int:
    """Running kanban task counts per profile -> a profile Table."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "profiles", result)

    # cmd_profile_status returns {"counts": {name: count}, "total_running": N,
    # "profiles": [names]}. Tolerate the older {"profiles": [{name, running}],
    # ...} list-of-dicts shape and a bare {name: count} dict defensively.
    table = theme.make_table("profiles")
    table.add_column("profile", no_wrap=True)
    table.add_column("running", justify="right")

    counts = result.get("counts")
    profiles = result.get("profiles")

    if isinstance(counts, dict):
        for name, count in counts.items():
            table.add_row(esc(name), esc(count))
    elif isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict):
                table.add_row(esc(p.get("name", "?")),
                              esc(p.get("running", p.get("count", "?"))))
            else:
                table.add_row(esc(p), "?")
    elif isinstance(profiles, dict):
        for name, count in profiles.items():
            table.add_row(esc(name), esc(count))
    else:
        # fall back to any scalar dict entries
        for k, v in result.items():
            if k in ("profiles", "counts", "total_running", "success", "error"):
                continue
            if isinstance(v, (int, str)):
                table.add_row(esc(k), esc(v))

    if result.get("total_running") is not None:
        table.caption = f"{result['total_running']} running task(s)"
    if not table.rows:
        table.add_row("[dim]no profile data[/dim]", "")
    console.print(table)
    return 0


# ── template ───────────────────────────────────────────────────────────────

def render_template(console: Console, sub: str, result: dict) -> int:
    """Render a template subcommand result (list/status/preview/validate/apply)."""
    # A result carrying an ``error`` key is a failure for every subcommand.
    if _object_error(result) is not None:
        return _emit_error_panel(console, f"template {sub}", result)

    renderer = {
        "list": _render_template_list,
        "status": _render_template_status,
        "preview": _render_template_preview,
        "validate": _render_template_validate,
        "apply": _render_template_apply,
    }.get(sub, _render_template_generic)
    return renderer(console, result)


def _render_template_generic(console: Console, result: dict) -> int:
    """Unknown subcommand result — show it readably rather than a bare dict."""
    if isinstance(result, dict) and not result:
        console.print("[dim]no output[/dim]")
    else:
        console.print(theme.make_panel(
            "template", esc(json.dumps(result, indent=2, default=str))))
    return 0


def _render_template_list(console: Console, result: dict) -> int:
    table = theme.make_table("cluster templates")
    table.add_column("name", no_wrap=True)
    table.add_column("version", justify="right")
    table.add_column("group", no_wrap=True)
    table.add_column("description")
    for t in result.get("templates") or []:
        table.add_row(
            esc(t.get("name", "?")),
            esc(t.get("version", "?")),
            esc(t.get("group", "") or "-"),
            esc(t.get("description", "") or "-"),
        )
    if not table.rows:
        table.add_row("[dim]no templates found[/dim]", "", "", "")
    console.print(table)
    console.print(f"[label]{result.get('count', 0)} template(s)[/label]")
    return 0


def _render_template_status(console: Console, result: dict) -> int:
    applied = result.get("applied")
    note = result.get("note", "")
    if applied:
        body = f"applied template: [primary]{esc(applied)}[/primary]"
    else:
        body = "no template applied"
    if note:
        body = f"{body}\n[dim]{esc(note)}[/dim]"
    console.print(theme.make_status_panel(body, status="ok",
                                          title="template status"))
    return 0


def _append_labeled_list(lines: list[str], label: str, values: list) -> None:
    if not values:
        values = ["(none)"]
    lines.append(f"[label]{label}[/label]")
    for v in values:
        lines.append(f"  {esc(v)}")


def _render_template_preview(console: Console, result: dict) -> int:
    title = f"template preview: {result.get('template', '')}"
    lines = []
    if result.get("description"):
        lines.append(f"[dim]{esc(result['description'])}[/dim]")
    changes = result.get("changes") or []
    lines.append(f"[label]changes[/label]  {len(changes)}")
    for c in changes:
        file = c.get("file", "?")
        summary = c.get("summary", "")
        lines.append(f"  [accent]{esc(file)}[/accent]  {esc(summary)}")
        for d in c.get("details") or []:
            lines.append(f"      {esc(d)}")
    serve_delta = result.get("serve_delta")
    if isinstance(serve_delta, dict):
        lines.append("")
        lines.append("[label]serve delta[/label]")
        for k, v in serve_delta.items():
            if isinstance(v, list) and v:
                lines.append(f"  [accent]{esc(k)}[/accent]  {len(v)}")
                for item in v:
                    lines.append(f"      {esc(item)}")
            elif not isinstance(v, list):
                lines.append(f"  [accent]{esc(k)}[/accent]  {esc(v)}")
    routing = result.get("routing")
    if routing:
        lines.append("")
        lines.append(f"[label]routing[/label]  {len(routing)}")
        for r in routing:
            lines.append(f"  {esc(r)}")
    if not lines:
        lines.append("[dim](no changes)[/dim]")
    console.print(theme.make_panel(title, "\n".join(lines)))
    return 0


def _render_template_validate(console: Console, result: dict) -> int:
    ok = bool(result.get("ok"))
    title = f"template validate: {result.get('template', '')}"
    lines = []
    for layer in ("structural", "placement"):
        lr = result.get(layer) or {}
        label = layer.capitalize()
        if lr.get("skipped"):
            lines.append(f"[dim]{label}: skipped (structural-only)[/dim]")
            continue
        status_colour = "ok" if lr.get("ok") else "error"
        glyph = "\N{CHECK MARK}" if lr.get("ok") else "\N{BALLOT X}"
        lines.append(f"[{status_colour}]{glyph}[/] [label]{label}[/] "
                     f"({'PASS' if lr.get('ok') else 'FAIL'})")
        _append_labeled_list(lines, "errors", lr.get("errors") or [])
        _append_labeled_list(lines, "warnings", lr.get("warnings") or [])
    if not lines:
        lines.append("[dim]no validation data[/dim]")
    console.print(theme.make_status_panel(
        "\n".join(lines), status="ok" if ok else "error", title=title))
    return 0 if ok else 1


def _render_template_apply(console: Console, result: dict) -> int:
    status = result.get("status", "preview")
    success = result.get("success", False)
    if not success:
        err = esc(result.get("note") or "apply failed")
        if result.get("errors"):
            err += "\n" + "\n".join(f"  {esc(e)}" for e in result["errors"])
        if result.get("validation") and result["validation"].get("ok") is False:
            err += "\n  template not deployable (see: hscc template validate)"
        console.print(theme.make_status_panel(err, status="error",
                                              title="template apply"))
        return 1
    lines = []
    if result.get("note"):
        lines.append(esc(result["note"]))
    steps = result.get("steps") or []
    if steps:
        lines.append(f"[label]steps[/label]  {len(steps)}")
        for s in steps:
            s_ok = s.get("status", "ok") != "error"
            glyph = "\N{CHECK MARK}" if s_ok else "\N{BALLOT X}"
            lines.append(f"  [{('ok' if s_ok else 'error')}]{glyph}[/] "
                         f"{esc(s.get('step', '?'))}")
    else:
        lines.append("[dim]no steps recorded[/dim]")
    console.print(theme.make_status_panel(
        "\n".join(lines), status="ok", title="template apply"))
    return 0
