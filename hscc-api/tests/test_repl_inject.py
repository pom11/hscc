"""Unit tests for repl_inject — the tmux REPL injection module.

Every test runs against a FAKE tmux: the module-level seams (run_tmux /
run_ps) are monkeypatched, so no real tmux server, no real pane, and never
any operator REPL is touched (the operator's standing rule: tests may not
inject live sessions). The fake records the exact argv the module would run,
so the tests pin the wire contract of the proven hermes-inject tool:
bracketed paste (-p), buffer cleanup (-d), /queue on busy, Enter last.
"""

import pytest

import repl_inject
from repl_inject import InjectError


class FakeTmux:
    """Records tmux argv; returns canned stdout per leading subcommand."""

    def __init__(self, responses=None, raise_on=None):
        self.calls = []           # list of (args_tuple, input_text)
        self.responses = responses or {}
        self.raise_on = raise_on  # subcommand -> exception to raise

    def __call__(self, *args, input_text=None):
        self.calls.append((args, input_text))
        if self.raise_on and args[0] == self.raise_on[0]:
            raise self.raise_on[1]
        for key, val in self.responses.items():
            if args[0] == key:
                return val
        return ""

    def sent_keys(self):
        return [c for c in self.calls if c[0][:1] == ("send-keys",)]

    def pasted(self):
        return [c for c in self.calls if c[0][:1] == ("paste-buffer",)]

    def loaded(self):
        return [c for c in self.calls if c[0][:1] == ("load-buffer",)]


def _pane_line(pid, target, tty):
    return f"{pid}\t{target}\t{tty}"


PANE_LINES = _pane_line("%1", "s1:0.0", "/dev/ttys003")
OTHER = _pane_line("%2", "s2:0.0", "/dev/ttys004")


@pytest.fixture
def fake(monkeypatch):
    ft = FakeTmux()
    monkeypatch.setattr(repl_inject, "run_tmux", ft)
    monkeypatch.setattr(repl_inject, "run_ps", lambda tty: "")
    return ft


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def test_hermes_panes_discovers_profile(fake, monkeypatch):
    fake.responses["list-panes"] = PANE_LINES
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: "/bin/zsh\n/Users/x/.hermes/bin/hermes -p pom-orch chat\n")
    panes = repl_inject.hermes_panes()
    assert len(panes) == 1
    assert panes[0]["profile"] == "pom-orch"
    assert panes[0]["pane"] == "%1"
    assert panes[0]["target"] == "s1:0.0"


def test_hermes_panes_profile_forms(fake, monkeypatch):
    fake.responses["list-panes"] = PANE_LINES
    for cmd, want in [
        ("hermes -p alpha chat", "alpha"),
        ("hermes --profile=beta chat", "beta"),
        ("hermes --profile gamma chat", "gamma"),
        ("hermes chat", "default"),
    ]:
        monkeypatch.setattr(repl_inject, "run_ps", lambda tty, c=cmd: c)
        assert repl_inject.hermes_panes()[0]["profile"] == want


def test_hermes_panes_excludes_gateway_and_non_hermes(fake, monkeypatch):
    fake.responses["list-panes"] = PANE_LINES
    for cmd in ("hermes gateway run --chat-ish", "node chat-server.js",
                "vim notes/chat", "tmux attach"):
        monkeypatch.setattr(repl_inject, "run_ps", lambda tty, c=cmd: c)
        assert repl_inject.hermes_panes() == []


def test_hermes_panes_skips_malformed_lines(fake, monkeypatch):
    fake.responses["list-panes"] = "garbage-no-tabs\n" + PANE_LINES
    monkeypatch.setattr(repl_inject, "run_ps", lambda tty: "hermes -p p chat")
    assert len(repl_inject.hermes_panes()) == 1


def test_hermes_panes_tmux_absent_is_empty_not_crash(monkeypatch):
    def boom(*a, **k):
        raise InjectError("tmux", "no server running")
    monkeypatch.setattr(repl_inject, "run_tmux", boom)
    assert repl_inject.hermes_panes() == []


# --------------------------------------------------------------------------- #
# resolution — refuse on 0 or >1, never guess
# --------------------------------------------------------------------------- #

def _two_panes():
    return [
        {"pane": "%1", "target": "s1:0.0", "tty": "/dev/ttys003",
         "profile": "pom-orch", "cmd": "hermes -p pom-orch chat"},
        {"pane": "%2", "target": "s2:0.0", "tty": "/dev/ttys004",
         "profile": "pom-orch", "cmd": "hermes -p pom-orch chat"},
    ]


def test_resolve_exactly_one_ok():
    p = repl_inject.resolve_pane("pom-orch", panes=[_two_panes()[0]])
    assert p["pane"] == "%1"


def test_resolve_zero_refuses(fake):
    fake.responses["list-panes"] = ""
    with pytest.raises(InjectError) as ei:
        repl_inject.resolve_pane("nope", panes=[])
    assert ei.value.kind == "no_panes"
    assert "no tmux pane" in ei.value.message


def test_resolve_ambiguous_refuses_never_guesses():
    with pytest.raises(InjectError) as ei:
        repl_inject.resolve_pane("pom-orch", panes=_two_panes())
    assert ei.value.kind == "ambiguous"
    assert "2 tmux pane" in ei.value.message


def test_resolve_pane_override_matches_id_and_target():
    panes = _two_panes()
    assert repl_inject.resolve_pane("pom-orch", panes=panes,
                                    pane_override="%2")["pane"] == "%2"
    assert repl_inject.resolve_pane("pom-orch", panes=panes,
                                    pane_override="s1:0.0")["pane"] == "%1"
    with pytest.raises(InjectError):
        repl_inject.resolve_pane("pom-orch", panes=panes, pane_override="%9")


# --------------------------------------------------------------------------- #
# busy detection
# --------------------------------------------------------------------------- #

def test_busy_marker_in_last_nonempty_lines(fake):
    fake.responses["capture-pane"] = (
        "some history\nmsg=interrupt · /queue · /bg · /steer\n\n\n")
    assert repl_inject.is_busy("%1") is True


def test_busy_marker_too_old_is_idle(fake):
    # marker present on screen but NOT within the last 4 non-empty lines
    lines2 = ["msg=interrupt"] + [f"line {i}" for i in range(10)]
    fake.responses["capture-pane"] = "\n".join(lines2)
    assert repl_inject.is_busy("%1") is False


def test_idle_without_marker(fake):
    fake.responses["capture-pane"] = "❯ \n"
    assert repl_inject.is_busy("%1") is False


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #

def test_auto_idle_sends_plain_single_paste_enter(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)
    receipt = repl_inject.inject("pom-orch", "hello there", sleep_fn=lambda s: None)
    assert receipt["mode"] == "plain" and receipt["busy"] is False
    # no /queue prefix
    assert not [c for c in fake.calls
                if c[0][:4] == ("send-keys", "-t", "%1", "-l")]
    ld = fake.loaded()
    assert len(ld) == 1 and ld[0][1] == "hello there"
    assert ld[0][0][:2] == ("load-buffer", "-b")
    assert ld[0][0][2].startswith("hscc-inject-")   # unique buffer name
    pb = fake.pasted()
    assert pb[0][0][0] == "paste-buffer" and "-p" in pb[0][0] and "-d" in pb[0][0]
    assert fake.calls[-1][0] == ("send-keys", "-t", "%1", "Enter")


def test_auto_busy_queues_never_interrupts(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    receipt = repl_inject.inject("pom-orch", "line1\nline2\nline3",
                                 sleep_fn=lambda s: None)
    assert receipt["mode"] == "queue" and receipt["busy"] is True
    keys = fake.sent_keys()
    # prefix goes in FIRST via send-keys -l, then paste, then Enter
    assert keys[0][0] == ("send-keys", "-t", "%1", "-l", "/queue ")
    assert fake.loaded()[0][1] == "line1\nline2\nline3"   # ONE paste
    assert receipt["sent_text"] == "/queue line1\nline2\nline3"
    assert fake.calls[-1][0] == ("send-keys", "-t", "%1", "Enter")


def test_explicit_modes(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    r = repl_inject.inject("pom-orch", "x", mode="steer", sleep_fn=lambda s: None)
    assert r["mode"] == "steer"
    assert fake.sent_keys()[0][0][-1] == "/steer "

    fake.calls.clear()
    r = repl_inject.inject("pom-orch", "x", mode="interrupt",
                           sleep_fn=lambda s: None)
    assert r["mode"] == "interrupt"          # explicit: no prefix, even busy
    assert not [c for c in fake.sent_keys() if c[0][-1].startswith("/")]


def test_trailing_newlines_stripped(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)
    repl_inject.inject("pom-orch", "msg\n\n", sleep_fn=lambda s: None)
    assert fake.loaded()[0][1] == "msg"


def test_empty_message_refuses(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    with pytest.raises(InjectError) as ei:
        repl_inject.inject("pom-orch", "   \n", sleep_fn=lambda s: None)
    assert ei.value.kind == "empty"


def test_unknown_mode_refuses(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    with pytest.raises(InjectError):
        repl_inject.inject("pom-orch", "x", mode="explode",
                           sleep_fn=lambda s: None)


def test_tmux_failure_propagates_as_inject_error(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)

    def boom(*a, **k):
        raise InjectError("tmux", "paste-buffer failed: can't find pane")
    monkeypatch.setattr(repl_inject, "run_tmux", boom)
    with pytest.raises(InjectError) as ei:
        repl_inject.inject("pom-orch", "x", sleep_fn=lambda s: None)
    assert ei.value.kind == "tmux"


def test_ambiguous_pane_blocks_inject_entirely(monkeypatch, fake):
    """Even an idle-looking first match must not be guessed when two exist."""
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: _two_panes())
    with pytest.raises(InjectError) as ei:
        repl_inject.inject("pom-orch", "x", sleep_fn=lambda s: None)
    assert ei.value.kind == "ambiguous"
    assert fake.loaded() == []               # nothing was pasted
    assert fake.sent_keys() == []


# --------------------------------------------------------------------------- #
# run_tmux seam itself (real subprocess path, rc handling)
# --------------------------------------------------------------------------- #

def test_run_tmux_nonzero_raises(monkeypatch):
    class P:
        returncode, stdout, stderr = 1, "", "no server running on /private/tmp/x"
    monkeypatch.setattr(repl_inject.subprocess, "run", lambda *a, **k: P())
    # Explicitly disarm the pytest guard for THIS test only (it tests the rc
    # path itself; it never touches real tmux — subprocess.run is faked).
    monkeypatch.setattr(repl_inject, "_ORIG_RUN_TMUX", object())
    with pytest.raises(InjectError) as ei:
        repl_inject.run_tmux("list-panes")
    assert ei.value.kind == "tmux"
    assert "no server running" in ei.value.message


def test_real_seams_refuse_under_pytest_without_stub():
    """The hermeticity guard itself: with NO stub in place, the original
    seams must refuse to execute (a forgotten stub can never type into a
    live operator pane)."""
    import os
    assert os.environ.get("PYTEST_CURRENT_TEST")       # premise of the guard
    with pytest.raises(InjectError) as ei:
        repl_inject._ORIG_RUN_TMUX("list-panes")
    assert "disabled under pytest" in ei.value.message
    with pytest.raises(InjectError) as ei:
        repl_inject._ORIG_RUN_PS("/dev/ttys003")
    assert "disabled under pytest" in ei.value.message


# --------------------------------------------------------------------------- #
# reply capture
# --------------------------------------------------------------------------- #

def test_last_reply_extracts_box_body():
    screen = (
        "user typed stuff\n"
        "☤ Hermes\n"
        "│ ALPHA answer\n"
        "│ second line\n"
        "╰──────────\n"
        "❯\n")
    assert repl_inject.last_reply(screen) == "ALPHA answer\nsecond line"


def test_last_reply_no_box_is_empty():
    assert repl_inject.last_reply("nothing here") == ""


def test_wait_for_reply_returns_on_change_and_idle():
    calls = {"n": 0}

    def screen_fn(pane, lines):
        calls["n"] += 1
        return "before" if calls["n"] == 1 else ("☤ Hermes\n│ ALPHA\n╰──\n")

    def busy_fn(pane):
        return False

    out = repl_inject.wait_for_reply("%1", "before", timeout=5, poll=0.01,
                                     sleep_fn=lambda s: None,
                                     busy_fn=busy_fn, screen_fn=screen_fn)
    assert out == "ALPHA"


def test_wait_for_reply_timeout_returns_none():
    out = repl_inject.wait_for_reply(
        "%1", "same", timeout=0.05, poll=0.01, sleep_fn=lambda s: None,
        busy_fn=lambda p: True, screen_fn=lambda p, l: "same")
    assert out is None
