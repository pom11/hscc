"""Which tmux SERVER hscc talks to (t_4c5855a6).

The operator bug: ``TMUX_TMPDIR`` is exported only by the INTERACTIVE shell rc,
so a fresh shell (cron, launchd, an ssh one-liner, the waker) aims tmux at a
socket directory the running server never used, and every pane disappears as
"found: none" while the REPL sits visibly on screen. These tests pin both sides
of the fix:

  * MANDATORY — a run with ``TMUX_TMPDIR`` REMOVED from the environment still
    finds the server that owns the panes, via a discovered ``-S <socket>``;
  * no-regression — when ``TMUX_TMPDIR`` (or ``TMUX``) is set, hscc adds no
    argument at all and the argv is byte-identical to before.

Everything here runs against a FAKE tmux binary and FAKE sockets the fixture
binds. No test can reach a live server: the fake answers only on the socket
paths the fixture marks live, and it records every invocation it receives so
the argv shape is asserted rather than assumed.
"""

import json
import os
import shutil
import socket
import sys

import pytest

import repl_inject
from repl_inject import InjectError

# A unix socket path must fit macOS' 104-byte sockaddr_un for BOTH bind and
# connect, and pytest's tmp_path is already ~85 bytes on this host — so the
# fake sockets live under the shortest temp root available (the fixture fails
# loudly if even that does not fit) while the fake binary and its call log stay
# under tmp_path.
SOCK_PATH_LIMIT = 104

CHAT_PS_OUT = (
    "user 1 0.0 0.1 ttysfake0 10:00AM 0:01.00 "
    "/x/python3 -I -c sys.argv = ['/x/hermes', '-p', 'general', 'chat']\n")

FAKE_TMUX_SRC = r'''#!/usr/bin/env PYTHON
"""Stand-in for the tmux binary: answers ONLY on the sockets it was given."""
import json
import os
import sys
import time

argv = sys.argv[1:]
log = os.environ["FAKE_TMUX_LOG"]
live = [p for p in os.environ.get("FAKE_TMUX_LIVE", "").split(os.pathsep) if p]

# Only a -S BEFORE the subcommand is tmux's global socket option. After the
# subcommand, -S is capture-pane's start-line (`capture-pane -S -12`), which is
# why this walks the leading options and stops at the first verb.
target = explicit = None
i = 0
while i < len(argv) and argv[i].startswith("-"):
    if argv[i] == "-S" and i + 1 < len(argv):
        target, explicit = argv[i + 1], True
        i += 2
        continue
    break

if target is None:
    # tmux's own rule (man tmux, -L): the socket `default` in
    # <TMUX_TMPDIR or /tmp>/tmux-<uid>/. The fake follows it, which is what
    # makes "TMUX_TMPDIR is set" a genuine no-regression test.
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    target = os.path.join(base, "tmux-%d" % os.getuid(), "default")
    explicit = False

with open(log, "a") as fh:
    fh.write(json.dumps({"argv": argv, "socket": target,
                         "explicit": explicit}) + "\n")

if target not in live:
    sys.stderr.write("no server running on %s\n" % target)
    sys.exit(1)

if os.environ.get("FAKE_TMUX_HANG") == target:
    time.sleep(float(os.environ.get("FAKE_TMUX_HANG_S", "30")))

verbs = [a for a in argv[i:] if not a.startswith("-") and not a.startswith("#{")]
verb = verbs[0] if verbs else ""

if verb == "list-sessions":
    sys.stdout.write("0: 1 windows (created fake)\n")
elif verb == "list-panes":
    sys.stdout.write(os.environ.get("FAKE_TMUX_PANES", ""))
elif verb == "display-message":
    if os.environ.get("FAKE_TMUX_BUSY") == "1":
        sys.stdout.write("* (prompt)\n")
sys.exit(0)
'''


def _temp_root():
    """Shortest existing temp dir (see SOCK_PATH_LIMIT)."""
    for cand in ("/tmp", "/var/tmp", os.path.realpath(tempfile_gettempdir())):
        if os.path.isdir(cand):
            return cand
    raise AssertionError("no temp dir available for a fake tmux socket")


def tempfile_gettempdir():
    import tempfile
    return tempfile.gettempdir()


def _verb_of(argv):
    """The subcommand, skipping tmux's GLOBAL options (`-S <socket>` first).

    Mirrors the fake's parser: a leading ``-S`` is the socket, and its value is
    not a verb — while a ``-S`` after the subcommand is capture-pane's start
    line. Getting this wrong reads a socket path back as a verb.
    """
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-S":
            i += 2
            continue
        break
    return next((a for a in argv[i:]
                 if not a.startswith("-") and not a.startswith("#{")), "")


class Calls:
    """The fake tmux's recorded invocations."""

    def __init__(self, log_path):
        self.log_path = log_path

    def all(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verbs(self):
        return [_verb_of(c["argv"]) for c in self.all()]

    def count(self, verb):
        return sum(1 for v in self.verbs() if v == verb)

    def find(self, verb):
        return [c for c, v in zip(self.all(), self.verbs()) if v == verb]


class Sandbox:
    def __init__(self, calls, default_dir, private_dir, bind, set_live):
        self.calls = calls
        self.default_dir = default_dir      # what TMUX_TMPDIR would name
        self.private_dir = private_dir      # a ~/.tmux/tmp stand-in
        self._bind = bind
        self._set_live = set_live

    def server(self, where, name="default"):
        """Bind a fake server socket under ``where`` ("default"/"private")."""
        root = self.default_dir if where == "default" else self.private_dir
        return self._bind(root, name)

    def live(self, *paths):
        """Which of those sockets a server is actually listening on."""
        self._set_live(paths)


@pytest.fixture(autouse=True)
def _fresh_resolution():
    """The resolved socket is process-global; no test may inherit another's."""
    repl_inject.reset_tmux_socket_cache()
    yield
    repl_inject.reset_tmux_socket_cache()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = os.path.realpath(_temp_root())
    base = os.path.join(root, f"h{os.getpid()}x")
    default_dir = os.path.join(base, "a")
    private_dir = os.path.join(base, "b")
    for d in (default_dir, private_dir):
        os.makedirs(os.path.join(d, f"tmux-{os.getuid()}"), exist_ok=True)
    # TMPDIR names a directory with no sockets in it: a candidate base that
    # contributes nothing, exactly like a fresh non-interactive shell's.
    empty = os.path.join(base, "c")
    os.makedirs(empty, exist_ok=True)

    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "tmux"
    fake.write_text(FAKE_TMUX_SRC.replace("#!/usr/bin/env PYTHON",
                                          f"#!{sys.executable}"))
    fake.chmod(0o755)

    listeners = []

    def bind(root_dir, name):
        path = os.path.join(root_dir, f"tmux-{os.getuid()}", name)
        if len(path) >= SOCK_PATH_LIMIT:
            raise AssertionError(
                f"fake socket path is {len(path)} bytes; macOS allows "
                f"{SOCK_PATH_LIMIT - 1}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(8)
        listeners.append(sock)
        return path

    def set_live(paths):
        monkeypatch.setenv("FAKE_TMUX_LIVE", os.pathsep.join(paths))

    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("FAKE_TMUX_PANES", "%1\tfoo:0.0\t/dev/ttysfake0\n")
    monkeypatch.setenv("TMPDIR", empty)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    # The candidate bases are seams so a test run never scans a real home dir
    # or the shared /tmp, and the fake binary owns every exec.
    monkeypatch.setattr(repl_inject, "TMUX_DEFAULT_TMPDIR", default_dir)
    monkeypatch.setattr(repl_inject, "TMUX_PRIVATE_TMPDIRS", (private_dir,))
    monkeypatch.setattr(repl_inject, "TMUX_BIN", str(fake))
    monkeypatch.setattr(repl_inject, "run_ps", lambda *a, **k: CHAT_PS_OUT)

    try:
        yield Sandbox(Calls(str(log)), default_dir, private_dir, bind, set_live)
    finally:
        for sock in listeners:
            sock.close()
        shutil.rmtree(base, ignore_errors=True)


def noop_sleep(_seconds):
    return None


def real_calls(calls):
    """Every recorded invocation except socket PROBES.

    A probe is by definition ``-S <candidate> list-sessions`` — that ``-S`` is
    the question being asked, not an argument hscc added to a working command.
    Every "hscc added no -S" assertion is about the commands that do work.
    """
    return [c for c in calls.all() if _verb_of(c["argv"]) != "list-sessions"]


# --------------------------------------------------------------------------- #
# MANDATORY: TMUX_TMPDIR removed — the inject still finds the server
# --------------------------------------------------------------------------- #

def test_finds_server_when_tmux_tmpdir_unset(sandbox):
    """The operator's case: no TMUX_TMPDIR, server on a private socket.

    Nothing else can reach that pane: with TMUX_TMPDIR absent, tmux's own rule
    (which the fake honours) points at a directory no server is in.
    """
    sock = sandbox.server("private")
    sandbox.live(sock)
    assert "TMUX_TMPDIR" not in os.environ

    assert repl_inject.tmux_socket_args() == ("-S", sock)
    panes = repl_inject.hermes_panes()
    assert [p["profile"] for p in panes] == ["general"], panes
    # every tmux call hscc made was aimed at the one server that answers
    assert sandbox.calls.all(), "hscc made no tmux call at all"
    for call in sandbox.calls.all():
        assert call["explicit"] and call["socket"] == sock, call


def test_inject_delivers_when_tmux_tmpdir_unset(sandbox):
    """The proof the card asks for: the INJECT reaches the pane."""
    sock = sandbox.server("private")
    sandbox.live(sock)

    receipt = repl_inject.inject("general", "hello", sleep_fn=noop_sleep)

    assert receipt["pane"] == "%1", receipt
    assert receipt["sent_text"] == "hello", receipt
    assert "dry_run" not in receipt, receipt
    sent = sandbox.calls.find("send-keys")
    assert sent, sandbox.calls.all()
    for call in sent:
        assert call["explicit"] and call["socket"] == sock, call
    # and no call was ever made without the socket it needed
    assert all(c["explicit"] for c in sandbox.calls.all()), sandbox.calls.all()


def test_stale_socket_file_is_not_a_server(sandbox):
    """A socket file with nothing listening is tmux's most common decoy.

    Both files sit in the scan path; only one has a listener. Picking the stale
    one would re-create the bug with a more confident-looking error.
    """
    stale = sandbox.server("default", name="stale")
    live = sandbox.server("default")
    sandbox.live(live)

    assert repl_inject.tmux_server_socket() == live
    assert stale not in [s for s in repl_inject.discover_tmux_sockets()
                         if repl_inject.probe_socket(s)]


def test_no_live_server_adds_nothing_and_does_not_lie(sandbox):
    """With no server at all hscc passes no -S: tmux's own error is the true one."""
    sandbox.server("default")          # file exists, nothing listening
    sandbox.live()                     # ... but no server

    assert repl_inject.tmux_server_socket() is None
    assert repl_inject.tmux_socket_args() == ()
    assert repl_inject.hermes_panes() == []
    assert real_calls(sandbox.calls), "hscc made no tmux call at all"
    assert all(not c["explicit"] for c in real_calls(sandbox.calls)), \
        real_calls(sandbox.calls)


def test_discovery_is_probed_not_assumed(sandbox):
    """Discovery offers candidates; only a server that ANSWERS is chosen."""
    a = sandbox.server("default")
    b = sandbox.server("private")
    candidates = repl_inject.discover_tmux_sockets()
    assert set(candidates) == {a, b}, candidates
    sandbox.live(b)
    assert repl_inject.tmux_server_socket() == b
    sandbox.live(a)
    repl_inject.reset_tmux_socket_cache()
    assert repl_inject.tmux_server_socket() == a


def test_two_live_servers_refuse_to_guess(sandbox):
    """Two live servers and no declared socket: refuse, never pick one.

    Swallowing that into "found: none" is the silent failure the card is about,
    so pane discovery must let the refusal through.
    """
    a = sandbox.server("default")
    b = sandbox.server("private")
    sandbox.live(a, b)

    with pytest.raises(InjectError) as ei:
        repl_inject.tmux_server_socket()
    assert ei.value.kind == "ambiguous"
    assert "TMUX_TMPDIR" in ei.value.message

    with pytest.raises(InjectError) as ei:
        repl_inject.hermes_panes()
    assert ei.value.kind == "ambiguous"
    assert not sandbox.calls.find("send-keys"), "injected despite refusing"


def test_resolution_is_cached_and_reprobed_after_failure(sandbox):
    """One probe per process — and a rejected resolution is never reused.

    Caching is what keeps one discovery deciding a whole call sequence (the
    pane listing and the ``send-keys`` after it must not straddle servers). The
    exception is the one that matters operationally: once a real tmux call has
    failed against the resolved socket, the NEXT call must look again, or a
    long-lived daemon keeps aiming at a server that has moved.
    """
    sock = sandbox.server("private")
    sandbox.live(sock)

    panes = repl_inject.hermes_panes()
    repl_inject.is_busy(panes[0]["pane"])
    assert sandbox.calls.count("list-sessions") == 1, \
        "one cached resolution must mean one probe"

    sandbox.live()                      # the server goes away
    assert repl_inject.hermes_panes() == []
    # the call that just failed still used the cached socket; what the failure
    # must buy is a fresh resolution on the call AFTER it
    assert sandbox.calls.find("list-panes")[-1]["socket"] == sock
    repl_inject.hermes_panes()
    assert sandbox.calls.count("list-sessions") >= 2, \
        "the rejected resolution was reused instead of re-probed"


def test_tmux_tmpdir_set_sends_no_socket_argument(sandbox):
    """NO-REGRESSION: declared TMUX_TMPDIR means the argv is byte-identical.

    hscc must not even probe: the environment declares where the server is and
    tmux reads TMUX_TMPDIR itself, so an injected -S would be second-guessing
    an explicit setting (and a test that passed only because of one would be
    testing the fallback, not the declared path).
    """
    sock = sandbox.server("default")
    sandbox.live(sock)
    os.environ["TMUX_TMPDIR"] = sandbox.default_dir
    try:
        assert repl_inject.tmux_socket_args() == ()
        receipt = repl_inject.inject("general", "hello", sleep_fn=noop_sleep)
        assert receipt["pane"] == "%1", receipt
        assert receipt["sent_text"] == "hello", receipt
        assert all(not c["explicit"] for c in real_calls(sandbox.calls)), \
            real_calls(sandbox.calls)
        assert sandbox.calls.count("list-sessions") == 0, \
            "hscc probed even though TMUX_TMPDIR declared the server"
    finally:
        del os.environ["TMUX_TMPDIR"]


def test_tmux_env_var_short_circuits_discovery(sandbox):
    """Inside a pane, $TMUX names the server; discovery must not run at all."""
    sock = sandbox.server("private")
    sandbox.live(sock)
    os.environ["TMUX"] = f"{sock},0,0"
    try:
        assert repl_inject.tmux_socket_args() == ()
        assert sandbox.calls.all() == [], "probed despite being inside a tmux"
    finally:
        del os.environ["TMUX"]


def test_probe_timeout_is_not_fatal(sandbox, monkeypatch):
    """A wedged server is 'not usable', not an exception out of a waker tick."""
    hung = sandbox.server("private")
    sandbox.live(hung)
    monkeypatch.setenv("FAKE_TMUX_HANG", hung)
    monkeypatch.setenv("FAKE_TMUX_HANG_S", "30")
    monkeypatch.setattr(repl_inject, "SOCKET_PROBE_TIMEOUT_S", 0.5)

    assert repl_inject.tmux_server_socket() is None
    assert repl_inject.hermes_panes() == []


def test_missing_binary_is_inject_error_not_crash(sandbox, monkeypatch):
    """No tmux on PATH stays the empty answer hermes_panes() has always given."""
    monkeypatch.setattr(repl_inject, "TMUX_BIN",
                        "/nonexistent/tmux-does-not-exist")
    assert repl_inject.probe_socket("/nonexistent/sock") is False
    assert repl_inject.hermes_panes() == []


def test_real_binary_under_pytest_never_probes(sandbox, monkeypatch):
    """The suite must not poke at the host's live sockets.

    With the REAL tmux binary restored, socket resolution does nothing at all:
    a test owns its tmux (stub run_tmux or fake TMUX_BIN) or it gets none.
    """
    sock = sandbox.server("private")
    sandbox.live(sock)
    monkeypatch.setattr(repl_inject, "TMUX_BIN", "tmux")

    assert repl_inject.tmux_socket_args() == ()
    assert sandbox.calls.all() == [], "discovered/execed with the real binary"


def test_real_binary_under_pytest_does_not_even_scan(sandbox, monkeypatch):
    """The pytest short-circuit must not even LOOK at the filesystem.

    The exec guard would already refuse a probe, so a resolution that skipped
    this short-circuit would still end up passing no `-S` — indistinguishable by
    outcome, which is exactly how a guard becomes dead code. What is not
    acceptable is a suite run walking the operator's directories (and paying a
    real exec per candidate) only to conclude nothing, so the pin is on the scan
    itself.
    """
    scans = []
    monkeypatch.setattr(repl_inject, "discover_tmux_sockets",
                        lambda env=None: scans.append(1) or [])
    monkeypatch.setattr(repl_inject, "TMUX_BIN", "tmux")

    assert repl_inject.tmux_socket_args() == ()
    assert scans == [], "discovery walked the filesystem with the real binary"
