"""Profile-root INTENT gate for hscc-api (t_c443c001).

WHY this file exists. hscc-api writes operator profile files — including
``delete_profile(name, yes=True)``, which removes a whole ``<profile>/``
directory — with no statement anywhere about WHICH profiles root it may write.
``hermes_constants._hermes_root_of`` returns the NATIVE home whenever
``HERMES_HOME`` resolves under ``~/.hermes``, so a process that believes a
scratch home inside the live root sandboxed it writes (and deletes) inside the
operator's real ``~/.hermes/profiles/**`` and exits 0. Three resolvers answer
that question in one process (hermes ``get_profile_dir``, the deliberately
call-time hscc ladder ``routes_profile._hermes_profiles_dir``, and
``rolelib.PROFILES_DIR``) and none of them was cross-checked. The disagreement
is the t_d39c720e incident.

These tests pin the ruled control: an intent ``HSCC_API_PROFILES_ROOT`` captured
ONCE at server start, asserted BEFORE bind (exit 2 on both entry paths, so a
wrong root never serves and never writes), asserted AGAIN at the write choke
point (503 ``profile_root_mismatch``, so call-time drift cannot write), and —
for the incidental chat-path writes — skipped with an ERROR log so a
server-config state can never fail an operator chat.

HOW the two halves differ, and why both are needed:

* ``A*`` and ``C1``/``C2``/``C3``/``C5`` spawn REAL children with ``HOME``
  relocated into ``tmp_path``, so the three resolvers are the real ones and the
  exit codes and fingerprints are filesystem facts. Those are what prove
  ``writer_roots()`` resolves what it claims and that a refused boot really
  writes nothing. (Every child passes an explicit env: the suite-wide guard in
  ``tests/conftest.py`` refuses one that inherits the operator env.)
* ``B*``/``C4``/``C6``-``C9``/``D*`` drive the guard's COMPARISON in-process by
  injecting the three resolver answers — the comparison contract *is* "intent
  vs these three values". The writers they protect are fakes that REALLY write
  files under tmp (and the editor's real ladder seam is retargeted), so
  "nothing written" is a fingerprint fact rather than a mock assertion.

Nothing in this file may write ``~/.hermes``. Every fake tree is under
``tmp_path``; acceptance criterion 5 is the repo-level ``find`` check around the
whole leg.
"""

import http.client
import json
import logging
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import api_server
import profile_root
import routes_orchestrator
import routes_profile
import routes_profiles

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "hscc-api"
INTENT = profile_root.INTENT_ENV


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _same(a, b) -> bool:
    """Path identity, symlinks and all (/var vs /private/var on macOS)."""
    return os.path.realpath(os.path.expanduser(str(a))) == \
        os.path.realpath(os.path.expanduser(str(b)))


def _fingerprint(root):
    """``{relpath: (mtime_ns, size) | "d"}`` for everything under ``root``.

    Used instead of "the mock was not called": the claim under test is that
    nothing was written, which is only a fact about the filesystem.
    """
    root = Path(root)
    if not root.exists():
        return {"__absent__": True}
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel + "/"] = "d"
        else:
            st = path.stat()
            out[rel] = (st.st_mtime_ns, st.st_size)
    return out


def _divergent_lines(msg):
    """Parse ``DIVERGENT <root key> -> <path>`` lines out of a refusal."""
    out = {}
    for line in (msg or "").splitlines():
        m = re.search(r"DIVERGENT (.+?) -> (.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _live_fp(home):
    """Fingerprint of the fake LIVE tree only — ``<home>/.hermes``.

    The daemon's own bookkeeping under ``<home>/.hscc`` (api.log, api.pid) is
    expected to move; a profile file there is not.
    """
    return _fingerprint(Path(home) / ".hermes")


def _seed_fake_home(home):
    """A HOME-shaped fake tree; returns ``(home, live_profiles)``.

    ``live_profiles`` is where an unguarded write lands — the stand-in for the
    operator's ``~/.hermes/profiles``.
    """
    home = Path(home)
    live = home / ".hermes" / "profiles"
    (live / "seed").mkdir(parents=True, exist_ok=True)
    (live / "seed" / "config.yaml").write_text("toolsets: [terminal]\n")
    (live / "seed" / "profile.yaml").write_text("description: seed profile\n")
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    (home / ".hscc" / "api-token").write_text("test-token-not-a-secret\n")
    return home, live


_CHILD_DROP = (
    "HERMES_HOME", "HERMES_REAL_HOME", "PYTHONPATH", INTENT,
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV", "TMPDIR",
    "PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER",
)


def _child_env(home, **extra):
    """Env for a child: PATH+HOME+NO_COLOR+TERM, operator env dropped.

    ``HOME`` is relocated FOR THE CHILD ONLY — the pytest process keeps the real
    HOME, because in-process it is what the three resolvers under test read.
    """
    env = {k: v for k, v in os.environ.items() if k not in _CHILD_DROP}
    env.update({
        "HOME": str(home),
        "TMPDIR": str(Path(home) / "tmp"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HSCC_REPO": str(REPO),
        "HSCC_DIR": str(Path(home) / ".hscc"),
    })
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


def _run_child(tmp_path, name, source, env, args=(), timeout=120):
    script = Path(tmp_path) / name
    script.write_text(source)
    return subprocess.run(
        [sys.executable, str(script), *[str(a) for a in args]],
        cwd=str(API_DIR), env=env, capture_output=True, text=True,
        timeout=timeout)


def _json_line(proc):
    for line in (proc.stdout or "").splitlines():
        if line.startswith("JSON "):
            return json.loads(line[5:])
    raise AssertionError(
        f"no JSON line from child (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_open(port, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# --- in-process resolver injection ---------------------------------------- #

class _Roots:
    """The three resolver answers the guard compares; retargetable per test."""

    def __init__(self):
        self.hermes = None
        self.ladder = None
        self.rolelib = None
        self.rolelib_importable = True

    def set_all(self, root):
        self.hermes = self.ladder = self.rolelib = root
        return self


class _RolelibStub:
    """Stand-in for the ``rolelib`` module with a tmp ``PROFILES_DIR``.

    ``assert_profile_root`` mirrors the real function's contract (first
    positional = the profiles dir the caller intends to write) against this
    stub's own root, so the D5 "reuse rolelib when it is importable" branch runs
    for real instead of being skipped.
    """

    def __init__(self, roots):
        self._roots = roots

    @property
    def PROFILES_DIR(self):
        return str(self._roots.rolelib)

    def assert_profile_root(self, expected, purpose=""):
        if not _same(expected, self._roots.rolelib):
            raise AssertionError(
                f"rolelib stub refused: expected {expected} != "
                f"PROFILES_DIR {self._roots.rolelib}")
        return str(self._roots.rolelib)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Inject the resolver answers and reset the captured intent per test."""
    r = _Roots()
    monkeypatch.setattr(profile_root, "_native_profiles_dir",
                        lambda: None if r.hermes is None else str(r.hermes))
    monkeypatch.setattr(profile_root, "_ladder_profiles_dir",
                        lambda: None if r.ladder is None else str(r.ladder))
    monkeypatch.setattr(profile_root, "_rolelib",
                        lambda: _RolelibStub(r) if r.rolelib_importable else None)
    # The captured intent is process state (D7 captures it once), so a previous
    # test's server must not leak into this one.
    monkeypatch.setattr(profile_root, "_captured", None)
    monkeypatch.setattr(profile_root, "_captured_set", False)
    monkeypatch.delenv(INTENT, raising=False)
    return r


class _FakeHermesProfiles:
    """``hermes_cli.profiles`` stand-in that REALLY writes under a tmp root.

    ``root`` is a callable so a test can move the root mid-test: the writer and
    the guard then both see the drifted answer, which is the drift case.
    """

    def __init__(self, root_getter):
        self._root = root_getter

    @property
    def root(self):
        return Path(self._root())

    def normalize_profile_name(self, name):
        return str(name).strip()

    def validate_profile_name(self, name):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(name)):
            raise ValueError(f"invalid profile name {name!r}")
        return None

    def profile_exists(self, name):
        return (self.root / str(name)).is_dir()

    def get_profile_dir(self, name):
        return self.root / str(name)

    def read_profile_meta(self, path):
        return {"description": "", "description_auto": False}

    def write_profile_meta(self, path, description=None, description_auto=False):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        path.joinpath("profile.yaml").write_text(
            f"description: {description!r}\n"
            f"description_auto: {bool(description_auto)}\n")

    def create_profile(self, name, **kw):
        d = self.root / str(name)
        d.mkdir(parents=True, exist_ok=False)
        d.joinpath("config.yaml").write_text("toolsets: [terminal]\n")
        return d

    def delete_profile(self, name, yes=False):
        d = self.root / str(name)
        shutil.rmtree(d)
        return d

    def rename_profile(self, old, new):
        target = self.root / str(new)
        (self.root / str(old)).rename(target)
        return target

    def list_profiles(self):
        return []


@pytest.fixture
def fake_hermes(monkeypatch):
    """Install the writing fake; ``.root`` is retargetable at any time."""
    holder = {"root": None, "mod": None}

    def install(root):
        holder["root"] = Path(root)
        mod = _FakeHermesProfiles(lambda: holder["root"])
        pkg = types.ModuleType("hermes_cli")
        pkg.__path__ = []
        pkg.profiles = mod
        monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", mod)
        holder["mod"] = mod
        return mod

    holder["install"] = install
    return holder


# --- real loopback HTTP (the same pattern as test_routes_profiles.py) ------- #

def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _req(server, path, body=None, method="GET"):
    host, port = server.server_address[:2]
    token = api_server.load_token(server.ctx.hscc_dir)
    conn = http.client.HTTPConnection(host, port, timeout=8)
    headers = {"Authorization": "Bearer " + token}
    raw = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, (json.loads(data) if data else {})
    except ValueError:
        return resp.status, {"raw": data.decode("utf-8", "replace")}


def _fp(*roots):
    """Merged fingerprint of several PROFILE trees (see ``_fingerprint``).

    Scoped deliberately: the server's own bookkeeping (``~/.hscc/api.log``,
    ``api.pid``, ``activity.json``) is expected to move — the claim under test is
    that no *profile file* was written.
    """
    out = {}
    for root in roots:
        prefix = os.path.realpath(str(root)) + os.sep
        for key, val in _fingerprint(root).items():
            out[prefix + key] = val
    return out


def _seed_profiles_dir(root):
    root = Path(root)
    (root / "seed").mkdir(parents=True, exist_ok=True)
    (root / "seed" / "config.yaml").write_text("toolsets: [terminal]\n")
    (root / "seed" / "profile.yaml").write_text("description: seed\n")
    return root


# --------------------------------------------------------------------------- #
# A — the default path is unchanged (the regression surface)
# --------------------------------------------------------------------------- #

# A driver script lives in tmp_path, so sys.path[0] is tmp_path — the plugin
# dir and the repo root have to be added explicitly (PYTHONPATH stays out of the
# child env, per the suite's isolation rules).
_CHILD_PRELUDE = """
import os, sys
sys.path.insert(0, os.environ["HSCC_REPO"])
sys.path.insert(0, os.path.join(os.environ["HSCC_REPO"], "hscc-api"))
"""

_CHILD_BOOT = """
import json
""" + _CHILD_PRELUDE + """
import api_server, profile_root
srv = api_server.create_server(hscc_dir=os.environ["HSCC_DIR"],
                               addr=("127.0.0.1", 0))
guard = None
try:
    profile_root.guard_write()
except Exception as exc:
    guard = f"{type(exc).__name__}: {exc}"
print("JSON " + json.dumps({
    "intent": profile_root.intent(),
    "ctx_intent": srv.ctx.profile_root_intent,
    "roots": profile_root.writer_roots(),
    "guard": guard,
    "startup": profile_root.startup_line(),
}))
srv.server_close()
sys.exit(0 if guard is None else 3)
"""

_CHILD_WRITE = """
import json
""" + _CHILD_PRELUDE + """
import profile_root, routes_profiles
try:
    routes_profiles._backing_describe_set(sys.argv[1], "written by probe")
except Exception as exc:
    print("JSON " + json.dumps({"error": type(exc).__name__,
                                "code": getattr(exc, "code", None),
                                "msg": str(exc)[:400]}))
    sys.exit(0)
print("JSON " + json.dumps({"wrote": True}))
"""


def test_a1_no_intent_boots_permits_and_uses_relocated_ambient_root(tmp_path):
    """A1: no intent ⇒ boots, ``guard_write()`` permits, and the root really is
    the relocated ambient tree.

    The "today's behaviour is unchanged" case: the guard stays a no-op and the
    writers resolve exactly where an unguarded server resolves today.
    """
    home, live = _seed_fake_home(tmp_path / "home")
    env = _child_env(home)
    proc = _run_child(tmp_path, "boot_a1.py", _CHILD_BOOT, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = _json_line(proc)
    assert data["intent"] is None
    assert data["ctx_intent"] is None              # D7: nothing to carry
    assert data["guard"] is None                   # guard_write() is a no-op
    for key, got in data["roots"].items():
        assert _same(got, live), f"{key} -> {got} != {live}"


def test_a2_startup_line_reports_root_and_intent_on_both_entry_paths(tmp_path):
    """A2: ``hscc-api: profiles root <realpath> (intent: none|<path>)``.

    Checked here on ``serve_main``'s stdout; the daemon entry path logs the same
    line into ``~/.hscc/logs/api.log``, asserted by the C2 child.
    """
    home, live = _seed_fake_home(tmp_path / "home")
    port = _free_port()
    env = _child_env(home)
    proc = subprocess.Popen(
        [sys.executable, "-m", "api_server", "serve", "--bind", "127.0.0.1",
         "--port", str(port)],
        cwd=str(API_DIR), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        listening = proc.stdout.readline()
        assert listening.startswith("hscc-api: listening on 127.0.0.1:"), listening
        line = proc.stdout.readline()
        m = re.match(r"hscc-api: profiles root (.+) \(intent: (.*)\)\s*$", line)
        assert m, line
        assert _same(m.group(1), live), m.group(1)
        assert m.group(2) == "none"
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_a3_profile_mode_hermes_home_boots_permits_and_writes(tmp_path):
    """A3: ``HERMES_HOME=<live>/profiles/<name>`` (legit profile mode) ⇒ boots,
    permits, and the write really lands in the fake live tree.

    Profile mode is honoured, not blocked — which is precisely why a
    resolver-level guard was ruled out (option 3): these bytes mean "profile
    mode" and "throwaway sandbox" equally, and only the caller knows which.
    """
    home, live = _seed_fake_home(tmp_path / "home")
    env = _child_env(home, HERMES_HOME=str(live / "seed"))
    proc = _run_child(tmp_path, "boot_a3.py", _CHILD_BOOT, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = _json_line(proc)
    assert data["guard"] is None
    assert _same(data["roots"]["hermes get_profile_dir"], live)

    before = _fingerprint(home)
    proc = _run_child(tmp_path, "write_a3.py", _CHILD_WRITE, env, args=["seed"])
    assert _json_line(proc) == {"wrote": True}
    assert _fingerprint(home) != before
    assert "written by probe" in (live / "seed" / "profile.yaml").read_text()


def test_a4_scratch_home_inside_live_root_default_path_unchanged(tmp_path):
    """A4: ``HERMES_HOME=<live>/scratch/.hermes`` with NO intent ⇒ the write
    still lands in the live tree — the default path is not silently changed.

    This pins the hazard as *intended* behaviour when no intent is asked for.
    """
    home, live = _seed_fake_home(tmp_path / "home")
    scratch = live / "scratch" / ".hermes"
    scratch.mkdir(parents=True)
    env = _child_env(home, HERMES_HOME=str(scratch))
    proc = _run_child(tmp_path, "boot_a4.py", _CHILD_BOOT, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _json_line(proc)["guard"] is None

    before = _fingerprint(home)
    proc = _run_child(tmp_path, "write_a4.py", _CHILD_WRITE, env, args=["seed"])
    assert _json_line(proc) == {"wrote": True}
    after = _fingerprint(home)
    changed = [k for k, v in after.items() if v != before.get(k)]
    assert changed, "the write went nowhere"
    # ...and what changed is the LIVE tree, not the scratch tree.
    assert all(k.startswith(".hermes/profiles/") for k in changed), changed
    assert not list((live / "scratch").rglob("profile.yaml"))


# --------------------------------------------------------------------------- #
# B — intent matches
# --------------------------------------------------------------------------- #

def test_b1_intent_matching_every_root_boots_carries_and_permits(
        tmp_path, monkeypatch, roots, fake_hermes):
    """B1: intent == every resolved root ⇒ boots, ``ctx`` carries it, the guard
    permits, and the write lands."""
    live = _seed_profiles_dir(tmp_path / "intended")
    roots.set_all(live)
    fake_hermes["install"](live)
    monkeypatch.setenv(INTENT, str(live))
    server = _serve(api_server.create_server(hscc_dir=str(tmp_path / "hscc"),
                                             addr=("127.0.0.1", 0)))
    try:
        assert server.ctx.profile_root_intent == os.path.realpath(str(live))
        profile_root.guard_write()                 # no raise
        status, payload = _req(server, "/v1/profiles/seed/describe",
                               {"description": "kept", "confirm": True},
                               method="POST")
        assert status == 200, (status, payload)
        assert "kept" in (live / "seed" / "profile.yaml").read_text()
        assert profile_root.startup_line().endswith(
            f"(intent: {os.path.realpath(str(live))})")
    finally:
        server.shutdown()
        server.server_close()


def test_b2_trailing_slash_and_home_prefix_normalise(tmp_path, monkeypatch,
                                                     roots):
    """B2: a trailing ``/`` and a ``~``-prefixed value compare as a match."""
    monkeypatch.setenv("HOME", str(tmp_path))
    live = tmp_path / ".hermes" / "profiles"
    live.mkdir(parents=True)
    roots.set_all(live)

    monkeypatch.setenv(INTENT, str(live) + "/")
    assert profile_root.assert_boot_intent() == os.path.realpath(str(live))

    monkeypatch.setenv(INTENT, "~/.hermes/profiles/")
    assert profile_root.assert_boot_intent() == os.path.realpath(str(live))


def test_b3_startup_line_reports_the_configured_intent(tmp_path, monkeypatch,
                                                       roots):
    """B3: intent set ⇒ the boot line carries ``(intent: <path>)``."""
    live = tmp_path / "live"
    live.mkdir()
    roots.set_all(live)
    monkeypatch.setenv(INTENT, str(live))
    profile_root.capture(profile_root.assert_boot_intent())
    line = profile_root.startup_line()
    assert line == (f"hscc-api: profiles root {os.path.realpath(str(live))} "
                    f"(intent: {os.path.realpath(str(live))})")
    assert "intent: none" not in line


# --------------------------------------------------------------------------- #
# C — startup refusal
# --------------------------------------------------------------------------- #

def test_c1_child_boot_refusal_exit2_nothing_bound_written(tmp_path):
    """C1: divergent intent at boot ⇒ exit exactly 2, the port is never bound,
    the fake live tree is byte-identical, and stderr names every root plus the
    env, in the contractual order."""
    home, live = _seed_fake_home(tmp_path / "home")
    port = _free_port()
    before = _fingerprint(home)
    wanted = tmp_path / "elsewhere" / "profiles"
    env = _child_env(home, HSCC_API_PROFILES_ROOT=str(wanted))
    proc = subprocess.run(
        [sys.executable, "-m", "api_server", "serve", "--bind", "127.0.0.1",
         "--port", str(port)],
        cwd=str(API_DIR), env=env, capture_output=True, text=True, timeout=120)

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not _port_open(port), "a refused boot must never bind"
    assert _fingerprint(home) == before, "the refusal itself wrote the tree"

    err = proc.stderr
    assert "refusing to start: profile root intent not honoured" in err
    assert "requested profiles root:" in err and str(wanted) in err
    assert "env: HOME=" in err and "HERMES_HOME=" in err and "HERMES_REAL_HOME=" in err
    divergent = _divergent_lines(err)
    for key in (profile_root.KEY_HERMES, profile_root.KEY_LADDER,
                profile_root.KEY_ROLELIB):
        assert key in divergent, f"{key} not named: {sorted(divergent)}"
        assert _same(divergent[key], live), divergent[key]
    assert "_hermes_root_of" in err
    order = [err.index(s) for s in
             ("requested profiles root:", "env: HOME=", "DIVERGENT ",
              "_hermes_root_of")]
    assert order == sorted(order), f"message order is contract: {order}"


def test_c2_daemon_serve_refusal_exit2_marks_pid_stopped_and_logs(tmp_path):
    """C2: the daemon entry path (``api_cli._serve``) refuses too — exit 2, the
    PID file is marked stopped, the API log carries the whole divergence report
    (and, on a successful boot, the same startup line A2 pins)."""
    home, _live = _seed_fake_home(tmp_path / "home")
    hscc = home / ".hscc"
    pid_file = hscc / "api.pid"
    pid_file.write_text("424242\n")               # the daemon writes this first
    api_log = hscc / "api.log"                      # api_cli.API_LOG_FILE
    port = _free_port()
    before = _live_fp(home)

    src = """
""" + _CHILD_PRELUDE + """
from hscc_daemon import api_cli
api = api_cli._load_api_server()
api_cli._serve(api, "127.0.0.1", int(sys.argv[1]))
"""
    env = _child_env(home, HSCC_API_PROFILES_ROOT=str(tmp_path / "elsewhere"))
    proc = _run_child(tmp_path, "serve_c2.py", src, env, args=[port])

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert not pid_file.exists(), "a refused server must not leave a live PID file"
    assert not _port_open(port)
    # The refusal legitimately writes its own log + PID bookkeeping under
    # ~/.hscc; what it must NOT touch is the profile tree.
    assert _live_fp(home) == before

    text = api_log.read_text()
    assert "profile root intent not honoured" in text
    assert "ERROR" in text
    divergent = _divergent_lines(text)
    for key in (profile_root.KEY_HERMES, profile_root.KEY_LADDER,
                profile_root.KEY_ROLELIB):
        assert key in divergent, f"{key} missing from api.log: {text[:2000]}"


def test_c3_control_without_intent_the_same_write_lands(tmp_path):
    """C3 (control): the same child and tree WITHOUT an intent writes for real —
    and the same write under a divergent intent does not.

    This pairing is what makes every "nothing written" assertion above
    non-vacuous: same input, only the intent differs.
    """
    home, live = _seed_fake_home(tmp_path / "home")
    env = _child_env(home)
    before = _fingerprint(home)
    proc = _run_child(tmp_path, "write_c3.py", _CHILD_WRITE, env, args=["seed"])
    assert _json_line(proc) == {"wrote": True}
    assert _fingerprint(home) != before
    assert "written by probe" in (live / "seed" / "profile.yaml").read_text()

    (live / "seed" / "profile.yaml").write_text("description: seed profile\n")
    env2 = _child_env(home,
                      HSCC_API_PROFILES_ROOT=str(tmp_path / "elsewhere/profiles"))
    before2 = _fingerprint(home)
    proc = _run_child(tmp_path, "write_c3b.py", _CHILD_WRITE, env2, args=["seed"])
    data = _json_line(proc)
    assert data["error"] == "ApiError", data
    assert "profile-root intent not honoured" in data["msg"], data
    assert _fingerprint(home) == before2


def test_c4_two_root_divergence_names_every_divergent_root(tmp_path, monkeypatch,
                                                           roots):
    """C4: intent == the hermes root while the LADDER differs ⇒ refused, and the
    message names each divergent root by its ``writer_roots()`` key.

    The ladder is the root ``rolelib.assert_profile_root`` cannot see, which is
    why the boot assert is not delegated wholesale to rolelib (D4/D5).
    """
    native = tmp_path / "live"
    native.mkdir()
    other = tmp_path / "ladder-grew-a-second-root"
    other.mkdir()
    roots.hermes = native
    roots.ladder = other
    roots.rolelib = other
    monkeypatch.setenv(INTENT, str(native))

    with pytest.raises(profile_root.ProfileRootRefusal) as exc:
        profile_root.assert_boot_intent()
    msg = str(exc.value)
    assert f"requested profiles root: {os.path.realpath(str(native))}" in msg
    divergent = _divergent_lines(msg)
    assert set(divergent) == {profile_root.KEY_LADDER, profile_root.KEY_ROLELIB}, \
        divergent
    assert _same(divergent[profile_root.KEY_LADDER], other)
    assert _same(divergent[profile_root.KEY_ROLELIB], other)


def test_c5_relative_intent_refused_as_exit2(tmp_path):
    """C5: a non-empty RELATIVE value ⇒ exit code 2 + "must be an absolute
    path" — never silently resolved against a mystery cwd."""
    home, _ = _seed_fake_home(tmp_path / "home")
    before = _fingerprint(home)
    env = _child_env(home, HSCC_API_PROFILES_ROOT="relative/profiles")
    proc = subprocess.run(
        [sys.executable, "-m", "api_server", "serve", "--bind", "127.0.0.1",
         "--port", str(_free_port())],
        cwd=str(API_DIR), env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    assert "must be an absolute path" in proc.stderr
    assert _fingerprint(home) == before


def test_c6_empty_or_whitespace_intent_is_unset(tmp_path, monkeypatch, roots):
    """C6: ``VAR=`` and whitespace count as UNSET (a plist ``VAR=`` must not
    brick the server)."""
    live = tmp_path / "live"
    live.mkdir()
    roots.set_all(live)
    for value in ("", "   ", "\t"):
        monkeypatch.setenv(INTENT, value)
        assert profile_root.intent() is None
        assert profile_root.assert_boot_intent() is None
        profile_root.guard_write()               # no raise


def test_c7_rolelib_unimportable_fallback_refuses(tmp_path, monkeypatch, roots):
    """C7: hscc-roles unimportable + intent set + a root diverges ⇒ the in-module
    fallback refuses (the guard can never be lost by a thin interpreter)."""
    live = tmp_path / "live"
    live.mkdir()
    roots.set_all(live)
    roots.rolelib_importable = False             # _rolelib() -> None
    monkeypatch.setenv(INTENT, str(tmp_path / "elsewhere"))
    assert profile_root._rolelib() is None
    with pytest.raises(profile_root.ProfileRootRefusal) as exc:
        profile_root.assert_boot_intent()
    divergent = _divergent_lines(str(exc.value))
    assert _same(divergent[profile_root.KEY_HERMES], live)
    assert profile_root.KEY_ROLELIB not in divergent   # unresolvable: omitted


def test_c8_hermes_cli_unimportable_uses_ladder_and_rolelib(tmp_path,
                                                            monkeypatch, roots):
    """C8: hermes_cli unimportable + intent == the two roots that DO answer ⇒
    boots (D7: unresolvable roots do not block a matching server)."""
    live = tmp_path / "live"
    live.mkdir()
    roots.hermes = None                          # resolver cannot answer
    roots.ladder = live
    roots.rolelib = live
    monkeypatch.setenv(INTENT, str(live))
    assert profile_root.assert_boot_intent() == os.path.realpath(str(live))
    profile_root.capture(profile_root.assert_boot_intent())
    profile_root.guard_write()                   # permitted


def test_c9_no_root_resolvable_while_intent_asked_refuses(tmp_path, monkeypatch,
                                                          roots):
    """C9: an intent asked for and ZERO roots resolvable ⇒ refuse (fail closed:
    an unanswerable resolver is not a match)."""
    roots.hermes = roots.ladder = roots.rolelib = None
    roots.rolelib_importable = False
    monkeypatch.setenv(INTENT, str(tmp_path / "wanted"))
    assert profile_root.writer_roots() == {
        profile_root.KEY_HERMES: None, profile_root.KEY_LADDER: None,
        profile_root.KEY_ROLELIB: None}
    with pytest.raises(profile_root.ProfileRootRefusal) as exc:
        profile_root.assert_boot_intent()
    assert "not honoured" in str(exc.value)
    # Same blindness with NO intent asked is a no-op — A1's world.
    monkeypatch.delenv(INTENT)
    assert profile_root.assert_boot_intent() is None


# --------------------------------------------------------------------------- #
# D — the request-time choke point
# --------------------------------------------------------------------------- #

def _drifted_server(roots, monkeypatch, tmp_path, fake_hermes, *, intent):
    """Boot a server whose three roots agree, then move ALL of them.

    The move is the drift the ladder permits by design; ``intent`` is the only
    difference between a refusal and the control, so the control is a true
    pairing. The editor's writer is moved too, through the real ladder seam
    (``routes_profile.hermes_root_dir``), so the write really WOULD land in the
    unintended tree.

    Returns ``(server, intended_root, drifted_root)``.
    """
    intended = _seed_profiles_dir(tmp_path / "intended" / "profiles")
    drifted_home = tmp_path / "drifted"
    drifted = _seed_profiles_dir(drifted_home / "profiles")
    roots.set_all(intended)
    fake_hermes["root"] = intended
    fake_hermes["install"](intended)
    monkeypatch.setattr(routes_profile, "hermes_root_dir",
                        lambda: drifted_home)
    if intent:
        monkeypatch.setenv(INTENT, str(intended))
    else:
        # The control must not inherit a previous iteration's intent: monkeypatch
        # restores at test end, not between loop passes.
        monkeypatch.delenv(INTENT, raising=False)
    server = _serve(api_server.create_server(hscc_dir=str(tmp_path / "hscc"),
                                             addr=("127.0.0.1", 0)))
    # Boot asserted against the intended root; now every resolver re-points.
    fake_hermes["root"] = drifted
    roots.set_all(drifted)
    return server, intended, drifted


@pytest.mark.parametrize("site,path,body", [
    pytest.param("create", "/v1/profiles/create",
                 {"name": "brand-new", "confirm": True}, id="site3-create"),
    pytest.param("delete", "/v1/profiles/seed/delete",
                 {"confirm": True}, id="site4-delete"),
    pytest.param("rename", "/v1/profiles/seed/rename",
                 {"new_name": "renamed", "confirm": True}, id="site5-rename"),
    pytest.param("describe", "/v1/profiles/seed/describe",
                 {"description": "drift", "confirm": True},
                 id="site6-describe-set"),
    pytest.param("editor", "/v1/profile/editor/seed",
                 {"toolsets": ["terminal", "web"], "confirm": True},
                 id="site7-editor-update"),
])
def test_d2_every_mutating_site_503_and_nothing_written(
        roots, monkeypatch, tmp_path, fake_hermes, site, path, body):
    """D2: every mutating site answers 503 ``profile_root_mismatch`` under
    divergence and writes NOTHING — and, as the control, really writes the same
    request when no intent was configured.

    A wrong status would itself be the bug: these requests' purpose IS to change
    a profile (D9), so a server-config state must surface as a refusal, not a
    200.
    """
    for intent in (True, False):
        server, intended, drifted = _drifted_server(
            roots, monkeypatch, tmp_path, fake_hermes, intent=intent)
        try:
            before = _fp(intended, drifted)
            status, payload = _req(server, path, body, method="POST")
            after = _fp(intended, drifted)
            if intent:
                assert status == 503, (site, status, payload)
                assert payload["error"]["code"] == "profile_root_mismatch"
                assert "requested profiles root" in payload["error"]["message"]
                assert after == before, f"{site}: wrote under divergence"
            else:
                # Control. If this branch ever stops changing the tree, the
                # assertion above has gone vacuous.
                assert status in (200, 201), (site, status, payload)
                assert after != before, f"{site}: control wrote nothing"
        finally:
            server.shutdown()
            server.server_close()


def test_d1_ladder_drift_after_boot_blocks_write_with_503(
        roots, monkeypatch, tmp_path, fake_hermes):
    """D1: boot matched, then the ladder re-points ⇒ the write is refused with
    503 and nothing is written to either tree.

    Boot-time assertion alone would NOT be enough here: ``routes_profile``
    resolves at call time on purpose, so the root that matched at boot is not
    the root this request would write.
    """
    server, intended, drifted = _drifted_server(
        roots, monkeypatch, tmp_path, fake_hermes, intent=True)
    try:
        assert server.ctx.profile_root_intent == os.path.realpath(str(intended))
        before = _fp(intended, drifted)
        status, payload = _req(server, "/v1/profiles/seed/describe",
                               {"description": "should not land",
                                "confirm": True}, method="POST")
        assert status == 503, (status, payload)
        assert payload["error"]["code"] == "profile_root_mismatch"
        assert _fp(intended, drifted) == before
        assert "should not land" not in (intended / "seed" / "profile.yaml").read_text()
        assert "should not land" not in (drifted / "seed" / "profile.yaml").read_text()
    finally:
        server.shutdown()
        server.server_close()


def test_d3_confirm_gate_still_precedes_the_guard(roots, monkeypatch, tmp_path,
                                                  fake_hermes):
    """D3: the confirm gate is checked first — a divergent server still answers
    409 ``confirm_required``, not 503, for an unconfirmed request."""
    server, _intended, _drifted = _drifted_server(
        roots, monkeypatch, tmp_path, fake_hermes, intent=True)
    try:
        status, payload = _req(server, "/v1/profiles/create",
                               {"name": "brand-new"}, method="POST")
        assert status == 409, (status, payload)
        assert payload["error"]["code"] == "confirm_required"
    finally:
        server.shutdown()
        server.server_close()


def test_d4_reads_stay_200_under_divergence(roots, monkeypatch, tmp_path,
                                            fake_hermes):
    """D4: a GET under divergence is still 200 — the guard is on writes only
    (D11: a wrong-root read degrades to an honest empty list)."""
    server, _intended, _drifted = _drifted_server(
        roots, monkeypatch, tmp_path, fake_hermes, intent=True)
    try:
        status, payload = _req(server, "/v1/profiles/list")
        assert status == 200, (status, payload)
    finally:
        server.shutdown()
        server.server_close()


def _fake_hermes_config_modules(monkeypatch, profiles_mod):
    """Inject ``hermes_cli`` + a recording ``utils.atomic_yaml_write``."""
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    pkg.profiles = profiles_mod
    wrote = []
    utils = types.ModuleType("utils")

    def _atomic(path, data, **kw):
        wrote.append(str(path))
        Path(path).write_text(json.dumps(data))

    utils.atomic_yaml_write = _atomic
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles_mod)
    monkeypatch.setitem(sys.modules, "utils", utils)
    return wrote


def test_d5_incidental_compaction_threshold_write_is_skipped_not_500(
        tmp_path, monkeypatch, roots, caplog):
    """D5: the incidental write on the chat / session-guard path skips + logs at
    ERROR instead of failing the turn (D9).

    ``_ensure_compaction_threshold`` is documented FAIL-SAFE — a raise here is
    what would surface in an operator's chat, which is the worse bug. Its caller
    treats ``None`` as "nothing done" and the turn continues, so this pins: no
    raise, nothing written, the divergence explained at ERROR. The control (no
    intent captured) shows the same call really writing.
    """
    live = _seed_profiles_dir(tmp_path / "live" / "profiles")
    roots.set_all(live)
    monkeypatch.setenv(INTENT, str(live))
    profile_root.capture(profile_root.assert_boot_intent())
    roots.set_all(tmp_path / "drifted")          # the resolvers re-point

    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod.normalize_profile_name = lambda name: str(name)
    profiles_mod.validate_profile_name = lambda name: None
    profiles_mod.profile_exists = lambda name: True
    profiles_mod.get_profile_dir = lambda name: live / str(name)
    wrote = _fake_hermes_config_modules(monkeypatch, profiles_mod)

    before = _fingerprint(live)
    with caplog.at_level(logging.ERROR, logger="hscc-api"):
        result = routes_orchestrator._ensure_compaction_threshold("seed")

    assert result is None, "the incidental write must be skipped, not raised"
    assert wrote == [], "atomic_yaml_write was reached"
    assert _fingerprint(live) == before
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "skipping compression.threshold_tokens" in text
    assert "requested profiles root" in text and "DIVERGENT" in text

    # Control: same call, same tree, no intent captured ⇒ the write happens.
    profile_root.capture(None)
    before2 = _fingerprint(live)
    result = routes_orchestrator._ensure_compaction_threshold("seed")
    assert result and result["set"] is True
    assert wrote == [str(live / "seed" / "config.yaml")]
    assert _fingerprint(live) != before2


def test_d5b_role_profile_sweep_skips_once_and_writes_nothing(
        tmp_path, monkeypatch, roots, caplog):
    """D5b: the ``_ensure_role_profiles`` sweep (site 2 = site 1 x N) exits
    fail-safe with ONE divergence line, not one per profile."""
    live = tmp_path / "live" / "profiles"
    roster = ["architect", "backend-engineer", "reviewer", "writer", "default"]
    for name in roster:
        (live / name).mkdir(parents=True)
        (live / name / "config.yaml").write_text("toolsets: [terminal]\n")
    roots.set_all(live)
    monkeypatch.setenv(INTENT, str(live))
    profile_root.capture(profile_root.assert_boot_intent())
    roots.set_all(tmp_path / "drifted")

    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod.normalize_profile_name = lambda name: str(name)
    profiles_mod.validate_profile_name = lambda name: None
    profiles_mod.profile_exists = lambda name: name in roster
    profiles_mod.get_profile_dir = lambda name: live / name
    profiles_mod.list_profiles = lambda: [
        types.SimpleNamespace(name=n, path=live / n) for n in roster]
    wrote = _fake_hermes_config_modules(monkeypatch, profiles_mod)

    before = _fingerprint(live)
    with caplog.at_level(logging.ERROR, logger="hscc-api"):
        result = routes_orchestrator._ensure_role_profiles()

    assert result is None, "the sweep must fail safe, not report a partial ensure"
    assert wrote == []
    assert _fingerprint(live) == before
    lines = [r.getMessage() for r in caplog.records if "DIVERGENT" in r.getMessage()]
    assert len(lines) == 1, f"one divergence line per sweep, got {len(lines)}"

    # Control: with no intent captured the same sweep writes every profile.
    profile_root.capture(None)
    result = routes_orchestrator._ensure_role_profiles()
    assert result and result["role_profiles"] == len(roster)
    assert len(wrote) == len(roster), wrote


def test_d6_projects_new_refuses_before_site8_writes(tmp_path, monkeypatch,
                                                     roots):
    """D6: ``POST /v1/projects/new`` under divergence is NON-2xx and site 8
    (``ensure_orchestrator`` -> ``rolelib.PROFILES_DIR``) writes nothing.

    Non-2xx matters: ``handle_create`` reports a failed STEP as a 202 "created
    partially", which would let a mis-rooted server look like it half-succeeded.
    The guard also sits before step 1, so a refusal does not leave a project
    repo/board/registry behind with no orchestrator.
    """
    import routes_bootstrap

    live = tmp_path / "live" / "profiles"
    live.mkdir(parents=True)
    drifted = tmp_path / "drifted"
    drifted.mkdir()
    roots.set_all(live)
    monkeypatch.setenv(INTENT, str(live))

    calls = []
    orch = types.ModuleType("orchestrators")

    def ensure_orchestrator(name, base_identity=None, path=None):
        calls.append(name)
        target = Path(profile_root._rolelib().PROFILES_DIR) / f"{name}-orch"
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text("toolsets: [terminal]\n")
        return {"changed": True}

    orch.ensure_orchestrator = ensure_orchestrator
    monkeypatch.setitem(sys.modules, "orchestrators", orch)
    created = []
    monkeypatch.setattr(routes_bootstrap, "_backing_create_project",
                        lambda *a, **kw: created.append(a) or
                        {"ok": True, "steps": ["repo"]})
    monkeypatch.setattr(routes_bootstrap, "_backing_plan",
                        lambda *a, **kw: {"ready": True, "collisions": {},
                                          "steps": ["repo"]})
    monkeypatch.setattr(routes_bootstrap, "_backing_ensure_session",
                        lambda *a, **kw: {"created_session": "s1"})
    server = _serve(api_server.create_server(hscc_dir=str(tmp_path / "hscc"),
                                             addr=("127.0.0.1", 0)))
    try:
        assert server.ctx.profile_root_intent == os.path.realpath(str(live))
        # Boot matched; now the root site 8 writes through re-points (D4: the
        # rolelib leg is exactly the one the CLI guard already owns).
        roots.rolelib = drifted
        before = _fp(live, drifted)
        status, payload = _req(server, "/v1/projects/new",
                               {"name": "acme", "repo": "/tmp/acme",
                                "confirm": True}, method="POST")
        assert status == 503, (status, payload)
        assert payload["error"]["code"] == "profile_root_mismatch"
        assert calls == [], "ensure_orchestrator ran despite the divergence"
        assert created == [], "the handler mutated before the guard ran"
        assert _fp(live, drifted) == before

        # Control: the same POST with no intent captured reaches site 8.
        profile_root.capture(None)
        roots.rolelib = live
        status, payload = _req(server, "/v1/projects/new",
                               {"name": "acme2", "repo": "/tmp/acme2",
                                "confirm": True}, method="POST")
        assert calls == ["acme2"], (status, payload, calls)
        assert (live / "acme2-orch" / "config.yaml").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_d7_guard_is_first_statement_of_every_mutating_backing():
    """D7 (source pin): the guard is the FIRST statement of each mutating
    backing, so "the guard runs before anything is removed" is a fact about the
    code, not about a test that happened to observe it.

    Site 4 (``delete_profile(name, yes=True)``) is the reason: a guard that ran
    after the first filesystem touch would still have deleted a profile.
    """
    import ast
    import inspect
    import routes_bootstrap
    import routes_profile_editor

    targets = [
        (routes_profiles, "_backing_create"),
        (routes_profiles, "_backing_delete"),
        (routes_profiles, "_backing_rename"),
        (routes_profiles, "_backing_describe_set"),
        (routes_profile_editor, "_backing_update"),
        (routes_bootstrap, "_backing_ensure_orchestrator"),
    ]
    for mod, name in targets:
        fn = getattr(mod, name)
        body = ast.parse(inspect.getsource(fn).lstrip()).body[0].body
        first = next((s for s in body
                      if not (isinstance(s, ast.Expr)
                              and isinstance(s.value, ast.Constant))), None)
        assert first is not None and isinstance(first, ast.Expr), name
        call = first.value
        assert isinstance(call, ast.Call), name
        src = ast.unparse(call)
        assert src == "profile_root.guard_write()", f"{name}: first stmt is {src}"


# --------------------------------------------------------------------------- #
# E — the intent channel stays env-only
# --------------------------------------------------------------------------- #

def test_e1_generated_launchd_unit_gains_neither_var():
    """E1: the generated launchd unit gains NEITHER ``HERMES_HOME`` nor
    ``HSCC_API_PROFILES_ROOT`` (D10: a hand-added key is honoured, the generator
    never adds one), and no argv carries it (D2)."""
    sys.path.insert(0, str(REPO))
    from hscc_daemon import api_launchd

    plist = api_launchd.build_api_plist("/p", "/d")
    assert set(plist["EnvironmentVariables"]) == {"HOME", "PATH"}
    assert "HSCC_API_PROFILES_ROOT" not in plist["EnvironmentVariables"]
    assert "HERMES_HOME" not in plist["EnvironmentVariables"]
    # D2: the intent is not reachable from serve's argv either — and serve's
    # argv is baked into this unit, which is why a flag there was rejected.
    blob = plistlib.dumps(plist).decode("utf-8") + Path(
        api_launchd.__file__).read_text()
    assert "HSCC_API_PROFILES_ROOT" not in blob
