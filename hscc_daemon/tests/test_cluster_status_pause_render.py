"""t_480cee2d — the fleet view must say ``paused (reason, since <t>)`` for a node
the operator parked, not silently fold it into ``idle hosts``.

WHY THIS SCREEN. ``hscc node pause`` stops a node's units through sparkrun and
records the intent in ``~/.hscc/node_pauses.json`` (``node_pauses.py``, #560).
``hscc cluster status`` then parses sparkrun's own status output, and sparkrun
has no idea a human is involved: the parked machine lands in the ``idle hosts``
list with no reason attached, exactly where a machine nobody has put work on
sits. The repair loops already honour the overlay (watchdog auto-heal #565,
keepalive, ``cluster up``, autoscale #564, the heal breaker #568, autodown #566)
and the API edge now projects it too (t_824b617f / #569, whose row contract is
``status``/``paused_reason``/``paused_since``); the human fleet view was the last
surface that still rendered a deliberate act as an unexplained gap.

WHAT IS PINNED, and the two directions that matter:

  * a parked node reads ``paused (reason, since <iso>)`` — the exact shape the
    card specifies, contiguous and greppable, so an operator (or a grep in a
    runbook) finds it;
  * a node the frame never listed at all still gets that line, because
    sparkrun does not report a machine whose workloads are stopped — the
    ABSENT row is the one an operator must not conclude is healthy;
  * a span sibling is parked too (#560's ``covering`` rule), and its own
    record wins over a span record that happens to name it;
  * the block is ADDITIVE: with an empty overlay the pre-existing lines are
    byte-identical, so this cannot move the wording the other pins on this
    screen depend on;
  * the overlay read must never be able to take the screen down, and must not
    happen at all on the error path.

Doctrine: the overlay is read through its documented ``directory=`` seam — an
explicit root, never an environment variable, because an env root is per-process
and is therefore not a root the CLI that WROTE the pause can ever have had (the
root-split class, t_adb66608). Hosts are TEST-NET-1 documentation addresses
(192.0.2.0/24) and every overlay in here lives in a tmp dir: no live
``~/.hscc``, no sparkrun, no ssh, nothing against the real cluster.
"""

import io
import os
import time

import pytest

from hscc_daemon import cli_theme, cluster_live, cluster_render, node_pauses

# ── fixtures and helpers ───────────────────────────────────────────────────

PARKED = "192.0.2.10"        # the keyed host — the machine `hscc node pause` named
SIBLING = "192.0.2.11"       # named only in span_nodes: a tp pair stops as one
ABSENT = "192.0.2.13"        # parked, and absent from sparkrun's frame entirely
LIVE = "192.0.2.12"          # an ordinary idle host, not parked
SINCE = "2026-11-21T18:04:07+00:00"


def _status_result(idle=(LIVE,), total=4):
    """The dict ``cmd_cluster_status`` hands the renderer (engine's own keys)."""
    return {"workloads": [{"name": "@official/model-a", "tp": "1", "pp": "1",
                           "container_id": "1b6e7719", "nodes": [LIVE]}],
            "idle_hosts": list(idle), "total_hosts": total}


def _overlay(reason="training run", since=SINCE):
    """The renderer's own overlay shape, injected — the pure path."""
    return {PARKED: {"reason": reason, "since": since}}


def _render(result, **kwargs):
    """Rendered panel text, wide so nothing wraps (a wrap would test columns)."""
    buf = io.StringIO()
    rc = cluster_render.render_cluster_status(
        cli_theme.make_console(file=buf, width=200), result, **kwargs)
    return buf.getvalue(), rc


# ── the deliverable: the paused wording, and where it appears ─────────────

def test_parked_node_reads_paused_with_reason_and_since():
    text, rc = _render(_status_result(idle=(LIVE, PARKED)),
                       pauses=_overlay())
    assert rc == 0
    # The exact contract shape, contiguous: a runbook can grep one string.
    assert f"paused ({'training run'}, since {SINCE})" in text
    assert PARKED in text
    # And it is NOT presented as a fault on this screen.
    assert "down" not in text and "offline" not in text


def test_parked_node_line_names_the_node_it_parks():
    text, _ = _render(_status_result(), pauses=_overlay(reason="disk swap"))
    line = [ln for ln in text.splitlines() if PARKED in ln and "paused" in ln]
    assert len(line) == 1, text
    assert "paused (disk swap, since " in line[0]


def test_node_absent_from_the_frame_still_gets_the_line():
    """The ABSENT row is the dangerous one: sparkrun omits a stopped machine.

    A renderer that only annotated rows it was handed would report nothing at
    all for the commonest pause, and the operator would read "3 hosts, 3 idle"
    as a healthy fleet.
    """
    text, _ = _render(_status_result(idle=(LIVE,)), pauses=_overlay())
    assert ABSENT not in text          # (sanity: this result does not list it)
    text, _ = _render(_status_result(idle=(LIVE,)),
                      pauses={ABSENT: {"reason": "disk swap", "since": SINCE}})
    assert f"{ABSENT}  paused (disk swap, since {SINCE})" in text


def test_span_sibling_is_parked_with_the_same_reason():
    """``covering``'s rule (#560): a tp pair is parked as one thing."""
    pauses = cluster_render.pause_overlay()  # empty by default (sealed root)
    assert pauses == {}
    pauses = {PARKED: {"reason": "training run", "since": SINCE},
              SIBLING: {"reason": "training run", "since": SINCE}}
    text, _ = _render(_status_result(idle=(LIVE,)), pauses=pauses)
    for node in (PARKED, SIBLING):
        assert f"{node}  paused (training run, since {SINCE})" in text


def test_own_record_wins_over_a_span_that_names_the_same_host():
    """The operator's own reason for a host beats a span record naming it."""
    text, _ = _render(_status_result(), pauses={
        PARKED: {"reason": "my own reason", "since": SINCE},
        SIBLING: {"reason": "spanned by .10", "since": SINCE},
    })
    assert "paused (my own reason, since" in text
    assert "paused (spanned by .10, since" in text


def test_pause_block_is_additive_and_counted():
    text, _ = _render(_status_result(idle=(LIVE, PARKED)),
                      pauses={PARKED: {"reason": "r", "since": SINCE},
                              ABSENT: {"reason": "", "since": None}})
    assert "node pauses  2" in text
    # sparkrun's own lines are untouched by the block.
    assert "idle hosts   2" in text
    assert "total hosts  4" in text


# ── the additive invariant: an unparked fleet renders byte-identical ──────

def test_empty_overlay_renders_byte_identical(tmp_path, monkeypatch):
    """No pause recorded ⇒ every byte this screen printed before exists."""
    called = []
    monkeypatch.setattr(cluster_render, "_backing_node_pauses",
                        lambda directory=None: called.append(directory) or {})
    baseline, _ = _render(_status_result(), pauses={})
    live, _ = _render(_status_result())        # production path: reads overlay
    assert live == baseline
    assert called == [None]                    # read once, default root


def test_overlay_with_no_entries_is_the_same_as_none():
    assert _render(_status_result(), pauses={})[0] == _render(
        _status_result(), pauses=None)[0]


# ── the formatter: it degrades, it never invents ──────────────────────────

@pytest.mark.parametrize("reason,since,expected", [
    ("training run", SINCE, f"paused (training run, since {SINCE})"),
    ("", SINCE, f"paused (since {SINCE})"),
    ("training run", None, "paused (training run)"),
    ("", None, "paused"),
    (None, None, "paused"),
    ("   ", "   ", "paused"),
    # A microsecond stamp is the writer's precision, not the reader's need.
    ("r", "2026-11-21T18:04:07.123456+00:00",
     "paused (r, since 2026-11-21T18:04:07+00:00)"),
])
def test_pause_display_degrades_rather_than_inventing(reason, since, expected):
    assert cluster_render.pause_display(reason, since) == expected


def test_pause_display_never_rewrites_the_timestamp():
    """No clock here, so no local-time conversion — the offset rides through."""
    out = cluster_render.pause_display("r", "2026-03-01T02:00:00+09:00")
    assert out == "paused (r, since 2026-03-01T02:00:00+09:00)"


# ── the reader: the directory seam, and failing open ──────────────────────

def test_overlay_projects_a_pause_the_writer_wrote(tmp_path):
    """End-to-end through the real overlay: writer = node_pauses, reader = here.

    This is why the seam is ``directory=`` and not an env var: the write and the
    read must be able to agree on a root the way two processes do.
    """
    node_pauses.pause(PARKED, reason="training run", units=["u1"],
                      span_nodes=[PARKED, SIBLING], stopped=True,
                      directory=str(tmp_path), now="2026-11-21T18:04:07+00:00")
    overlay = cluster_render.pause_overlay(str(tmp_path))
    assert overlay[PARKED] == {"reason": "training run", "since": SINCE}
    assert overlay[SIBLING] == {"reason": "training run", "since": SINCE}
    text, _ = _render(_status_result(idle=(LIVE,)), pauses=overlay)
    assert f"paused (training run, since {SINCE})" in text


def test_overlay_is_read_from_the_root_the_caller_names(tmp_path):
    """A different root is a different answer — never "the default root".

    The other half of the root-split rule: a reader that ignored ``directory``
    (or fell back to HOME's ``~/.hscc``) would report the operator's live
    pauses to a caller that asked about its own tree.
    """
    other = tmp_path / "other"
    other.mkdir()
    node_pauses.pause(PARKED, reason="in the named root",
                      directory=str(tmp_path), now="2026-11-21T18:04:07+00:00")
    assert cluster_render.pause_overlay(str(other)) == {}
    assert PARKED in cluster_render.pause_overlay(str(tmp_path))


def test_default_root_is_the_overlay_module_own_constant(tmp_path, monkeypatch):
    """The production path resolves through node_pauses, not a private copy."""
    node_pauses.pause(PARKED, reason="via sealed root", directory=str(tmp_path))
    monkeypatch.setattr(node_pauses, "HSCC_DIR", str(tmp_path))
    assert PARKED in cluster_render.pause_overlay()
    assert cluster_render.pause_overlay(None) == cluster_render.pause_overlay()


def test_overlay_reader_is_the_monkeypatch_seam(tmp_path, monkeypatch):
    """A test may patch the ONE reader; nothing reaches into node_pauses itself."""
    node_pauses.pause(PARKED, reason="through the seam",
                      directory=str(tmp_path), now="2026-11-21T18:04:07+00:00")
    seen = []

    def fake(directory=None):
        seen.append(directory)
        return node_pauses.list_pauses(str(tmp_path))

    monkeypatch.setattr(cluster_render, "_backing_node_pauses", fake)
    assert PARKED in cluster_render.pause_overlay()
    assert seen == [None]


def test_unreadable_overlay_fails_open_and_the_screen_still_renders(monkeypatch):
    def boom(directory=None):
        raise RuntimeError("overlay unreadable")

    monkeypatch.setattr(cluster_render, "_backing_node_pauses", boom)
    assert cluster_render.pause_overlay() == {}
    text, rc = _render(_status_result())
    assert rc == 0
    assert "paused" not in text
    assert "idle hosts" in text          # the screen itself still renders


@pytest.mark.parametrize("raw", [
    None, [], {"pauses": "not a dict"}, {"192.0.2.10": "not an entry"},
    {PARKED: {"reason": 7, "since": 42}},           # wrong types, right shape
    {"": {"reason": "r", "since": SINCE}},          # blank host key names nothing
    {PARKED: {"reason": "r", "since": SINCE, "span_nodes": "not-a-list"}},
])
def test_junk_in_the_overlay_never_reaches_the_screen(raw, monkeypatch):
    monkeypatch.setattr(cluster_render, "_backing_node_pauses",
                        lambda directory=None: raw)
    overlay = cluster_render.pause_overlay()
    assert isinstance(overlay, dict)
    for node, fields in overlay.items():
        assert isinstance(node, str) and node
        assert set(fields) == {"reason", "since"}
        assert isinstance(fields["reason"], str)
        assert fields["since"] is None or isinstance(fields["since"], str)
    text, rc = _render(_status_result())
    assert rc == 0 and "Traceback" not in text


def test_span_projection_does_not_let_a_span_overwrite_a_real_record(monkeypatch):
    """The keyed record is assigned, the span only fills a gap (``setdefault``).

    Both directions matter: a span that overwrote the operator's own reason
    would misreport why a machine is parked, and a keyed record that never got
    written would drop the pause entirely.
    """
    raw = {SIBLING: {"reason": "span", "since": SINCE,
                     "span_nodes": [SIBLING, PARKED]},
           PARKED: {"reason": "own", "since": SINCE}}
    monkeypatch.setattr(cluster_render, "_backing_node_pauses",
                        lambda directory=None: raw)
    overlay = cluster_render.pause_overlay()
    assert overlay[PARKED]["reason"] == "own"
    assert overlay[SIBLING]["reason"] == "span"
    # And the span-only case: the sibling of a parked span IS in the overlay.
    raw = {PARKED: {"reason": "span", "since": SINCE,
                    "span_nodes": [PARKED, SIBLING]}}
    monkeypatch.setattr(cluster_render, "_backing_node_pauses",
                        lambda directory=None: raw)
    assert cluster_render.pause_overlay()[SIBLING]["reason"] == "span"


# ── markup safety: the reason is operator prose, i.e. DATA ────────────────

def test_markup_in_the_reason_cannot_break_the_view():
    """A reason like ``[bold]disk[/bold] swap`` renders literally.

    This screen escapes sparkrun output already; the pause reason joins that
    class the moment an operator is allowed to type it.
    """
    text, rc = _render(_status_result(),
                       pauses={PARKED: {"reason": "[bold]disk[/bold] swap",
                                        "since": SINCE}})
    assert rc == 0
    assert "[bold]disk[/bold] swap" in text


def test_markup_in_a_host_key_cannot_break_the_view():
    text, rc = _render(_status_result(),
                       pauses={"[dim]192.0.2.99[/dim]": {"reason": "r",
                                                         "since": SINCE}})
    assert rc == 0
    assert "[dim]192.0.2.99[/dim]" in text


# ── the serving grid: the same word the API's monitor payload carries ──────
#
# `hscc cluster monitor` is the CLI twin of the API's monitor payload, and
# t_824b617f taught that payload to say `status: "paused"` for a parked node.
# The grid still spelled the same machine `down`, which is the split this seam
# exists to close: two surfaces, one machine, two verdicts.

PARKED_PORT = "8000"


def _down_row(node=PARKED, error="HTTP Error 111: Connection refused"):
    """One ServingPoller row for an unreachable backend — exactly its shape."""
    return {"node": node, "port": PARKED_PORT, "routes": ["orchestrator-model"],
            "model": "-", "error": error}


def _live_row(node=LIVE, model="@official/model-a"):
    return {"node": node, "port": "8001", "routes": [model], "model": model,
            "running": 2, "waiting": 0, "kv": 0.1, "gen_tps": 31.0,
            "prompt_tps": 12.0, "prompt_total": 100, "gen_total": 200}


def _grid(rows, width=200, **kwargs):
    buf = io.StringIO()
    cli_theme.make_console(file=buf, width=width).print(
        cluster_live.serving_table(rows, width, **kwargs))
    return buf.getvalue()


def _view(rows, width=200, **kwargs):
    buf = io.StringIO()
    cli_theme.make_console(file=buf, width=width).print(
        cluster_live.render({"hosts": []}, rows, 5, None, None,
                            snapshot=True, width=width, **kwargs))
    return buf.getvalue()


def test_parked_backend_reads_paused_instead_of_down():
    out = _grid([_down_row()], pauses={PARKED: {"reason": "training run",
                                                "since": SINCE}})
    assert "down" not in out
    # The Model column caps at 26 characters, so the CELL shows the front of the
    # string with the grid's own ellipsis — which is the whole reason `render`
    # also prints the block below (see test_pause_block_carries_the_full_string).
    assert "paused" in out and "training run" in out
    assert PARKED in out


def test_pause_block_carries_the_full_string():
    """The contract string survives whole on any terminal that can hold it."""
    out = _view([_down_row()], width=200,
                pauses={PARKED: {"reason": "training run", "since": SINCE}})
    assert f"paused (training run, since {SINCE})" in out
    assert "node pauses" in out
    assert "down" not in out


def test_an_unparked_unreachable_backend_is_still_down():
    """The polarity: `paused` is an overlay fact, never a guess from silence."""
    out = _grid([_down_row()], pauses={ABSENT: {"reason": "r", "since": SINCE}})
    assert "down" in out
    assert "paused" not in out


def test_serving_table_without_the_overlay_renders_the_old_view():
    """`pauses=None` is not "go read the disk" — the golden stays hermetic.

    `ios-app/scripts/cluster_monitor_cli_parity.py` feeds committed fixtures
    through this function and the Swift view-model is pinned to the output, so a
    default that consulted `~/.hscc` would make a committed golden depend on
    whether whoever regenerated it had parked a node.
    """
    with_overlay = _grid([_down_row()], pauses={})
    default = _grid([_down_row()])
    assert default == with_overlay
    assert "down" in default and "paused" not in default


def test_parked_row_keeps_its_error_text():
    """"Parked" and "could not sample" are both true; one does not delete the other."""
    out = _grid([_down_row(error="timed out")],
                pauses={PARKED: {"reason": "r", "since": SINCE}})
    assert "timed out" in out


def test_pause_block_wraps_like_any_other_line_when_the_terminal_is_narrow():
    """At 70 columns the line does not fit, and Rich folds it — measured, not hoped.

    Pinned so nobody "fixes" it with ``no_wrap=True``: that would TRADE a fold for
    a truncation, and losing the ``since`` stamp is the worse of the two errors.
    Nothing is invented or dropped by the fold — the halves are all there.
    """
    out = _view([_down_row()], width=70,
                pauses={PARKED: {"reason": "training run on the long one",
                                 "since": SINCE}})
    assert "node pauses" in out
    assert "paused (training run on the long one, since" in out
    assert SINCE in out
    assert f"paused (training run on the long one, since {SINCE})" not in out  # folded


def test_pause_block_survives_a_cell_that_ellipsises():
    """The Model cell clips at its 26-char cap; the block below does not."""
    out = _view([_down_row()], width=140,
                pauses={PARKED: {"reason": "training run on the long one",
                                 "since": SINCE}})
    assert f"paused (training run on the long one, since {SINCE})" in out


def test_render_without_the_overlay_grows_no_block():
    assert "node pauses" not in _view([_down_row()])
    assert "node pauses" not in _view([_down_row()], pauses={})


def test_monitor_snapshot_caller_actually_passes_the_overlay(monkeypatch):
    """The wiring, not just the parameter: a forgotten argument is the bug.

    Every live surface must hand the grid an overlay, or the operator sees
    `down` for a parked node and no test below the function boundary notices.
    """
    class FakePoller:
        proxy_error = None

        def poll(self):
            return [_down_row()]

        def close(self):
            pass

    monkeypatch.setattr(cluster_live, "ServingPoller", FakePoller)
    monkeypatch.setattr(cluster_render, "pause_overlay",
                        lambda directory=None: {PARKED: {"reason": "training run",
                                                         "since": SINCE}})
    buf = io.StringIO()
    rc = cluster_render.render_monitor(
        cli_theme.make_console(file=buf, width=200),
        {"json": {"hosts": [{"host": LIVE, "sample": {}}]}})
    assert rc == 0
    assert f"paused (training run, since {SINCE})" in buf.getvalue()
    assert "down" not in buf.getvalue()


def test_live_surface_reads_the_overlay_per_frame(monkeypatch):
    """`run_live` must read it EACH frame, so a resume shows up on the next one.

    Two frames, deliberately: a hoisted read (once before the loop) also produces
    exactly one call for one frame, and the bug worth catching is a monitor that
    keeps saying `paused` for a node the operator already resumed.
    """
    calls = []

    def counting_overlay(directory=None):
        calls.append(directory)
        return {}

    monkeypatch.setattr(cluster_render, "pause_overlay", counting_overlay)

    class FakeStream:
        frame = {"hosts": []}
        error = None

        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    class FakePoller:
        proxy_error = None
        polls = 0

        def poll(self):
            FakePoller.polls += 1
            if FakePoller.polls > 2:
                raise KeyboardInterrupt()
            return []

        def close(self):
            pass

    monkeypatch.setattr(cluster_live, "HostStream", FakeStream)
    monkeypatch.setattr(cluster_live, "ServingPoller", FakePoller)
    monkeypatch.setattr(cluster_live, "time",
                        type("FakeClock", (), {"sleep": staticmethod(lambda s: None),
                                               "monotonic": time.monotonic}))
    buf = io.StringIO()
    assert cluster_live.run_live(cli_theme.make_console(file=buf, width=120),
                                 interval=5) == 0
    assert calls == [None, None]   # one read per drawn frame, never one per run


# ── the identity rule lives once ──────────────────────────────────────────

@pytest.mark.parametrize("node", [
    "192.0.2.10", "  192.0.2.10  ", "worker-a", "  WORKER-a ", "WORKER-A",
])
def test_pause_for_node_compares_trimmed_and_case_folded(node):
    """One rule for both spellings of a fleet name: trim, case-fold, no guessing."""
    overlay = {"192.0.2.10": {"reason": "r", "since": None},
               "worker-a": {"reason": "r", "since": None}}
    assert cluster_render.pause_for_node(overlay, node) == "paused (r)"


def test_pause_for_node_is_none_for_everything_that_is_not_a_parked_node():
    overlay = {PARKED: {"reason": "r", "since": None}}
    assert cluster_render.pause_for_node({}, PARKED) is None
    assert cluster_render.pause_for_node(overlay, None) is None
    assert cluster_render.pause_for_node(overlay, "192.0.2.99") is None
    assert cluster_render.pause_for_node(overlay, "") is None
    assert cluster_render.pause_for_node({PARKED: "junk"}, PARKED) is None


def test_a_answering_backend_is_rendered_exactly_as_before():
    """Parity for the healthy case: the overlay changes nothing it does not hold."""
    pauses = {ABSENT: {"reason": "r", "since": SINCE}}
    assert _grid([_live_row()], pauses=pauses) == _grid([_live_row()])
    assert "paused" not in _grid([_live_row()], pauses=pauses)


# ── the error path must not touch the disk ────────────────────────────────

def test_error_result_reads_no_overlay_and_keeps_its_exit_code(monkeypatch):
    def boom(directory=None):
        raise AssertionError("the error panel must not read the overlay")

    monkeypatch.setattr(cluster_render, "_backing_node_pauses", boom)
    buf = io.StringIO()
    rc = cluster_render.render_cluster_status(
        cli_theme.make_console(file=buf, width=200),
        {"error": "sparkrun not found"})
    assert rc == 1
    assert "sparkrun not found" in buf.getvalue()


# ── one word, two surfaces ────────────────────────────────────────────────

def test_the_api_edge_and_the_cli_say_the_same_word():
    """``hscc-api/routes_cluster.py`` owns the machine value; this the display.

    ``hscc_daemon`` cannot import ``hscc-api`` (that edge would be a cycle, and
    the hyphenated dir is not a package name), so the literal lives twice and
    is pinned from source text instead. It SKIPS until #569 lands — and starts
    biting the moment it does, which is the point.
    """
    src = os.path.join(os.path.dirname(cluster_render.__file__), os.pardir,
                       "hscc-api", "routes_cluster.py")
    if not os.path.exists(src):
        pytest.skip(f"no API route module at {src}")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    marker = 'PAUSE_STATUS = "'
    at = body.find(marker)
    if at < 0:
        pytest.skip("the API edge has no PAUSE_STATUS yet (lands with #569)")
    literal = body[at + len(marker):].split('"', 1)[0]
    assert literal == cluster_render.PAUSE_STATUS


def test_the_render_module_grows_no_overlay_reader_of_its_own():
    """This seam must not grow its own file reader or its own JSON parse.

    A second reader is how the CLI and the daemon start disagreeing about what
    is parked — the split the overlay was written to close (#560). The render
    module holds a PROJECTION: it calls the overlay's reader and formats.
    """
    import inspect

    src = inspect.getsource(cluster_render)
    for forbidden in ("OVERLAY_NAME", "overlay_path", "node_pauses.save",
                      "node_pauses.pause(", "open("):
        assert forbidden not in src, f"cluster_render must not do {forbidden!r}"
    # The one and only way in: the overlay's own reader, called through the seam.
    assert src.count("node_pauses.list_pauses(") == 1
