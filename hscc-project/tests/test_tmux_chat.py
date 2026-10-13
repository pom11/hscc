"""tmux_chat — every hscc CLI chat launch is tmux-aware (operator addendum,
2026-10-13).

Branches (the contract):
  A. $TMUX set               -> exec the chat in place (exactly the old
                                behaviour; tmux is never even consulted).
  B. no $TMUX, tmux usable   -> the chat lives in the named session
                                hscc-<project>: create it detached running the
                                chat, or reuse the session's live chat, then
                                ATTACH (attach only — never a second chat on
                                the operator's live REPL).
  C. tmux unusable (missing binary / broken server / the named session runs a
     DIFFERENT profile's REPL) -> stop and ask an explicit Y/N naming what is
     missing and the iOS consequence. Default N. No TTY -> refuse, never open.
     Answer y -> plain chat anyway (the operator's informed choice).

Hermeticity: the module's real seams (run_tmux/run_ps/read_line/exec_chat)
raise under pytest unless the test replaced the module attribute — a suite
run can never exec a chat, touch a live tmux server, or block on stdin. The
command-level tests use the existing `exec_seam` + the new `chat_env` pin.
"""

import argparse

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.core import tmux_chat


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class CapExec:
    def __init__(self):
        self.calls = []

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))
        return 0


class Cp:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


HERMES_CHAT = ["hermes", "-p", "pom-orch", "chat", "--continue", "pom"]


class FakeTmux:
    """A scripted tmux server: `sessions` exist (each with pane ttys whose ps
    output is `ps[tty]`). Records every argv."""

    def __init__(self, sessions=None, ps=None, new_session_rc=0):
        self.sessions = sessions or {}          # name -> [tty, ...]
        self.ps = ps or {}                      # tty  -> command lines
        self.new_session_rc = new_session_rc
        self.calls = []

    def attach_session(self, name, ttys):
        self.sessions[name] = list(ttys)

    def run(self, *args):
        self.calls.append(list(args))
        if args[0] == "has-session":
            return Cp(0) if args[-1].lstrip("=") in self.sessions else Cp(1)
        if args[0] == "list-panes" and "-a" in args:
            return Cp(0, out="".join(
                f"{name}\t%{i}-{tty}\t{tty}\n"
                for name, ttys in self.sessions.items()
                for i, tty in enumerate(ttys)))
        if args[0] in ("select-window", "select-pane"):
            return Cp(0)
        if args[0] == "list-panes":
            name = args[args.index("-t") + 1].lstrip("=")
            if name not in self.sessions:
                return Cp(1, err="no such session")
            return Cp(0, out="\n".join(self.sessions[name]) + "\n")
        if args[0] == "new-session":
            if self.new_session_rc != 0:
                return Cp(self.new_session_rc, err="no server running on /private/tmp/tmux-501/default")
            name = args[args.index("-s") + 1]
            self.sessions.setdefault(name, ["t-new"])
            return Cp(0)
        if args[0] == "new-window":
            name = args[args.index("-t") + 1].lstrip("=")
            self.sessions.setdefault(name, []).append("t-win")
            return Cp(0)
        raise AssertionError(f"unexpected tmux argv: {args}")

    def run_ps(self, tty):
        return self.ps.get(tty, "")

    def tmux_argv(self, verb):
        return [c for c in self.calls if c[0] == verb]


def _wire(monkeypatch, fake, *, exec_chat=None, read_line=None):
    """Install the fake world on the module (the identity guard means the
    originals refuse to run under pytest — replacing the attribute is the
    only way through)."""
    monkeypatch.setattr(tmux_chat, "run_tmux", fake.run)
    monkeypatch.setattr(tmux_chat, "run_ps", fake.run_ps)
    monkeypatch.setattr(tmux_chat, "exec_chat", exec_chat or CapExec())
    if read_line is not None:
        monkeypatch.setattr(tmux_chat, "read_line", read_line)
    return exec_chat


# --------------------------------------------------------------------------- #
# Branch A — inside tmux: exec in place, tmux never consulted
# --------------------------------------------------------------------------- #

def test_branch_A_inside_tmux_execs_in_place(monkeypatch):
    fake = FakeTmux()
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat(
        "pom", HERMES_CHAT, env={"TMUX": "/tmp/tmux-501/default,123,0"},
        exec_seam=seam)

    assert rc == 0
    assert seam.calls == [("hermes", HERMES_CHAT)]
    assert fake.calls == [], "inside tmux the chat opens HERE — no tmux calls at all"


# --------------------------------------------------------------------------- #
# Branch B — outside tmux: the chat lives in hscc-<project>
# --------------------------------------------------------------------------- #

def test_branch_B_creates_named_session_then_attaches(monkeypatch, capsys):
    """No session exists -> create it DETACHED running the chat, exec attaches
    (never execs hermes directly: the pane, not this process, hosts the chat)."""
    fake = FakeTmux()
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam)

    assert rc == 0
    created = fake.tmux_argv("new-session")
    assert created == [["new-session", "-d", "-s", "hscc-pom", "--", *HERMES_CHAT]]
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=hscc-pom"])]
    assert "hscc-pom" in capsys.readouterr().out, "operator must SEE where the chat opened"


def test_branch_B_reuses_live_chat_attach_only(monkeypatch):
    """The session already runs THIS profile's chat -> attach only; a second
    chat on the operator's live REPL would fight the session lease."""
    fake = FakeTmux(sessions={"hscc-pom": ["t1"]},
                   ps={"t1": "hermes -p pom-orch chat --continue pom"})
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam)

    assert rc == 0
    assert fake.tmux_argv("new-session") == []
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=hscc-pom"])]


def test_branch_B_reuses_live_pane_in_any_session(monkeypatch, capsys):
    """The profile's REPL already runs in an ad-hoc session ('0') -> attach to
    THAT pane; never create hscc-<project> with a second chat (two panes for
    one profile would make the iOS path refuse as ambiguous)."""
    fake = FakeTmux(sessions={"0": ["t0", "t1"]},
                   ps={"t0": "hermes -p other-orch chat",
                       "t1": "hermes -p pom-orch chat"})
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam,
                                           which=lambda _: "/usr/bin/tmux")

    assert rc == 0
    assert fake.tmux_argv("new-session") == []
    assert fake.tmux_argv("new-window") == []
    assert fake.tmux_argv("select-pane") == [["select-pane", "-t", "%1-t1"]]
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=0"])]
    assert "'0'" in capsys.readouterr().out


def test_branch_C_two_live_panes_for_profile_asks(monkeypatch, capsys):
    """Two panes already run this profile -> no guess, no third chat: named
    reason + Y/N (here N)."""
    fake = FakeTmux(sessions={"0": ["t0"], "x": ["t1"]},
                   ps={"t0": "hermes -p pom-orch chat", "t1": "hermes -p pom-orch chat"})
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam,
                                           which=lambda _: "/usr/bin/tmux",
                                           is_tty=True, ask=lambda q: False)

    assert rc == 1
    assert seam.calls == []
    assert fake.tmux_argv("new-session") == []


def test_branch_B_chatless_named_session_gets_chat_window(monkeypatch):
    """Session exists but holds no chat (plain shell) -> open the chat as a
    new window inside it; attaching to a chat-less session would no-op."""
    fake = FakeTmux(sessions={"hscc-pom": ["t1"]}, ps={"t1": "-zsh"})
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam)

    assert rc == 0
    assert fake.tmux_argv("new-window") == [["new-window", "-t", "=hscc-pom", "-P", "-d",
                                             "--", *HERMES_CHAT]]
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=hscc-pom"])]


def test_session_name_sanitised():
    # tmux session names forbid '.' and ':' — a dotted project name must not
    # produce a name tmux refuses to create.
    assert tmux_chat.session_name("ecofire-bc") == "hscc-ecofire-bc"
    assert tmux_chat.session_name("my.proj:v2") == "hscc-my-proj-v2"


def test_target_profile_parses_chat_argv():
    assert tmux_chat.target_profile(HERMES_CHAT) == "pom-orch"
    assert tmux_chat.target_profile(["hermes", "--profile", "x", "chat"]) == "x"
    assert tmux_chat.target_profile(["hermes", "chat"]) is None


# --------------------------------------------------------------------------- #
# Branch C — tmux unusable: named reason, explicit Y/N, default N
# --------------------------------------------------------------------------- #

def _branch_C(monkeypatch, *, which=(lambda n: "/usr/local/bin/tmux"), answer=None,
              is_tty=True, sessions=None, ps=None, new_session_rc=0):
    """Run branch C end-to-end (the prompt text itself is asserted via
    capsys — prompt_yes_no prints it before reading the answer)."""
    fake = FakeTmux(sessions=sessions, ps=ps, new_session_rc=new_session_rc)
    seam = CapExec()
    lines = iter([answer] if answer is not None else [])

    def read_line():
        return next(lines)

    _wire(monkeypatch, fake, exec_chat=seam, read_line=read_line)
    rc = tmux_chat.launch_interactive_chat(
        "pom", HERMES_CHAT, env={}, which=which, is_tty=is_tty, exec_seam=seam)
    return rc, seam, fake


def test_branch_C_missing_tmux_answers_no_refuses(monkeypatch, capsys):
    rc, seam, fake = _branch_C(monkeypatch, which=lambda n: None, answer="n")

    assert rc != 0
    assert seam.calls == [], "refusal must exec NOTHING"
    out = capsys.readouterr().out
    assert "tmux is not installed (brew install tmux)" in out
    assert "messages from the iOS app will NOT reach this chat" in out
    assert "[y/N]" in out


def test_branch_C_default_answer_empty_is_N(monkeypatch, capsys):
    """The prompt DEFAULT is refuse: an empty Enter answers N."""
    rc, seam, _ = _branch_C(monkeypatch, which=lambda n: None, answer="")
    assert rc != 0 and seam.calls == []


def test_branch_C_default_answer_y_opens_plain(monkeypatch, capsys):
    """An EXPLICIT yes is the operator's informed choice -> plain chat anyway."""
    rc, seam, _ = _branch_C(monkeypatch, which=lambda n: None, answer="y")
    assert rc == 0
    assert seam.calls == [("hermes", HERMES_CHAT)]


def test_branch_C_no_tty_refuses_without_asking(monkeypatch, capsys):
    """Non-interactive (no TTY) -> refuse with the consequence, never open,
    never a silent fallback (e.g. `hscc project chat` inside a CI step)."""
    rc, seam, _ = _branch_C(monkeypatch, which=lambda n: None, is_tty=False)
    assert rc != 0
    assert seam.calls == []
    err = capsys.readouterr().err
    assert "refused (not a terminal to ask)" in err
    assert "messages from the iOS app will NOT reach this chat" in err


def test_branch_C_foreign_profile_session_refused(monkeypatch, capsys):
    """An existing hscc-<project> whose REPL is ANOTHER profile's must never
    be attached to or co-mingled — the reason names exactly that."""
    rc, seam, fake = _branch_C(
        monkeypatch, sessions={"hscc-pom": ["t9"]},
        ps={"t9": "hermes -p other-orch chat --continue other"}, answer="n")
    assert rc != 0 and seam.calls == []
    out = capsys.readouterr().out
    assert "exists but its REPL is for another profile" in out
    assert "other-orch" in out and "pom-orch" in out


def test_branch_C_broken_server_message_includes_stderr(monkeypatch, capsys):
    rc, seam, fake = _branch_C(monkeypatch, new_session_rc=1, answer="n")
    out = capsys.readouterr().out
    assert "tmux server failed to start" in out
    assert "no server running" in out, "the actual stderr is part of the named reason"


# --------------------------------------------------------------------------- #
# Hermeticity guard — the real seams must refuse under pytest
# --------------------------------------------------------------------------- #

def test_real_seams_refuse_under_pytest():
    for fn, args in ((tmux_chat._ORIG_RUN_TMUX, ("has-session", "-t", "=x")),
                     (tmux_chat._ORIG_RUN_PS, ("ttys000",)),
                     (tmux_chat._ORIG_RUN_PS_ALL, ()),
                     (tmux_chat._ORIG_READ_LINE, ())):
        with pytest.raises(RuntimeError, match="hermeticity guard"):
            fn(*args)


# --------------------------------------------------------------------------- #
# Command-level wiring — BOTH chat entry points go through the one helper
# --------------------------------------------------------------------------- #

def _chat_ns(tmp_path, *, exec_seam, chat_env):
    reg = str(tmp_path / "registry.yaml")
    from flightdeck.core import registry
    registry.add_project("pom", repo=str(tmp_path / "pom"), board="pom", path=reg)
    return argparse.Namespace(
        name="pom", registry=reg, session_db=None, default_db=None,
        archive_dir=None, mapping_path=None,
        exec_seam=exec_seam, resume_id=None, extra=None,
        chat_env=chat_env,
    )


def _fake_discovery(monkeypatch, orch_id="20261013_abc"):
    import unittest.mock as mock
    world = {"project": "pom", "topic": None,
             "orch_profile": "pom-orch", "telegram_profile": "default",
             "orch_session": orch_id,
             "orchestrator": {"id": orch_id, "title": "pom", "messages": 5,
                              "last": 1, "started": 0},
             "telegram": [], "telegram_unmapped": 0, "runtime_error": None}
    mock.patch.object(project_cmd.session_discovery, "list_project_sessions",
                      return_value=world).start()
    monkeypatch.setattr(project_cmd.project_lifecycle, "ensure_session",
                        lambda *a, **k: {"session": orch_id, "created": False})
    return world


def test_cmd_chat_outside_tmux_opens_named_session(monkeypatch, tmp_path):
    """`hscc project chat pom` from OUTSIDE tmux -> chat goes to a detached
    hscc-pom session and the CLI execs the attach (the live behaviour the
    manual smoke check reproduces)."""
    _fake_discovery(monkeypatch)
    fake = FakeTmux()
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    rc = project_cmd.cmd_chat(_chat_ns(tmp_path, exec_seam=seam, chat_env={}))

    assert rc == 0
    assert fake.tmux_argv("new-session") and seam.calls[0][0] == "tmux"
    assert seam.calls[0][1][:2] == ["tmux", "attach-session"]


def test_cmd_chat_resume_outside_tmux_uses_same_helper(monkeypatch, tmp_path):
    """The --resume door is tmux-aware through the SAME helper (no second
    exec site may keep the old unconditional execvp)."""
    world = _fake_discovery(monkeypatch)
    fake = FakeTmux()
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)

    args = _chat_ns(tmp_path, exec_seam=seam, chat_env={})
    args.resume_id = world["orchestrator"]["id"]
    rc = project_cmd.cmd_chat(args)

    assert rc == 0
    assert fake.tmux_argv("new-session"), "resume must not bypass tmux"
    created = fake.tmux_argv("new-session")[0]
    assert "--resume" in created and world["orchestrator"]["id"] in created
