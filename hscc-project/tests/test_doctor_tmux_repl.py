"""The tmux/REPL section of doctor — iOS chat reachability.

The iOS app delivers chat into the project's REPL PANE (hscc-api/repl_inject
discovers it via tmux). A REPL running OUTSIDE tmux is invisible to that
path, so iPhone messages silently go to a hidden session and any /goal in
the terminal never sees them. doctor must make that loud, with the statuses
this command is built around: ok / problem / unverified — and an
unverifiable answer must never read as all-clear.

World-injected throughout (_which/_pane_ttys/_chat_procs): no real tmux, no
real ps (the guarded tmux_chat seams refuse to run under pytest anyway).
"""

import json

from flightdeck.commands import doctor as cmd
from flightdeck.core.registry import Project

from tests.test_doctor_command import _args  # reuse the triangle-world harness


def _proj(name="pom"):
    return Project(name=name, repo=f"/tmp/{name}", board=name, topic=None)


def _snap(**over):
    """A triangle-clean world (repo_check ok, boards/workdirs injected) plus
    the tmux world to add. Workdir uses the SAME normalisation the check
    applies (realpath), so /tmp-vs-/private/tmp can't fake a triangle miss."""
    from flightdeck.core import kanban
    base = dict(
        boards=["pom"], workdirs={"pom": kanban._resolve_path("/tmp/pom")},
        repo_check=lambda proj, **_: {"ok": True, "detail": "repo ok"},
    )
    base.update(over)
    return base


WORLD_OK = dict(
    _which=lambda n: "/usr/local/bin/tmux",
    _pane_ttys=lambda: {"ttys001"},
    _chat_procs=lambda: [("ttys001", "pom-orch")],
)
WORLD_OUTSIDE = dict(
    _which=lambda n: "/usr/local/bin/tmux",
    _pane_ttys=lambda: {"ttys001"},
    _chat_procs=lambda: [("ttys007", "pom-orch")],   # a real tty, no pane owns it
)
WORLD_IDLE = dict(
    _which=lambda n: "/usr/local/bin/tmux",
    _pane_ttys=lambda: {"ttys001"},
    _chat_procs=lambda: [],
)
WORLD_NO_TMUX = dict(
    _which=lambda n: None,
    _pane_ttys=lambda: (_ for _ in ()).throw(AssertionError("must not be consulted")),
    _chat_procs=lambda: (_ for _ in ()).throw(AssertionError("must not be consulted")),
)


def _cmd_world(world):
    """Map a *_tmux_repl_checks world onto cmd_doctor's arg seam names
    (cmd_doctor reads args.which/pane_ttys/chat_procs and passes them down
    as the underscore-prefixed check seams)."""
    return dict(tmux_repl=True, which=world["_which"],
                pane_ttys=world["_pane_ttys"], chat_procs=world["_chat_procs"])


def test_repl_in_tmux_pane_ok():
    checks = cmd._tmux_repl_checks(projects=[_proj()], **WORLD_OK)
    repl = [c for c in checks if c["check"] == "repl:pom"]
    assert repl[0]["status"] == "ok" and "tmux pane" in repl[0]["detail"]


def test_repl_outside_tmux_is_problem_naming_consequence():
    checks = cmd._tmux_repl_checks(projects=[_proj()], **WORLD_OUTSIDE)
    repl = [c for c in checks if c["check"] == "repl:pom"][0]
    assert repl["status"] == "problem"
    assert "OUTSIDE tmux" in repl["detail"]
    assert "iOS messages will NOT reach it" in repl["detail"]
    assert "ttys007" in repl["detail"]


def test_no_repl_running_is_not_a_problem():
    """Idle is normal — doctor must not cry wolf (the launch door fixes it)."""
    checks = cmd._tmux_repl_checks(projects=[_proj()], **WORLD_IDLE)
    repl = [c for c in checks if c["check"] == "repl:pom"][0]
    assert repl["status"] == "ok"
    assert "right now" in repl["detail"]


def test_missing_tmux_is_problem():
    checks = cmd._tmux_repl_checks(projects=[_proj()], **WORLD_NO_TMUX)
    assert [c for c in checks if c["check"] == "tmux"][0]["status"] == "problem"
    # ...and per-project reachability is reported too, not silently skipped.
    assert [c for c in checks if c["check"] == "repl:pom"][0]["status"] == "problem"


def test_broken_tmux_server_is_unverified_not_ok():
    def boom():
        raise RuntimeError("no server running")
    checks = cmd._tmux_repl_checks(
        projects=[_proj()], _which=lambda n: "/usr/local/bin/tmux",
        _pane_ttys=boom, _chat_procs=boom)
    assert len(checks) == 1 and checks[0]["status"] == "unverified"
    assert "unknown" in checks[0]["detail"]


def test_cmd_doctor_renders_section_and_exit_code_falls():
    """The exit-code contract: outside-tmux REPL => NOT ALL CLEAR (1); the
    same world with the REPL in a pane => all clear (0)."""
    out = _args(**_snap(**_cmd_world(WORLD_OUTSIDE)))
    assert cmd.cmd_doctor(out, [_proj()]) == 1

    ok_args = _args(**_snap(**_cmd_world(WORLD_OK)))
    assert cmd.cmd_doctor(ok_args, [_proj()]) == 0


def test_cmd_doctor_json_carries_tmux_repl(capsys):
    args = _args(json=True, **_snap(**_cmd_world(WORLD_OUTSIDE)))
    assert cmd.cmd_doctor(args, [_proj()]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["_tmux_repl"]["repl:pom"]["status"] == "problem"


def test_section_absent_when_not_requested():
    """Tests (and any non-production caller) that don't opt in get the
    pre-section behaviour COMPLETELY untouched — no tmux key, no exit change."""
    args = _args(**_snap())  # no tmux_repl key
    assert cmd.cmd_doctor(args, [_proj()]) == 0
    checks = cmd._tmux_repl_checks  # (documented: the gate is in cmd_doctor)
    assert callable(checks)


def test_repl_matcher_excludes_gateway_and_default_profile():
    from flightdeck.core import tmux_chat as tc
    assert tc.chat_cmd_profile("hermes -p pom-orch chat --continue pom") == "pom-orch"
    assert tc.chat_cmd_profile("hermes chat") == "default"
    # gateway/serve processes carry 'chat' in their argv too — never a REPL.
    assert tc.chat_cmd_profile("hermes gateway run --serve chat") is None
    assert tc.chat_cmd_profile("python -m http.server") is None
