"""GAP-C-3 reconciliation — tmux_chat holds NO pane matcher of its own.

Audit C found tmux_chat re-deriving hscc-api/repl_inject's pane predicate in
three places (``chat_cmd_profile``, the inline rule in ``_session_profiles``,
the per-line ``any()`` in ``_live_profile_panes``) and ALREADY diverged: the
twin returned ``default`` for EVERY profile-less chat line, while the
canonical rule scans the whole line set and prefers the sibling that NAMES a
profile. On a wrapper pane (launcher first, ``hermes -p X chat`` child
second) the twin mislabelled the operator's REPL ``default`` — so the launch
door could miss a live pane and offer a second chat for a profile
repl_inject resolves just fine, and doctor could name the wrong profile.

Which behaviour wins: the canonical prefer-profiled-sibling rule (repl_inject
owns it; tmux_chat delegates). These tests pin the reconciliation AND that
the matcher is genuinely the canonical module — plus the honest surfaces when
hscc-api is not deployed beside hscc-project (PaneMatcherMissing: named
branch-C refusal, UNVERIFIED doctor row, never a private fallback).

Hermetic: tmux/ps seams faked as in test_tmux_chat.py; no live tmux, no real
ps, no operator pane.
"""

import pytest

from flightdeck.commands import doctor as doctor_cmd
from flightdeck.core import tmux_chat
from flightdeck.core.registry import Project

# A realistic wrapper pair (same shape as hscc-api/tests/test_repl_inject.py):
# a launcher under ~/.hermes/bin (contains "hermes") invoked as `... chat`
# with NO profile flag, plus the real profiled child underneath it.
_LAUNCHER = "/Users/x/.hermes/bin/orch-launch chat --project alpha"
_CHILD = "/Users/x/.hermes/hermes-agent/venv/bin/hermes -p kronos-orch chat"


class Cp:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


# --------------------------------------------------------------------------- #
# The delegation is real: tmux_chat resolves through the canonical module
# --------------------------------------------------------------------------- #

def test_tmux_chat_uses_the_canonical_matcher_module():
    """_pane_matcher() resolves to a module carrying the canonical API —
    the same rule injection uses, not a local twin."""
    m = tmux_chat._pane_matcher()
    for name in tmux_chat._MATCHER_API:
        assert hasattr(m, name), f"matcher module lacks {name}"
    assert m is tmux_chat._pane_matcher(), "resolution must be cached"


def test_chat_cmd_profile_is_pure_delegation():
    """Single-line answers keep the pre-fix semantics (profiled line names
    the profile; a lone profile-less chat is `default`; gateway and non-
    hermes lines are None) — but they come FROM the canonical rule."""
    m = tmux_chat._pane_matcher()
    assert tmux_chat.chat_cmd_profile(_CHILD) == m.pane_chat_command([_CHILD])[0]
    assert tmux_chat.chat_cmd_profile(_CHILD) == "kronos-orch"
    assert tmux_chat.chat_cmd_profile("hermes chat") == "default"
    assert tmux_chat.chat_cmd_profile("hermes gateway run --serve chat") is None
    assert tmux_chat.chat_cmd_profile("python -m http.server") is None


def test_pane_profile_matches_repl_inject_verbatim():
    """pane_profile IS repl_inject.pane_chat_profile — same input, same answer."""
    m = tmux_chat._pane_matcher()
    lines = ["/bin/zsh", _LAUNCHER, _CHILD]
    assert tmux_chat.pane_profile(lines) == m.pane_chat_profile(lines)
    assert tmux_chat.pane_profile(lines) == "kronos-orch"


# --------------------------------------------------------------------------- #
# The divergence itself: what the twin got WRONG and every consumer now gets
# --------------------------------------------------------------------------- #

def test_wrapper_tty_resolves_to_the_childs_profile_once(monkeypatch):
    """The twin saw the wrapper row on a tty, said `default`, AND saw the
    child row, said `kronos-orch` — the same pane reported twice under two
    profiles. Canonical rule: one row, the real profile."""
    monkeypatch.setattr(tmux_chat, "run_ps_all", lambda: (
        f"ttys003 {_LAUNCHER}\n"
        f"ttys003 {_CHILD}\n"))
    assert tmux_chat.chat_processes() == [("ttys003", "kronos-orch")]


def test_wrapper_only_tty_still_default(monkeypatch):
    """No line names a profile -> `default` exactly as repl_inject falls back
    (the per-line rule is gone; the FALLBACK rule stays)."""
    monkeypatch.setattr(tmux_chat, "run_ps_all",
                        lambda: f"ttys003 {_LAUNCHER}\n")
    assert tmux_chat.chat_processes() == [("ttys003", "default")]


def test_live_profile_panes_sees_the_wrapper_pane(monkeypatch):
    """_live_profile_panes (the launch door's reuse check): the OLD per-line
    copy compared `chat_cmd_profile(line) == profile` and matched neither
    line's pair for a wrapper pane whose CHILD is profiled — so the door
    considered the operator's live pane absent and opened a SECOND chat.
    Canonical resolution matches the pane on the child's profile."""
    calls = []

    def fake_tmux(*args):
        calls.append(list(args))
        return Cp(0, out="0\t%1\tt1\n")

    monkeypatch.setattr(tmux_chat, "run_tmux", fake_tmux)
    monkeypatch.setattr(tmux_chat, "run_ps", lambda tty: f"/bin/zsh\n{_LAUNCHER}\n{_CHILD}\n")
    assert tmux_chat._live_profile_panes("kronos-orch") == [("0", "%1")]


def test_session_profiles_resolves_one_profile_per_pane(monkeypatch):
    """_session_profiles (branch B's foreign-session check): the retired
    third copy added one entry PER LINE — a wrapper pane contributed BOTH
    `default` and its real profile, and `default` in the set made a foreign
    profile look compatible (or a matching session look foreign). Canonical:
    exactly the resolved profile per pane."""
    monkeypatch.setattr(tmux_chat, "run_tmux",
                        lambda *a: Cp(0, out="t1\n"))
    monkeypatch.setattr(tmux_chat, "run_ps",
                        lambda tty: f"{_LAUNCHER}\n{_CHILD}\n")
    assert tmux_chat._session_profiles("hscc-alpha") == {"kronos-orch"}


def test_launch_door_reuses_wrapper_pane_instead_of_double_opening(monkeypatch):
    """End-to-end at the door: the operator's REPL runs behind a launcher
    wrapper in an ad-hoc session. `hscc project chat` must ATTACH to that
    pane (branch B reuse) — never create hscc-<project> with a second chat
    for the same profile (which repl_inject would then refuse as ambiguous)."""
    class FakeTmux:
        def run(self, *args):
            if args[0] == "list-panes" and "-a" in args:
                return Cp(0, out="0\t%1\tt1\n")
            if args[0] in ("select-window", "select-pane"):
                return Cp(0)
            raise AssertionError(f"unexpected tmux argv: {args}")

    executed = []
    monkeypatch.setattr(tmux_chat, "run_tmux", FakeTmux().run)
    monkeypatch.setattr(tmux_chat, "run_ps",
                        lambda tty: f"{_LAUNCHER}\n{_CHILD}\n")
    rc = tmux_chat.launch_interactive_chat(
        "alpha", ["hermes", "-p", "kronos-orch", "chat"], env={},
        which=lambda _: "/usr/bin/tmux",
        exec_seam=lambda f, argv: executed.append((f, list(argv))))
    assert rc == 0
    assert executed == [("tmux", ["tmux", "attach-session", "-t", "=0"])]


# --------------------------------------------------------------------------- #
# Matcher missing: named refusal / UNVERIFIED — never a private fallback
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_matcher(monkeypatch):
    """The hscc-api sibling cannot be imported AND is not on disk."""
    monkeypatch.setattr(tmux_chat, "_MATCHER", None)
    monkeypatch.setitem(__import__("sys").modules, "repl_inject", None)
    monkeypatch.setattr(tmux_chat, "_load_matcher_from_sibling_dir",
                        lambda: None)


def test_matcher_missing_raises_named_error(no_matcher):
    with pytest.raises(tmux_chat.PaneMatcherMissing) as ei:
        tmux_chat.pane_profile(["hermes chat"])
    assert "repl_inject" in str(ei.value)


def test_matcher_missing_launch_refuses_with_reason(no_matcher, monkeypatch, capsys):
    """Branch C with a NAMED reason and no TTY: refuse loudly, exec nothing —
    the operator learns hscc-api is missing, instead of a silent private-rule
    verdict that could attach the wrong pane."""
    monkeypatch.setattr(tmux_chat, "run_tmux", lambda *a: Cp(1))
    executed = []
    rc = tmux_chat.launch_interactive_chat(
        "pom", ["hermes", "-p", "pom-orch", "chat"], env={},
        which=lambda _: "/usr/bin/tmux", is_tty=False,
        exec_seam=lambda f, argv: executed.append((f, list(argv))))
    assert rc == 1
    assert executed == []
    err = capsys.readouterr().err
    assert "pane matcher unavailable" in err
    assert "Without tmux, messages from the iOS app will NOT reach" in err


def test_matcher_missing_foreign_session_check_refuses(no_matcher, monkeypatch, capsys):
    """Even the session-exists path reports the missing matcher as a branch-C
    reason rather than guessing the session's profiles."""
    monkeypatch.setattr(tmux_chat, "run_tmux", lambda *a: Cp(0))  # has-session OK
    executed = []
    rc = tmux_chat.launch_interactive_chat(
        "pom", ["hermes", "-p", "pom-orch", "chat"], env={},
        which=lambda _: "/usr/bin/tmux", is_tty=False,
        exec_seam=lambda f, argv: executed.append((f, list(argv))))
    assert rc == 1 and executed == []
    assert "pane matcher unavailable" in capsys.readouterr().err


def test_matcher_module_lacking_canonical_api_is_rejected(monkeypatch):
    """A module cached under the name `repl_inject` that is NOT the matcher
    (e.g. an older deployment predating the public API) must not be trusted
    as the rule owner."""
    class NotTheMatcher:
        pass

    monkeypatch.setattr(tmux_chat, "_MATCHER", None)
    monkeypatch.setitem(__import__("sys").modules, "repl_inject", NotTheMatcher())
    monkeypatch.setattr(tmux_chat, "_load_matcher_from_sibling_dir",
                        lambda: NotTheMatcher())
    with pytest.raises(tmux_chat.PaneMatcherMissing):
        tmux_chat._pane_matcher()


def test_doctor_reports_unverified_when_matcher_missing(monkeypatch):
    """doctor's REPL panel: an unconsultable rule is UNVERIFIED (never ok,
    never the generic tmux-broke wording), and the section stops there."""
    def boom():
        raise tmux_chat.PaneMatcherMissing(
            "pane matcher unavailable — hscc-api/repl_inject.py ...")

    checks = doctor_cmd._tmux_repl_checks(
        projects=[Project(name="pom", repo="/tmp/pom", board="pom", topic=None)],
        _which=lambda n: "/usr/local/bin/tmux",
        _pane_ttys=lambda: set(), _chat_procs=boom)
    assert len(checks) == 1
    assert checks[0]["status"] == "unverified"
    assert "pane matcher unavailable" in checks[0]["detail"]
