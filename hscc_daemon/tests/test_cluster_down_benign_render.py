"""t_c23c5aef — the UNIFIED ``hscc cluster down`` renderer said ERROR for an
already-down fleet that the standalone view already called OK.

``hscc cluster down`` has TWO human renderers over the SAME engine result dict:

  * standalone — ``hscc-cluster/hscc.py::_human_lines`` (the ``down``/``up``/
    ``stop`` branch). t_1244b3b4 (PR #253) taught it to read the ADDITIVE
    ``benign_no_workloads`` / ``benign_note`` keys;
  * unified — ``hscc_daemon/cluster_render.py::render_down`` ->
    ``_render_fleet_op``. It read ``success`` alone, so the operator's complaint
    survived the merge on the surface they actually type.

Two defects were measured on ``f2cbae3f`` with the real ``_run_or_print`` shape
(probe: verdict of ``render_down`` on a faked dict, no subprocess anywhere):

  ``{success: False, returncode: 1, benign_no_workloads: True, …}``  -> ERROR/1
  ``{success: True,  returncode: 0, error: ""}``                     -> ERROR/1

The second one is why this card had to touch the ``error`` key at all:
``_run_or_print`` always sets ``error`` (captured stderr, ``""`` when there was
none), ``_object_error`` returns that ``""``, and the verdict tested ``is None`` —
so even a clean teardown rendered red. No benign key can rescue that shape,
because ``_benign_no_workloads_note`` returns None for any rc-0 run by design.

Doctrine this file pins (decided on t_1244b3b4, not re-decided here):

  * ``success`` still means "sparkrun exited 0". Nothing here rewrites it — the
    verdict rides in the additive keys, and the renderer never mutates the dict.
  * ONE classifier. ``_benign_no_workloads_note`` in ``hscc-cluster/hscc.py`` is
    the only place sparkrun's wording is read; this renderer reads keys. If it
    ever grew its own marker table the two surfaces could disagree with no test
    able to see it (the ``2f90f711`` kinds-vs-labels split), so both halves of
    that are pinned here: the verdict is invariant to the output TEXT, and no
    marker table may live in the renderer.
  * The veto direction is the one that matters: a real failure — ssh/auth/
    docker/timeout, a host sparkrun ``could not query``, unknown wording —
    carries no benign key and stays red. Anything the classifier did not say is
    not evidence of success.

Fakes only: ``render_down``/``_human_lines`` are called directly, the CLI test
stubs ``_load_cluster_engine``, and the engine's classifier is called on a fake
dict. No ``cluster down``, no ssh, no sparkrun is ever executed.
Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import io
import re
import sys
from contextlib import redirect_stdout

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import cli_theme, cluster_render

# ── fakes ─────────────────────────────────────────────────────────────────

STOPPED_OK = "Stopped 4 job(s), 4 container(s) across 4 host(s)."
ALREADY_DOWN_NONE = "No sparkrun containers running."
ALREADY_DOWN_INTENT = (
    "Error: failed to stop containers on 10.0.0.247: No running workload "
    "matches intent sparkrun_7f2a on hosts ['10.0.0.247']")
# sparkrun/cli/_stop_logs.py:215 — benign ONLY while nothing real rides with it.
ALREADY_DOWN_SUMMARY = ("No sparkrun containers found on the hosts that could "
                        "be queried.")
COULD_NOT_QUERY = "Error: could not query 10.0.0.246: connection reset by peer"
REAL_FAILURE = ("Error: failed to stop containers on 10.0.0.248: "
                "ssh: Permission denied (publickey)")
UNKNOWN_WORDING = "some future sparkrun wording we do not know"

_DOWN_CMD = ["sparkrun", "stop", "--all"]


def _engine():
    """The hscc-cluster engine through the production seam the CLI itself uses.

    Loading it is how this file gets the ONE classifier instead of a second
    copy of it: ``_benign_no_workloads_note`` is called, never re-implemented.
    """
    eng = unified._load_cluster_engine()
    assert eng is not None, "hscc-cluster engine must load through _load_cluster_engine"
    return eng


def _run_or_print_shape(returncode, output, error=""):
    """Exactly the dict ``_run_or_print`` hands the renderers (all five keys)."""
    return {"success": returncode == 0, "returncode": returncode,
            "command": list(_DOWN_CMD), "output": output, "error": error}


def _with_benign_keys(res):
    """The two lines ``cmd_cluster_down`` runs, and nothing else."""
    note = _engine()._benign_no_workloads_note(res)
    if note:
        res["benign_no_workloads"] = True
        res["benign_note"] = note
    return res


def _render_down(result):
    """(rendered text, exit code) from the unified renderer, no ANSI (pipe).

    Wide on purpose: at the default 80 columns a long sparkrun line is broken
    and the wrap is padded with panel borders, so a phrase assertion would be
    about the column count rather than the code. The verdict is width-
    independent, which is why the CLI-level tests use the real console.
    """
    buf = io.StringIO()
    rc = cluster_render.render_down(cli_theme.make_console(file=buf, width=200),
                                    result)
    return buf.getvalue(), rc


def _panel_verdict(text):
    """True if the status Panel says OK. ``make_status_panel`` prints the word."""
    assert ("OK" in text) != ("ERROR" in text), f"no single verdict in {text!r}"
    return "OK" in text


def _flat(text):
    """Rendered text with Rich's wrapping undone, for substring assertions.

    A Console without a tty renders at 80 columns, so a long sparkrun line comes
    back split across rows with panel indent — asserting a phrase against the raw
    capture would depend on the column count, not on the code.
    """
    return re.sub(r"\s+", " ", text)


def _standalone_verdict(result):
    """The standalone view's verdict for the SAME dict (its OK/ERROR tag)."""
    text = "\n".join(_engine()._human_lines("down", result))
    assert ("[ok]OK[/]" in text) != ("ERROR" in text), f"no single verdict: {text!r}"
    return "[ok]OK[/]" in text


class _StubEngine:
    def __init__(self, result):
        self._result = result
        self.seen = []

    def cmd_cluster_down(self, dry_run=False):
        self.seen.append(dry_run)
        return dict(self._result)


def _run_down_cli(monkeypatch, result):
    """`hscc cluster down` end-to-end with a stubbed engine -> (text, rc).

    ``main()`` does ``sys.exit(_handle_cluster())``, so the rc observed here IS
    the renderer's return value propagated — the number the operator's scripts
    and the daemon's autodown path act on.
    """
    stub = _StubEngine(result)
    monkeypatch.setattr(unified, "_load_cluster_engine", lambda: stub)
    monkeypatch.setattr(sys, "argv", ["hscc", "cluster", "down"])
    buf = io.StringIO()
    code = None
    with redirect_stdout(buf):
        try:
            unified.main()
        except SystemExit as exc:
            code = exc.code
    return buf.getvalue(), code, stub


# ── the card's three cases, at the renderer ───────────────────────────────

def test_benign_already_down_renders_ok_panel_and_returns_zero():
    """The dict from the card, verbatim: OK panel, exit code 0."""
    res = {"success": False, "returncode": 1, "benign_no_workloads": True,
           "benign_note": ("nothing to stop — the fleet was already down "
                           "(sparkrun exited nonzero on an already-down host)"),
           "output": f"{STOPPED_OK}\n{ALREADY_DOWN_INTENT}"}
    text, rc = _render_down(res)
    assert rc == 0, text
    assert _panel_verdict(text) is True
    assert "ERROR" not in text
    # the note is shown, and the raw sparkrun lines are still there to read
    flat = _flat(text)
    assert "nothing to stop" in flat, flat
    assert "No running workload matches intent" in flat, flat


def test_without_the_benign_keys_the_same_dict_is_still_red():
    """The keys are the whole difference — absence is not evidence of success."""
    res = {"success": False, "returncode": 1,
           "output": f"{STOPPED_OK}\n{ALREADY_DOWN_INTENT}"}
    assert "benign_no_workloads" not in res
    text, rc = _render_down(res)
    assert rc == 1, text
    assert _panel_verdict(text) is False


def test_hosts_failed_ssh_tail_without_keys_renders_error():
    """A ``hosts_failed`` ssh/permission-denied tail is never softened."""
    res = _run_or_print_shape(1, STOPPED_OK, REAL_FAILURE)
    assert _engine()._benign_no_workloads_note(res) is None
    text, rc = _render_down(res)
    assert rc == 1, text
    assert _panel_verdict(text) is False
    assert "Permission denied" in _flat(text)


# ── the exit code the unified CLI propagates ──────────────────────────────

@pytest.mark.parametrize("expect_rc,build", [
    (0, lambda: _with_benign_keys(_run_or_print_shape(1, ALREADY_DOWN_INTENT))),
    (0, lambda: _run_or_print_shape(0, STOPPED_OK)),          # clean teardown
    (1, lambda: _run_or_print_shape(1, ALREADY_DOWN_INTENT)),  # keys absent
    (1, lambda: _run_or_print_shape(1, STOPPED_OK, REAL_FAILURE)),
])
def test_cli_exit_code_is_the_renderers_exit_code(monkeypatch, expect_rc, build):
    text, rc, _stub = _run_down_cli(monkeypatch, build())
    assert rc == expect_rc, f"rc={rc} for {expect_rc}: {text}"
    assert _panel_verdict(text) is (expect_rc == 0)


def test_clean_stop_does_not_render_red(monkeypatch):
    """The other half of the operator's complaint, measured on f2cbae3f.

    ``_run_or_print`` always sets ``error`` (``""`` when stderr was empty) and
    ``_object_error`` returns that ``""``; the old ``err is None`` read called a
    clean stop a failure. No benign key exists on an rc-0 run, so nothing else
    in the stack could have fixed this.
    """
    res = _run_or_print_shape(0, STOPPED_OK)
    assert res["error"] == ""                       # the trap, made explicit
    assert _engine()._benign_no_workloads_note(res) is None
    text, rc = _render_down(res)
    assert rc == 0 and _panel_verdict(text) is True, text
    text, rc, _ = _run_down_cli(monkeypatch, res)
    assert rc == 0 and _panel_verdict(text) is True, text


def test_dry_run_still_reports_zero_and_touches_nothing(monkeypatch):
    res = {"dry_run": True, "command": list(_DOWN_CMD), "issued": []}
    text, rc, stub = _run_down_cli(monkeypatch, res)
    assert rc == 0 and _panel_verdict(text) is True, text
    assert stub.seen == [False]                     # dispatched as --dry-run
    assert "dry-run" in text.lower()


# ── the --json machine contract is untouched by the renderer ──────────────

def test_rendering_never_mutates_the_result_dict():
    """The panel is a view: no key added, moved or rewritten on the way past."""
    res = _with_benign_keys(_run_or_print_shape(1, STOPPED_OK, ALREADY_DOWN_INTENT))
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in res.items()}
    _render_down(res)
    assert res == before
    assert res["success"] is False                  # still "sparkrun exited 0"
    assert res["returncode"] == 1


def test_a_structural_error_key_still_vetoes():
    """``cmd_cluster_down``'s early-return failure dicts stay red."""
    res = {"error": "cannot import hscc_daemon.serving (fleet down requires "
                    "the daemon's serving module)"}
    text, rc = _render_down(res)
    assert rc == 1 and _panel_verdict(text) is False, text


def test_up_and_stop_surfaces_are_not_softened_by_the_keys():
    """The benign keys belong to the down verb; the siblings keep ruling red.

    ``render_up``/``render_stop`` have their own verdicts. Should anyone route
    them through ``_render_fleet_op`` later, this pins what must not change:
    a failed launch is a failed launch whatever text it carries.
    """
    res = _with_benign_keys(_run_or_print_shape(1, ALREADY_DOWN_NONE))
    buf = io.StringIO()
    console = cli_theme.make_console(file=buf)
    assert cluster_render.render_up(console, {"success": False, "issued": [],
                                              "benign_no_workloads": True,
                                              "benign_note": "stray"}) == 1
    assert cluster_render.render_stop(console, {"success": False, "output": "boom",
                                                "benign_no_workloads": True,
                                                "benign_note": "stray"}) == 1
    assert "ERROR" in buf.getvalue()


# ── THE AGREEMENT PIN: the two surfaces cannot disagree ───────────────────
#
# The corpus is the whole set of shapes the ``down`` verb can hand both
# renderers, in the real ``_run_or_print`` shape. For each one the verdict is
# taken twice: from the unified renderer, and from the standalone view's own
# ``_human_lines``. The classifier that decides anything is the engine's — so if
# either surface grew its own reading of sparkrun's text, the pair below breaks
# on the shape where the copy disagrees.

_CORPUS = [
    # name,                                rc, output,                       error, expect_ok
    ("clean_stop",                          0, STOPPED_OK,                   "",     True),
    ("already_down_none",                   1, ALREADY_DOWN_NONE,            "",     True),
    ("already_down_intent_on_stderr",       1, STOPPED_OK,    ALREADY_DOWN_INTENT,   True),
    ("already_down_summary_only",           1, ALREADY_DOWN_SUMMARY,         "",     True),
    ("unqueryable_host",                    1, ALREADY_DOWN_SUMMARY, COULD_NOT_QUERY, False),
    ("stopped_plus_ssh_denied",             1, STOPPED_OK,        REAL_FAILURE,      False),
    ("already_down_plus_ssh_denied",        1, f"{ALREADY_DOWN_NONE}\n{REAL_FAILURE}", "", False),
    ("unknown_wording",                     1, UNKNOWN_WORDING,              "",     False),
    ("hard_failure_no_output",              1, "", COULD_NOT_QUERY,                  False),
]


@pytest.mark.parametrize("name,rc,output,error,expect_ok", _CORPUS)
def test_the_two_surfaces_cannot_classify_the_same_run_differently(
        name, rc, output, error, expect_ok):
    res = _with_benign_keys(_run_or_print_shape(rc, output, error))
    unified_ok, rc2 = _render_down(res)
    unified_ok = _panel_verdict(unified_ok)
    standalone_ok = _standalone_verdict(res)

    assert unified_ok == standalone_ok, (
        f"{name}: the two renderers disagree — unified={unified_ok} "
        f"standalone={standalone_ok} for {res!r}")
    assert unified_ok is expect_ok, name
    # the exit code the CLI propagates tracks the panel, every shape
    assert rc2 == (0 if unified_ok else 1), f"{name}: panel/exit-code split"


@pytest.mark.parametrize("benign,expect_ok,expect_rc", [
    (False, False, 1),      # nothing classified it => the exit code rules
    (True, True, 0),        # the engine classified it => the note rules
])
def test_only_the_engine_may_read_the_sparkrun_text(benign, expect_ok, expect_rc):
    """The renderer's verdict is a pure function of the KEYS.

    Fixed control keys, four different sparkrun wordings (one already-down, one
    already-down on stderr, one unknown, one already-down with an ssh failure
    riding along) -> one verdict, one exit code. This is what makes a second
    marker table in ``cluster_render`` useless-by-construction rather than
    silently-wrong: the moment the verdict read the text, one of these wordings
    would flip it.
    """
    keys = {"success": False, "returncode": 1, "command": list(_DOWN_CMD),
            "error": "", "output": ""}
    if benign:
        keys.update({"benign_no_workloads": True, "benign_note": "classified"})
    texts = [ALREADY_DOWN_NONE, ALREADY_DOWN_INTENT, UNKNOWN_WORDING,
             f"{ALREADY_DOWN_NONE}\n{REAL_FAILURE}"]
    verdicts, codes = set(), set()
    for t in texts:
        text, code = _render_down(dict(keys, output=t))
        verdicts.add(_panel_verdict(text))
        codes.add(code)
    assert verdicts == {expect_ok}, f"verdict moved with the text: {verdicts}"
    assert codes == {expect_rc}, f"exit code moved with the text: {codes}"


def test_unified_view_is_never_softer_than_the_standalone_one():
    """The one asymmetry that survives, and which way round it goes.

    ``_human_lines`` has no ``error``-key veto, so a result that claims success
    while carrying a non-empty ``error`` reads OK there and ERROR here. That is
    the fail-safe direction (a red panel the operator chases beats a green one
    they trust); the reverse — unified OK where standalone says ERROR — is the
    mistake this card exists to prevent, and the corpus pin above forbids it.
    """
    res = {"success": True, "returncode": 0, "command": list(_DOWN_CMD),
           "output": STOPPED_OK, "error": "docker: error during connect"}
    text, rc = _render_down(res)
    assert _panel_verdict(text) is False and rc == 1, text
    assert _standalone_verdict(res) is True     # documented, not asserted-equal


# ── the single-classifier guard ───────────────────────────────────────────

def _referenced_names(module):
    """Every name the module's CODE touches — docstrings excluded on purpose.

    A source-text scan would flag this very file's prose about where the
    classifier lives; what must not exist is a *reference* — an import, a call,
    or a module-level definition duplicating the engine's tables.
    """
    names = set()

    def walk(code):
        names.update(code.co_names)
        for const in code.co_consts:
            if hasattr(const, "co_names"):
                walk(const)

    for obj in vars(module).values():
        code = getattr(obj, "__code__", None)
        if code is not None:
            walk(code)
    return names


def test_the_renderer_holds_no_marker_table_of_its_own():
    """No second copy of the classification may live in the renderer.

    The failure this guards is invisible from inside either file: two marker
    tables, each self-consistent, disagreeing about one shape the operator then
    sees as a wrong verdict (the ``2f90f711`` kinds-vs-labels split, where
    nothing checked the two halves agreed). The corpus pins above catch a
    *disagreement*; this one catches the *copy* before it can disagree.
    """
    # (a) no MARKER-shaped module attribute, and none referenced by the code
    assert not [n for n in dir(cluster_render) if "MARKER" in n.upper()], \
        "cluster_render must not grow marker tables"
    names = _referenced_names(cluster_render)
    for name in ("_ALREADY_DOWN_MARKERS", "_REAL_FAILURE_MARKERS",
                 "_benign_no_workloads_note"):
        assert name not in names, f"{name} is referenced by the renderer's code"
    # (b) the renderer never imports the engine — its only input is the result
    #     dict it is handed, so no module-level object may come from that tree
    for name, obj in vars(cluster_render).items():
        origin = str(getattr(obj, "__file__", "") or "")
        assert "hscc-cluster" not in origin, \
            f"cluster_render.{name} was imported from the engine"
    # (c) the engine still owns the only copy, and the seam still reaches it
    eng = _engine()
    assert eng._ALREADY_DOWN_MARKERS and eng._REAL_FAILURE_MARKERS
    assert callable(eng._benign_no_workloads_note)
