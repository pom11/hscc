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
# discovery — WRAPPER panes (the 2026-10-29 reference-tool fix)
#
# A launcher (e.g. the one that starts kronos-orch) can be the pane's FIRST
# chat-looking process, with the real `hermes -p <profile> chat` underneath it.
# The wrapper line carries no -p/--profile, so taking the first match labelled
# the pane `default` and an inject by profile name refused on the one pane
# that was actually the right one.
# --------------------------------------------------------------------------- #

# Realistic wrapper: a launcher script living under ~/.hermes/bin (so it
# contains "hermes") invoked as `... chat --project ...` (so it contains " chat")
# with NO profile flag — it is the child that names the profile.
_LAUNCHER = "/Users/x/.hermes/bin/orch-launch chat --project alpha"
_CHILD = "/Users/x/.hermes/hermes-agent/venv/bin/hermes -p kronos-orch chat"


def test_wrapper_pane_resolves_the_childs_profile_not_default(fake, monkeypatch):
    """Wrapper FIRST, real chat SECOND: the profile-named line wins."""
    fake.responses["list-panes"] = PANE_LINES
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: f"/bin/zsh\n{_LAUNCHER}\n{_CHILD}\n")
    panes = repl_inject.hermes_panes()
    assert len(panes) == 1
    assert panes[0]["profile"] == "kronos-orch"
    assert panes[0]["cmd"] == _CHILD            # the command that justified it


def test_wrapper_pane_child_first_ordering_also_resolves(fake, monkeypatch):
    """Order must not matter — the rule is 'the line that names a profile'."""
    fake.responses["list-panes"] = PANE_LINES
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: f"{_CHILD}\n{_LAUNCHER}\n")
    assert repl_inject.hermes_panes()[0]["profile"] == "kronos-orch"


def test_wrapper_only_pane_falls_back_to_default(fake, monkeypatch):
    """No line names a profile -> `default` (the reference tool's fallback)."""
    fake.responses["list-panes"] = PANE_LINES
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: f"/bin/zsh\n{_LAUNCHER}\n")
    panes = repl_inject.hermes_panes()
    assert len(panes) == 1 and panes[0]["profile"] == "default"


def test_wrapper_pane_never_picks_a_gateway_line(fake, monkeypatch):
    """A gateway line before the real chat must not become the target."""
    fake.responses["list-panes"] = PANE_LINES
    monkeypatch.setattr(
        repl_inject, "run_ps",
        lambda tty: ("hermes gateway run --chat-ish\n" + _LAUNCHER + "\n"
                     + _CHILD + "\n"))
    panes = repl_inject.hermes_panes()
    assert panes[0]["profile"] == "kronos-orch"
    assert "gateway" not in panes[0]["cmd"]


def test_wrapper_pane_ambiguous_still_refuses_and_sends_nothing(monkeypatch,
                                                                fake):
    """Two wrapper panes resolving to ONE profile: exactly-one rule holds."""
    fake.responses["list-panes"] = PANE_LINES + "\n" + OTHER
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: f"{_LAUNCHER}\n{_CHILD}\n")
    panes = repl_inject.hermes_panes()
    assert [p["profile"] for p in panes] == ["kronos-orch", "kronos-orch"]
    assert [p["pane"] for p in panes] == ["%1", "%2"]
    with pytest.raises(InjectError) as ei:
        repl_inject.inject("kronos-orch", "x", sleep_fn=lambda s: None)
    assert ei.value.kind == "ambiguous"
    assert "2 tmux pane" in ei.value.message
    assert fake.loaded() == []                 # nothing pasted
    assert fake.pasted() == []
    assert fake.sent_keys() == []              # nothing typed


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
    # `interrupt` is the reference tool's old name for a no-verb send; the
    # receipt reports the canonical name, never the legacy alias.
    assert r["mode"] == "plain"
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
# mode table — the operator naming contract (plain / queue / steer / auto)
# --------------------------------------------------------------------------- #

def test_mode_table_is_the_naming_contract():
    """plain = NO verb prefix; queue/steer carry theirs; auto resolves."""
    assert repl_inject.MODES == ("auto", "plain", "queue", "steer")
    assert repl_inject.VERB_PREFIX == {"plain": "", "queue": "/queue ",
                                       "steer": "/steer "}
    # auto is resolved against busy, never delivered verbatim.
    assert repl_inject.resolve_mode("auto", True) == "queue"
    assert repl_inject.resolve_mode("auto", False) == "plain"
    for m in ("plain", "queue", "steer"):
        assert repl_inject.resolve_mode(m, True) == m
        assert repl_inject.resolve_mode(m, False) == m
    # legacy alias, in both directions of busy.
    assert repl_inject.resolve_mode("interrupt", True) == "plain"
    assert repl_inject.normalize_mode("interrupt") == "plain"


def test_resolve_mode_rejects_unknown():
    with pytest.raises(InjectError) as ei:
        repl_inject.resolve_mode("explode", False)
    assert ei.value.kind == "empty"
    assert "unknown mode" in ei.value.message


def test_plain_while_busy_sends_no_prefix_and_reports_interrupts(monkeypatch,
                                                                  fake):
    """The one combination that cuts off a turn — it must be visible, not quiet."""
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    r = repl_inject.inject("pom-orch", "x", mode="plain", sleep_fn=lambda s: None)
    assert r["mode"] == "plain" and r["busy"] is True
    assert r["interrupts"] is True and r["verb"] == ""
    # Enter only — no verb prefix typed first (FakeTmux records (args, input)).
    assert fake.sent_keys() == [(("send-keys", "-t", "%1", "Enter"), None)]
    assert fake.loaded()[0][1] == "x"


def test_idle_plain_reports_no_interrupt(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)
    r = repl_inject.inject("pom-orch", "x", mode="plain", sleep_fn=lambda s: None)
    assert r["mode"] == "plain" and r["interrupts"] is False


def test_receipt_carries_verb_for_every_mode(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    for mode, want_mode, want_verb in [
        ("auto", "queue", "/queue "),
        ("queue", "queue", "/queue "),
        ("steer", "steer", "/steer "),
        ("plain", "plain", ""),
    ]:
        fake.calls.clear()
        r = repl_inject.inject("pom-orch", "hi", mode=mode,
                               sleep_fn=lambda s: None)
        assert (r["mode"], r["verb"]) == (want_mode, want_verb)


# --------------------------------------------------------------------------- #
# dry-run / planning — prints the decision, sends nothing
# --------------------------------------------------------------------------- #

def test_dry_run_sends_nothing_and_reports_the_target(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    r = repl_inject.inject("pom-orch", "do the thing", dry_run=True,
                           sleep_fn=lambda s: None)
    assert r["dry_run"] is True
    assert (r["pane"], r["target"], r["profile"]) == ("%1", "s1:0.0", "pom-orch")
    assert r["busy"] is True and r["mode"] == "queue" and r["verb"] == "/queue "
    assert r["text"] == "do the thing" and r["chars"] == len("do the thing")
    # THE point of --dry-run: not one keystroke, not one buffer.
    assert fake.loaded() == [] and fake.pasted() == [] and fake.sent_keys() == []
    assert fake.calls == []            # no tmux call at all beyond discovery


def test_dry_run_idle_chooses_plain(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)
    r = repl_inject.inject("pom-orch", "x", dry_run=True, sleep_fn=lambda s: None)
    assert r["busy"] is False and r["mode"] == "plain" and r["verb"] == ""
    assert fake.calls == []


def test_dry_run_reports_interrupt_when_plain_meets_busy(monkeypatch, fake):
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    r = repl_inject.inject("pom-orch", "x", mode="plain", dry_run=True,
                           sleep_fn=lambda s: None)
    assert r["mode"] == "plain" and r["interrupts"] is True
    assert fake.calls == []


def test_dry_run_reads_busy_from_the_real_screen_and_still_sends_nothing(
        monkeypatch, fake):
    """The --dry-run contract: the idle/busy it prints is the REAL one.

    is_busy is NOT stubbed here — only the seams are — so this pins that a dry
    run really captures the pane (one capture-pane, no send-keys), rather than
    printing a made-up state.
    """
    fake.responses["list-panes"] = PANE_LINES
    fake.responses["capture-pane"] = (
        "history line\nmsg=interrupt · /queue · /steer\n\n")
    monkeypatch.setattr(repl_inject, "run_ps",
                        lambda tty: "/Users/x/hermes -p pom-orch chat\n")
    r = repl_inject.inject("pom-orch", "x", dry_run=True, sleep_fn=lambda s: None)
    assert r["busy"] is True and r["mode"] == "queue"
    assert [c[0][0] for c in fake.calls] == ["list-panes", "capture-pane"]


def test_dry_run_still_validates(monkeypatch, fake):
    """A dry run must refuse exactly what a real send would refuse."""
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: False)
    with pytest.raises(InjectError) as ei:      # empty text
        repl_inject.inject("pom-orch", "  \n", dry_run=True,
                           sleep_fn=lambda s: None)
    assert ei.value.kind == "empty"
    with pytest.raises(InjectError) as ei:      # unknown mode
        repl_inject.inject("pom-orch", "x", mode="explode", dry_run=True,
                           sleep_fn=lambda s: None)
    assert ei.value.kind == "empty"
    with pytest.raises(InjectError) as ei:      # ambiguous pane
        monkeypatch.setattr(repl_inject, "hermes_panes", lambda: _two_panes())
        repl_inject.inject("pom-orch", "x", dry_run=True,
                           sleep_fn=lambda s: None)
    assert ei.value.kind == "ambiguous"
    assert fake.calls == []


def test_plan_rejects_bad_mode_before_touching_a_pane(monkeypatch, fake):
    """No capture-pane, no send: a bad mode is refused up front."""
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    with pytest.raises(InjectError):
        repl_inject.plan("pom-orch", mode="explode")
    assert fake.calls == []


def test_plan_pane_and_inject_agree_on_the_decision(monkeypatch, fake):
    """One shared decision: the preview cannot drift from what delivery does."""
    monkeypatch.setattr(repl_inject, "hermes_panes", lambda: [_two_panes()[0]])
    monkeypatch.setattr(repl_inject, "is_busy", lambda pane: True)
    planned = repl_inject.plan("pom-orch")
    sent = repl_inject.deliver(_two_panes()[0], "x", sleep_fn=lambda s: None)
    for key in ("pane", "target", "profile", "busy", "mode", "verb",
                "interrupts"):
        assert sent[key] == planned[key], key


def test_format_plan_lines():
    busy_queue = repl_inject.plan_pane(_two_panes()[0], busy_fn=lambda p: True)
    assert repl_inject.format_plan(busy_queue) == (
        "target: pom-orch @ s1:0.0 (busy) -> queue")
    idle = repl_inject.plan_pane(_two_panes()[0], busy_fn=lambda p: False)
    assert repl_inject.format_plan(idle) == (
        "target: pom-orch @ s1:0.0 (idle) -> plain")
    interrupt = repl_inject.plan_pane(_two_panes()[0], mode="plain",
                                      busy_fn=lambda p: True)
    assert repl_inject.format_plan(interrupt) == (
        "target: pom-orch @ s1:0.0 (busy) -> plain (interrupts the running turn)")


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
