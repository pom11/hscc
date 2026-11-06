"""``verify_chat_roundtrip.py`` must survive being run the way ``verify`` runs it.

What shipped, verbatim (reproduced 2026-11-04 on the repo venv interpreter, which
HAS rich — so rich was never the blocker here)::

    $ python <repo>/hscc_daemon/verify_chat_roundtrip.py --json   # cwd=/private/tmp
    Traceback (most recent call last):
      File "…/verify_chat_roundtrip.py", line 49, in <module>
        from hscc_daemon import cli_theme as theme
    ModuleNotFoundError: No module named 'hscc_daemon'
    exit code 1

The file carried a ``sys.path`` bootstrap and it was DEAD CODE: line 49 imported
``hscc_daemon`` and line 52 inserted the package root — the insert ran AFTER the
import it was written to protect. A bare script path puts only the script's OWN
directory (``hscc_daemon/``) on ``sys.path``, never the parent that contains the
package, so ``hscc verify --chat``'s deep check died before it read a single
config value — the identical fault that made the ``api_routes`` row lie in 2.15.2
(t_ef36fe71, fixed there for ``api_route_sweep.py``).

Three invariants are pinned here:

1. STANDALONE RUNNABILITY. Run it the way ``verify.run_chat_roundtrip`` and the
   installed keg do — a bare script path, from a directory that is NOT the repo,
   with a scrubbed HOME and no ``hscc`` on PATH — and it must not die with
   ``ModuleNotFoundError``. The safe assertion is the preconditions path: with no
   API host and no token it exits 2 with a machine-readable error. It never
   reaches the POST, so NO real chat prompt is ever fired at the live model.
2. THE RENDERER IS OPTIONAL. A missing ``cli_theme``/``rich`` costs the human
   view its colouring and nothing else: no traceback, and the exit code stays
   the round trip's own verdict.
3. NO REGRESSION OF THE ORDERING. An AST check over the whole repo fails the
   suite if any bare-runnable module ever again imports ``hscc_daemon`` before
   the ``sys.path`` bootstrap meant to protect it.

Everything runs against a FAKE keg in tmp_path; the live API, the operator's
``~/.hscc`` and the fleet are never touched.
"""

import ast
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUNDTRIP_SRC = os.path.join(REPO, "hscc_daemon", "verify_chat_roundtrip.py")
CLI_THEME_SRC = os.path.join(REPO, "hscc_daemon", "cli_theme.py")

# An interpreter-level failure inside the module the human view depends on —
# the shape of "the interpreter running us has no rich" without depending on
# which interpreter runs the suite.
_BROKEN_THEME = 'raise ImportError("simulated: no rich in this interpreter")\n'

# A result dict complete enough for ALL THREE render branches (0/1/2), so one
# payload can drive every exit code in the verdict test.
_ANY_RESULT = {
    "error": "no API host or token (is `hscc api status` running?)",
    "http": 500,
    "job_id": "job_pin",
    "status": "done",
    "elapsed": 1.5,
    "reply": "pong",
    "tokens_before": 100,
    "tokens_after": 115,
    "delta": 15,
}


def _fake_keg(tmp_path, theme=None, name="keg"):
    """Lay out a minimal *installed* tree; return (root, script_path).

    Mirrors the keg and the deployed plugin dir: ``<root>/hscc_daemon/`` holds
    the round trip as a plain file with the package beside it. ``theme`` is the
    text written into the fake ``cli_theme.py``; pass None to leave it out.
    """
    root = tmp_path / name
    pkg = root / "hscc_daemon"
    pkg.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROUNDTRIP_SRC, pkg / "verify_chat_roundtrip.py")
    (pkg / "__init__.py").write_text("")
    if theme is not None:
        (pkg / "cli_theme.py").write_text(theme)
    return root, str(pkg / "verify_chat_roundtrip.py")


def _run(script, argv, cwd, home, extra_path):
    """Run the script as a BARE SCRIPT PATH — the verify/operator invocation.

    ``PYTHONPATH`` is dropped on purpose: inheriting the test runner's path
    would hand the child the real repo for free and mask the very bug this file
    exists to pin.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join([str(extra_path), "/usr/bin", "/bin"])
    return subprocess.run([sys.executable, script] + argv, cwd=str(cwd),
                          capture_output=True, text=True, timeout=180, env=env)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Scrub HOME + PATH so no path in the script can reach live state.

    No ``hscc`` on PATH means ``api_base()``'s ``hscc api status`` fallback fails
    the same way it would on a box with no API running — which is the exit-2
    precondition we assert on, and the reason this test can never POST a prompt.
    """
    (tmp_path / "emptybin").mkdir(exist_ok=True)
    home = tmp_path / "fakehome"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    return home


# ── 1. standalone runnability (the actual repro) ─────────────────────────────

def test_bare_script_path_never_dies_with_module_not_found(tmp_path, _hermetic):
    """The operator's manual repro: a bare script path must import its package.

    Run from ``/`` — a directory that is not the repo — against a fake keg that
    holds nothing but this script. The package root has to resolve because the
    script bootstraps ``sys.path`` itself, not because whoever invoked it was
    kind enough to.
    """
    _, script = _fake_keg(tmp_path, theme="")
    cp = _run(script, ["--json"], cwd="/", home=_hermetic,
              extra_path=tmp_path / "emptybin")
    combined = cp.stdout + cp.stderr
    assert "No module named" not in combined, combined
    assert "Traceback" not in combined, combined


def test_preconditions_path_is_machine_readable_and_exits_2(tmp_path, _hermetic):
    """No API host + no token ⇒ exit 2 with a parseable error, never a crash.

    This is the SAFE assertion the card asked for: the script stops before the
    POST, so nothing is ever fired at the live orchestrator, and verify's
    ``json.loads(stdout).get("error")`` fallback gets real JSON instead of a
    traceback.
    """
    _, script = _fake_keg(tmp_path, theme="")
    cp = _run(script, ["--json"], cwd="/", home=_hermetic,
              extra_path=tmp_path / "emptybin")
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 2, combined
    payload = json.loads(cp.stdout)  # raises (and fails) if this is a traceback
    assert "no API host or token" in payload["error"], payload


def test_bare_script_run_can_import_the_real_theme(tmp_path, _hermetic):
    """THE root-cause pin: the human view must render from a bare script path.

    With the REAL ``cli_theme.py`` planted in the fake keg, ``python
    <keg>/hscc_daemon/verify_chat_roundtrip.py`` run from ``/`` has to import it
    and draw a Rich panel. Delete the ``sys.path`` bootstrap and this goes red:
    the import dies, ``theme`` stays None, and the run degrades to the plain
    fallback with no panel — the shipped behaviour, just politely labelled.
    """
    pytest.importorskip("rich")
    root, script = _fake_keg(tmp_path, theme=None)
    shutil.copyfile(CLI_THEME_SRC, root / "hscc_daemon" / "cli_theme.py")
    cp = _run(script, [], cwd="/", home=_hermetic,
              extra_path=tmp_path / "emptybin")
    combined = cp.stdout + cp.stderr
    assert "Traceback" not in combined, combined
    assert "╭" in combined, combined       # a real Rich panel was drawn
    assert "cannot run chat round trip" in combined, combined
    assert cp.returncode == 2, combined    # the round trip's own verdict


# ── 2. the renderer is optional ──────────────────────────────────────────────

def test_missing_renderer_degrades_to_plain_not_a_traceback(tmp_path, _hermetic):
    """No cli_theme/rich ⇒ the human view prints plain text; exit code is honest.

    The old code raised out of ``render_human`` and handed verify a traceback as
    the row detail. A missing renderer must never be reported as a chat failure,
    so the exit code stays the round trip's verdict (2 = preconditions), not a
    crash.
    """
    _, script = _fake_keg(tmp_path, theme=_BROKEN_THEME)
    cp = _run(script, [], cwd="/", home=_hermetic,
              extra_path=tmp_path / "emptybin")
    combined = cp.stdout + cp.stderr
    assert "Traceback" not in combined, combined
    assert "AttributeError" not in combined, combined
    assert "cannot run chat round trip" in combined, combined
    assert cp.returncode == 2, combined


def test_machine_json_path_survives_a_missing_renderer(tmp_path, _hermetic):
    """``--json`` never needs the theme, so verify's path is renderer-proof."""
    _, script = _fake_keg(tmp_path, theme=_BROKEN_THEME)
    cp = _run(script, ["--json"], cwd="/", home=_hermetic,
              extra_path=tmp_path / "emptybin")
    combined = cp.stdout + cp.stderr
    assert "Traceback" not in combined, combined
    assert cp.returncode == 2, combined
    assert "no API host or token" in json.loads(cp.stdout)["error"]


def test_missing_renderer_keeps_the_verdict_of_a_failed_round_trip(tmp_path,
                                                                   monkeypatch,
                                                                   capsys):
    """The fallback view also covers the FAILED path, and still exits 1.

    Unit-level (no subprocess) so the exact code/result contract is pinned:
    whatever the renderer, ``render_human`` returns the code it was given.
    """
    import hscc_daemon.verify_chat_roundtrip as rt

    monkeypatch.setattr(rt, "theme", None)
    code = rt.render_human(1, {"error": "boom", "http": 500, "job_id": "j1",
                              "reply": None, "tokens_before": 100,
                              "tokens_after": None})
    out = capsys.readouterr().out
    assert code == 1
    assert "CHAT ROUND TRIP FAILED" in out
    assert "j1" in out and "boom" in out
    assert "generation_tokens_total: 100 ->" in out


def test_fallback_view_needs_no_markup_escaping(tmp_path, monkeypatch, capsys):
    """A reply quoting Rich markup cannot break the plain view.

    The Rich path escapes markup with ``theme.esc``; the fallback prints no
    markup at all, so ``[/bold]`` in live data is inert here by construction —
    it must print literally and must not raise.
    """
    import hscc_daemon.verify_chat_roundtrip as rt

    monkeypatch.setattr(rt, "theme", None)
    code = rt.render_human(0, {"job_id": "j2", "status": "done", "elapsed": 1.0,
                              "reply": "[/bold]pong", "tokens_before": 1.0,
                              "tokens_after": 2.0, "delta": 1.0})
    out = capsys.readouterr().out
    assert code == 0
    assert "[/bold]pong" in out


def test_the_renderer_can_never_alter_the_verdict(tmp_path, monkeypatch):
    """The exit code comes from ``run()``, and the renderer echoes it unchanged.

    Two halves, both load-bearing because ``verify.run_chat_roundtrip`` maps this
    script's exit code onto the ``chat_roundtrip`` row:

    * ``main()`` returns the code ``run()`` produced — a rendering detail (Rich
      missing) cannot move the verdict, whatever a renderer returns.
    * Each renderer path — Rich and the plain fallback — returns the code it was
      handed for ALL three codes (0/1/2), so it can neither invent a pass nor
      swallow a failure. A mutation-probe that made the fallback ``return 0`` on
      the precondition path is invisible through ``main()`` precisely because of
      the first half, so this pins it where it is actually observable.
    """
    import io
    from contextlib import redirect_stdout

    import hscc_daemon.verify_chat_roundtrip as rt

    for label, theme in (("plain (no renderer)", None),
                         ("rich", rt.theme)):
        monkeypatch.setattr(rt, "theme", theme)
        monkeypatch.setattr(sys, "argv", ["verify_chat_roundtrip.py"])
        seen = []
        for code in (0, 1, 2):
            monkeypatch.setattr(rt, "run",
                                lambda c=code: (seen.append(c) or (c, _ANY_RESULT)))
            with redirect_stdout(io.StringIO()):
                assert rt.main() == code, (label, code)
        assert seen == [0, 1, 2]

        for code in (0, 1, 2):
            with redirect_stdout(io.StringIO()):
                assert rt.render_human(code, dict(_ANY_RESULT)) == code, (label, code)


# ── 3. no regression of the ordering, repo-wide ──────────────────────────────
#
# The card's item 2 was "check every OTHER module that runs as a standalone
# script for the same ordering mistake". A report rots; this check does not. It
# is deliberately narrow: only the ORDERING fault (bootstrap exists but sits
# after the import) is a bug, because a file with no bootstrap at all may be
# fine (launched with PYTHONPATH, or with a guarded import fallback).

def _is_syspath_call(node):
    f = node.func if isinstance(node, ast.Call) else None
    return bool(f and isinstance(f, ast.Attribute)
                and f.attr in ("insert", "append")
                and isinstance(f.value, ast.Attribute) and f.value.attr == "path"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "sys")


def _stmt_has_bootstrap(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    return any(_is_syspath_call(n) for n in ast.walk(stmt))


def _stmt_has_pkg_import(stmt, pkg="hscc_daemon"):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    for n in ast.walk(stmt):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == pkg:
            return True
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == pkg:
                    return True
    return False


def _ordering_offence(src):
    """Return (import_line, bootstrap_line) if the import precedes the bootstrap.

    Module-level statements only, in source order: an import nested in a
    function body runs at call time and is a different (usually deliberate,
    lazy) shape, so it is not this bug.
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    first_import = first_boot = None
    for st in tree.body:
        if first_boot is None and _stmt_has_bootstrap(st):
            first_boot = st.lineno
        if first_import is None and _stmt_has_pkg_import(st):
            first_import = st.lineno
    if first_import is None or first_boot is None:
        return None
    return (first_import, first_boot) if first_import < first_boot else None


_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build",
              "dist", ".pytest_cache", "_archive", "docs", ".worktrees"}


def _bare_runnable_modules():
    """Repo .py files that can be executed as a bare script path."""
    out = []
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            p = os.path.join(base, f)
            try:
                src = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            runnable = src.startswith("#!/")
            try:
                for st in ast.parse(src).body:
                    if isinstance(st, ast.If) and isinstance(st.test, ast.Compare) \
                            and isinstance(st.test.left, ast.Name) \
                            and st.test.left.id == "__name__":
                        runnable = True
                        break
            except (SyntaxError, ValueError):
                continue
            if runnable:
                out.append(p)
    return out


def test_the_shipped_file_no_longer_imports_before_its_bootstrap():
    """Pin the fix itself: the import now follows the insert, not the reverse."""
    src = open(ROUNDTRIP_SRC, encoding="utf-8").read()
    assert _ordering_offence(src) is None
    assert "sys.path.insert(0, REPO)" in src
    # Ordering, asserted on the text too so a reflow cannot silently undo it.
    assert src.index("sys.path.insert(0, REPO)") < \
        src.index("from hscc_daemon import cli_theme")


def test_no_repo_module_imports_hscc_daemon_before_its_bootstrap():
    """Repo-wide guard: the bootstrap must never again sit after the import.

    Offenders are reported with file:line so the next worker fixes the ordering
    instead of guessing what broke.
    """
    offenders = []
    for path in _bare_runnable_modules():
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        hit = _ordering_offence(src)
        if hit:
            offenders.append("%s: import@%d bootstrap@%d"
                             % (os.path.relpath(path, REPO), hit[0], hit[1]))
    assert not offenders, (
        "These modules put their sys.path bootstrap AFTER the hscc_daemon import "
        "it is meant to protect, so the bootstrap is dead code and a bare script "
        "path dies with ModuleNotFoundError:\n" + "\n".join(offenders))


def test_the_ordering_check_itself_has_teeth(tmp_path):
    """A guard that cannot fail is decoration — prove this one can.

    Feed the checker the exact shipped shape (import, then insert) and it must
    report it; feed it the fixed shape and it must stay quiet.
    """
    broken = ("import os, sys\n"
              "from hscc_daemon import cli_theme as theme\n"
              "REPO = os.path.dirname(os.path.abspath(__file__))\n"
              "sys.path.insert(0, REPO)\n"
              'if __name__ == "__main__":\n'
              "    pass\n")
    assert _ordering_offence(broken) is not None
    fixed = ("import os, sys\n"
             "REPO = os.path.dirname(os.path.abspath(__file__))\n"
             "sys.path.insert(0, REPO)\n"
             "from hscc_daemon import cli_theme as theme\n"
             'if __name__ == "__main__":\n'
             "    pass\n")
    assert _ordering_offence(fixed) is None
    # A function-level (lazy) import is NOT this bug.
    lazy = ("import os, sys\n"
            "def go():\n"
            "    from hscc_daemon import cli_theme as theme\n"
            "    return theme\n"
            "sys.path.insert(0, os.getcwd())\n"
            'if __name__ == "__main__":\n'
            "    pass\n")
    assert _ordering_offence(lazy) is None
