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
from typing import Any

from rich.console import Console

from hscc_daemon import cli_theme as theme


# ── shared helpers ─────────────────────────────────────────────────────────

def _as_lines(value: Any) -> list[str]:
    """Collapse a scalar/blob into a list of display lines (never empty)."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split("\n")
    if isinstance(value, (dict, list)):
        return [json.dumps(value, indent=2, default=str)]
    return [str(value)]


def _object_error(result: dict) -> str | None:
    """Return the user-facing error string, or None if the result is a success."""
    return result.get("error") if isinstance(result, dict) else None


def _emit_error_panel(console: Console, title: str, result: dict) -> int:
    """Render a uniform red error panel; exit code is always 1."""
    msg = result.get("error", "command failed")
    usage = result.get("usage")
    body = str(msg)
    if usage:
        body = f"{body}\n\nusage: {usage}"
    console.print(theme.make_status_panel(body, status="error", title=title))
    return 1


# ── cluster ────────────────────────────────────────────────────────────────

def render_cluster_status(console: Console, result: dict) -> int:
    """Running workloads + idle hosts -> a workload Table + a summary Panel."""
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
        table.add_row(
            str(w.get("name", "?")),
            str(w.get("tp", "?")),
            str(w.get("pp", "?")),
            str(w.get("container_id", "?")),
        )
    if not workloads:
        table.add_row("[dim]no running workloads[/dim]", "", "", "")
    console.print(table)

    idle = result.get("idle_hosts") or []
    total = result.get("total_hosts", 0)
    lines = [f"[label]total hosts[/label]  {total}",
             f"[label]idle hosts[/label]   {len(idle)}"]
    if idle:
        lines.append("[dim]" + ", ".join(str(h) for h in idle) + "[/dim]")
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
            str(h.get("name") or h.get("id") or "?"),
            str(h.get("ip", "?")),
            str(h.get("role", "?")),
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
            body = [str(src)]
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
    if isinstance(data, dict):
        table = theme.make_table("cluster monitor")
        table.add_column("metric")
        table.add_column("value")
        for k, v in data.items():
            table.add_row(str(k), str(v))
        console.print(table)
        return 0

    # Fall back to the raw output line when there was no parseable JSON.
    body = result.get("output") or (result if not isinstance(result, dict)
                                    else json.dumps(result, indent=2, default=str))
    console.print(theme.make_panel("cluster monitor", str(body)))
    return 0


def render_jobs(console: Console, result: dict) -> int:
    """All sparkrun jobs -> a status Panel wrapping the raw status output."""
    err = _object_error(result)
    if err is not None:
        return _emit_error_panel(console, "cluster jobs", result)

    output = result.get("output") if isinstance(result, dict) else None
    if output:
        console.print(theme.make_panel("cluster jobs", str(output)))
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
                        f"{', '.join(sorted(files)) or '(none)'}")

    if not sections:
        sections.append("[dim]no cluster configuration found[/dim]")
    console.print(theme.make_panel("cluster info", "\n".join(sections)))
    return 0


# ── cluster stop / down / up (fleet ops) ──────────────────────────────────

def _render_fleet_op(console: Console, title: str, result: dict) -> int:
    """stop/down/up share a shape: a status Panel + the raw command output."""
    dry_run = bool(result.get("dry_run"))
    ok = _object_error(result) is None and result.get("success", True)
    status = "ok" if ok else "error"

    lines = []
    if result.get("command"):
        cmd = result["command"]
        lines.append(f"[label]command[/label]  "
                     f"{cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}")
    if dry_run:
        lines.append("[dim]--dry-run: nothing executed[/dim]")
    for key in ("output", "error"):
        if result.get(key):
            lines.append(f"[label]{key}[/label]")
            for line in _as_lines(result[key]):
                lines.append(f"  {line}")
    result_lines = list(lines) or ["[dim]command completed[/dim]"]
    console.print(theme.make_status_panel("\n".join(result_lines), status=status,
                                          title=title))
    return 0 if ok else 1


def render_stop(console: Console, result: dict) -> int:
    if _object_error(result) is not None:
        return _emit_error_panel(console, "cluster stop", result)
    ok = bool(result.get("success"))
    if not ok:
        status = "error"
        body = ""
        for key in ("output", "error"):
            if result.get(key):
                body += f"{key}: {result[key]}\n"
        console.print(theme.make_status_panel(body.strip() or "stop failed",
                                              status="error", title="cluster stop"))
        return 1
    body = result.get("output") or "workload stopped"
    console.print(theme.make_status_panel(str(body), status="ok",
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
        lines.append(f"  [error]{i.get('error', i.get('output', '?') or '?')}[/error]")
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
            table.add_row(str(name), str(count))
    elif isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict):
                table.add_row(str(p.get("name", "?")),
                              str(p.get("running", p.get("count", "?"))))
            else:
                table.add_row(str(p), "?")
    elif isinstance(profiles, dict):
        for name, count in profiles.items():
            table.add_row(str(name), str(count))
    else:
        # fall back to any scalar dict entries
        for k, v in result.items():
            if k in ("profiles", "counts", "total_running", "success", "error"):
                continue
            if isinstance(v, (int, str)):
                table.add_row(str(k), str(v))

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
            "template", json.dumps(result, indent=2, default=str)))
    return 0


def _render_template_list(console: Console, result: dict) -> int:
    table = theme.make_table("cluster templates")
    table.add_column("name", no_wrap=True)
    table.add_column("version", justify="right")
    table.add_column("group", no_wrap=True)
    table.add_column("description")
    for t in result.get("templates") or []:
        table.add_row(
            str(t.get("name", "?")),
            str(t.get("version", "?")),
            str(t.get("group", "") or "-"),
            str(t.get("description", "") or "-"),
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
        body = f"applied template: [primary]{applied}[/primary]"
    else:
        body = "no template applied"
    if note:
        body = f"{body}\n[dim]{note}[/dim]"
    console.print(theme.make_status_panel(body, status="ok",
                                          title="template status"))
    return 0


def _append_labeled_list(lines: list[str], label: str, values: list) -> None:
    if not values:
        values = ["(none)"]
    lines.append(f"[label]{label}[/label]")
    for v in values:
        lines.append(f"  {v}")


def _render_template_preview(console: Console, result: dict) -> int:
    title = f"template preview: {result.get('template', '')}"
    lines = []
    if result.get("description"):
        lines.append(f"[dim]{result['description']}[/dim]")
    changes = result.get("changes") or []
    lines.append(f"[label]changes[/label]  {len(changes)}")
    for c in changes:
        file = c.get("file", "?")
        summary = c.get("summary", "")
        lines.append(f"  [accent]{file}[/accent]  {summary}")
        for d in c.get("details") or []:
            lines.append(f"      {d}")
    serve_delta = result.get("serve_delta")
    if isinstance(serve_delta, dict):
        lines.append("")
        lines.append("[label]serve delta[/label]")
        for k, v in serve_delta.items():
            if isinstance(v, list) and v:
                lines.append(f"  [accent]{k}[/accent]  {len(v)}")
                for item in v:
                    lines.append(f"      {item}")
            elif not isinstance(v, list):
                lines.append(f"  [accent]{k}[/accent]  {v}")
    routing = result.get("routing")
    if routing:
        lines.append("")
        lines.append(f"[label]routing[/label]  {len(routing)}")
        for r in routing:
            lines.append(f"  {r}")
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
        err = result.get("note") or "apply failed"
        if result.get("errors"):
            err += "\n" + "\n".join(f"  {e}" for e in result["errors"])
        if result.get("validation") and result["validation"].get("ok") is False:
            err += "\n  template not deployable (see: hscc template validate)"
        console.print(theme.make_status_panel(err, status="error",
                                              title="template apply"))
        return 1
    lines = []
    if result.get("note"):
        lines.append(result["note"])
    steps = result.get("steps") or []
    if steps:
        lines.append(f"[label]steps[/label]  {len(steps)}")
        for s in steps:
            s_ok = s.get("status", "ok") != "error"
            glyph = "\N{CHECK MARK}" if s_ok else "\N{BALLOT X}"
            lines.append(f"  [{('ok' if s_ok else 'error')}]{glyph}[/] "
                         f"{s.get('step', '?')}")
    else:
        lines.append("[dim]no steps recorded[/dim]")
    console.print(theme.make_status_panel(
        "\n".join(lines), status="ok", title="template apply"))
    return 0
