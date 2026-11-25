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
import shutil
import subprocess

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


def _target_name(target):
    """`=NAME:` / `=NAME` / `NAME` -> NAME (the session part of a tmux target)."""
    return target.lstrip("=").rstrip(":")


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
            return (Cp(0) if _target_name(args[-1]) in self.sessions
                    else Cp(1))
        if args[0] == "list-panes" and "-a" in args:
            return Cp(0, out="".join(
                f"{name}\t%{i}-{tty}\t{tty}\n"
                for name, ttys in self.sessions.items()
                for i, tty in enumerate(ttys)))
        if args[0] in ("select-window", "select-pane"):
            return Cp(0)
        if args[0] == "list-panes":
            name = _target_name(args[args.index("-t") + 1])
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
            name = _target_name(args[args.index("-t") + 1])
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
    assert fake.tmux_argv("new-window") == [["new-window", "-t", "=hscc-pom:", "-P",
                                             "-d", "--", *HERMES_CHAT]]
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=hscc-pom"])]


def test_branch_B_chatless_session_named_zero_gets_a_window(monkeypatch):
    """Session literally named `0`: `=0` is WINDOW 0, so the chat must target
    `=0:` — the session — or tmux fails with "index 0 in use" (same as #483)."""
    fake = FakeTmux(sessions={"0": ["t1"]}, ps={"t1": "-zsh"})
    seam = CapExec()
    _wire(monkeypatch, fake, exec_chat=seam)
    monkeypatch.setattr(tmux_chat, "session_name", lambda project: "0")

    rc = tmux_chat.launch_interactive_chat("pom", HERMES_CHAT, env={}, exec_seam=seam)

    assert rc == 0
    assert fake.tmux_argv("new-window") == [["new-window", "-t", "=0:", "-P",
                                             "-d", "--", *HERMES_CHAT]]
    assert seam.calls == [("tmux", ["tmux", "attach-session", "-t", "=0"])]


def test_session_profiles_addresses_the_session_not_its_window_zero(monkeypatch):
    """`list-panes -t =0` is WINDOW 0 of the current session; the profile check
    must pin session `0` (`=0:`) and cover every window (`-s`), including the
    chat window `new-window -d` did not make current."""
    calls = []

    def fake_run(*args):
        calls.append(list(args))
        return Cp(0, out="t1\n")

    monkeypatch.setattr(tmux_chat, "run_tmux", fake_run)
    monkeypatch.setattr(tmux_chat, "run_ps",
                        lambda tty: "hermes -p pom-orch chat")

    assert tmux_chat._session_profiles("0") == {"pom-orch"}
    assert calls == [["list-panes", "-s", "-t", "=0:", "-F",
                      "#{pane_tty}"]]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_chatless_session_named_zero_on_a_real_tmux_server(tmp_path, monkeypatch):
    """The fake runner cannot model tmux target resolution, so use a real
    throwaway server (`tmux -L`, never the default): session `0` already has
    window 0; the chat must open at the next free index, not "index 0 in use"."""
    socket = f"hscc-test-{tmp_path.name[-12:]}"

    def tmux(*args):
        return subprocess.run(["tmux", "-L", socket, *map(str, args)],
                              capture_output=True, text=True)

    created = tmux("new-session", "-d", "-s", "0", "-n", "operator",
                   "sleep", "30")
    assert created.returncode == 0, created.stderr
    try:
        monkeypatch.setattr(tmux_chat, "run_tmux", tmux)
        monkeypatch.setattr(tmux_chat, "run_ps", lambda tty: "-zsh\n")
        monkeypatch.setattr(tmux_chat, "session_name", lambda project: "0")

        reason = tmux_chat._ensure_tmux_session(
            "pom", ["sleep", "30"], which=lambda _: "/usr/bin/tmux")

        assert reason is None, reason
        windows = tmux("list-windows", "-t", "=0:", "-F",
                       "#{window_index} #{window_name}")
        assert windows.returncode == 0, windows.stderr
        assert "0 operator" in windows.stdout, windows.stdout
        assert "1 " in windows.stdout, windows.stdout
    finally:
        tmux("kill-server")


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
    world = {"project": "pom", "topic": None,
             "orch_profile": "pom-orch", "telegram_profile": "default",
             "orch_session": orch_id,
             "orchestrator": {"id": orch_id, "title": "pom", "messages": 5,
                              "last": 1, "started": 0},
             "telegram": [], "telegram_unmapped": 0, "runtime_error": None}
    # Registered with monkeypatch (auto-undone at teardown), never a bare
    # mock.patch(...).start(): that survives the test and pins discovery for
    # every later module in the same pytest process (t_af788f73).
    monkeypatch.setattr(project_cmd.session_discovery, "list_project_sessions",
                        lambda *a, **k: world)
    monkeypatch.setattr(project_cmd.project_lifecycle, "ensure_session",
                        lambda *a, **k: {"session": orch_id, "created": False})
    return world


def test_fake_discovery_patch_is_scoped_to_its_test():
    """Regression (t_af788f73): `_fake_discovery` must leave no live patch
    behind. A bare `mock.patch.object(...).start()` is invisible to
    monkeypatch teardown, so `test_chat_seed.py` — collected later in the same
    process — saw the pom world and 4 seed cases failed."""
    real = project_cmd.session_discovery.list_project_sessions
    scoped = pytest.MonkeyPatch()
    try:
        assert _fake_discovery(scoped)["orchestrator"]["id"] == "20261013_abc"
        assert project_cmd.session_discovery.list_project_sessions is not real
    finally:
        scoped.undo()
    assert project_cmd.session_discovery.list_project_sessions is real, (
        "_fake_discovery left a global mock.patch started; a later test module "
        "would see the pom discovery world")


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


# --------------------------------------------------------------------------- #
# The tmux SERVER seam (t_4b3e3806): a chat launch must join the running server
#
# With TMUX_TMPDIR unset (a fresh shell), unqualified tmux looks only in /tmp
# and the launch door CREATES hscc-<project> on a SECOND server, invisible to
# the operator's own tmux. These tests run the real run_tmux against a fake
# tmux binary and fake unix sockets; no live server is reachable.
# --------------------------------------------------------------------------- #

FAKE_TMUX_SERVER_SRC = r'''#!{python}
import json
import os
import sys

argv = sys.argv[1:]
log = os.environ["FAKE_TMUX_LOG"]
live = [p for p in os.environ.get("FAKE_TMUX_LIVE", "").split(os.pathsep) if p]

target = None
i = 0
while i < len(argv) and argv[i].startswith("-"):
    if argv[i] == "-S" and i + 1 < len(argv):
        target = argv[i + 1]
        i += 2
        continue
    break
explicit = target is not None
if target is None:
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    target = os.path.join(base, "tmux-%d" % os.getuid(), "default")

with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "socket": target,
                         "explicit": explicit}}) + "\n")

if target not in live:
    sys.stderr.write("no server running on %s\n" % target)
    sys.exit(1)

verb = next((a for a in argv[i:]
             if not a.startswith("-") and not a.startswith("#{{")), "")
if verb == "has-session":
    sys.exit(1)                       # no hscc-<project> session yet
if verb in ("new-session", "new-window"):
    sys.stdout.write("%9\n")
sys.exit(0)
'''


def _load_repl_inject_by_path():
    import importlib.util
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "hscc-api" / "repl_inject.py"
    spec = importlib.util.spec_from_file_location("_tmux_chat_socket_ri", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmux_server(tmp_path, monkeypatch):
    import os
    import socket

    base = os.path.join("/tmp", f"htc{os.getpid()}")
    default_dir = os.path.join(base, "a")
    private_dir = os.path.join(base, "b")
    for d in (default_dir, private_dir):
        os.makedirs(os.path.join(d, f"tmux-{os.getuid()}"), exist_ok=True)

    listeners = []
    paths = {}
    for where, root in (("default", default_dir), ("private", private_dir)):
        path = os.path.join(root, f"tmux-{os.getuid()}", "default")
        assert len(path) < 104, path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(8)
        listeners.append(sock)
        paths[where] = path

    log = tmp_path / "tmux-calls.jsonl"
    fake = tmp_path / "tmux"
    fake.write_text(FAKE_TMUX_SERVER_SRC.format(python=__import__("sys").executable))
    fake.chmod(0o755)

    ri = _load_repl_inject_by_path()
    monkeypatch.setattr(tmux_chat, "_MATCHER", ri)
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", os.path.join(base, "empty"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setattr(ri, "TMUX_DEFAULT_TMPDIR", default_dir)
    monkeypatch.setattr(ri, "TMUX_PRIVATE_TMPDIRS", (private_dir,))
    monkeypatch.setattr(ri, "TMUX_BIN", str(fake))
    ri.reset_tmux_socket_cache()

    def live(*where):
        monkeypatch.setenv(
            "FAKE_TMUX_LIVE", os.pathsep.join(paths[w] for w in where))

    def calls():
        import json
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()
                if line.strip()]

    live("private")

    def cleanup():
        for s in listeners:
            s.close()
        ri.reset_tmux_socket_cache()
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    yield {"paths": paths, "live": live, "calls": calls,
           "default_dir": default_dir, "ri": ri, "cleanup": cleanup}
    cleanup()


def _tmux_verb(argv):
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-S":
            i += 2
            continue
        break
    return next((a for a in argv[i:]
                 if not a.startswith("-") and not a.startswith("#{")), "")


def test_launch_joins_the_server_discovered_without_tmux_tmpdir(tmux_server):
    """MANDATORY shape from t_4c5855a6: TMUX_TMPDIR removed, private server.

    At base `new-session` carries no -S, the fake (like tmux) answers only on
    the live private socket, and the named session lands on a second server.
    """
    import os
    assert "TMUX_TMPDIR" not in os.environ
    reason = tmux_chat._ensure_tmux_session(
        "alpha", HERMES_CHAT, lambda _: "/usr/bin/tmux")
    assert reason is None, reason
    news = [c for c in tmux_server["calls"]()
            if _tmux_verb(c["argv"]) == "new-session"]
    assert len(news) == 1, tmux_server["calls"]()
    assert news[0]["explicit"] is True
    assert news[0]["socket"] == tmux_server["paths"]["private"]


def test_launch_adds_no_socket_argument_when_tmux_tmpdir_is_set(tmux_server,
                                                                 monkeypatch):
    """NO-REGRESSION: declared TMUX_TMPDIR -> argv byte-identical."""
    import os
    tmux_server["live"]("default")
    monkeypatch.setenv("TMUX_TMPDIR", tmux_server["default_dir"])
    r = tmux_chat.run_tmux("list-sessions")
    assert r.returncode == 0, r.stderr
    assert os.environ["TMUX_TMPDIR"] == tmux_server["default_dir"]
    assert tmux_server["calls"]()
    assert all(not c["explicit"] for c in tmux_server["calls"]())


def test_two_live_servers_refuse_as_a_named_branch_c_reason(tmux_server):
    """Two servers + no TMUX_TMPDIR: branch C says which fix, never guesses."""
    tmux_server["live"]("default", "private")
    reason = tmux_chat._ensure_tmux_session(
        "alpha", HERMES_CHAT, lambda _: "/usr/bin/tmux")
    assert reason is not None
    assert "TMUX_TMPDIR" in reason, reason
    assert not [c for c in tmux_server["calls"]()
                if _tmux_verb(c["argv"]) == "new-session"]
