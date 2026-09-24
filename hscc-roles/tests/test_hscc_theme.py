"""No-ANSI + byte-identity regression tests for the themed hscc-roles CLI.

The RICH CLI hscc-roles card converted every raw human-facing ``print`` /
``json.dumps`` in hscc.py to the themed Rich layer (hscc-roles/_theme.py). The
HARD INVARIANTS this file pins:

- The machine ``json.dumps`` paths (generate / orch / orch-all) stay
  BYTE-IDENTICAL: raw ``print(json.dumps(..., indent=2, ensure_ascii=False))``,
  never routed through a Console. Asserted equal to re-encoding the exact same
  payload from the same inputs — not merely "no ANSI".
- On a NON-TTY stdout, every themed human view degrades to PLAIN: NO ``\\x1b``
  escape bytes (scripts / the daemon parse this output; a single escaped byte
  in a pipe breaks a watcher).

Per the card's invariant we assert against a named constant (_ESC), never a
scattered literal escape character.
"""

import contextlib
import io
import json
import os
import subprocess
import sys

import pytest

import hscc
import rolelib


# The escape character, as one named constant — asserted, never a literal.
_ESC = "\x1b"


def _no_ansi(fn):
    """Run ``fn`` with stdout redirected to a non-TTY StringIO, assert no ANSI
    escape bytes, and return the captured text. Mirrors the flightdeck
    Test*NoAnsi pattern."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    out = buf.getvalue()
    assert _ESC + "[" not in out, f"ANSI escape found in piped output: {out!r}"
    assert _ESC not in out, f"ANSI escape found in piped output: {out!r}"
    return out


def _roles_setup(tmp_path, monkeypatch, names):
    """Point hscc-roles at an isolated tmp roles/profiles dir with ``names``
    role specs, so no command touches the operator's real tree. Also isolates
    the autonomy flag file (its default path ~/.hscc/autonomy would otherwise
    leak the operator's live value into tests)."""
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    for n in names:
        (roles_dir / f"{n}.yaml").write_text(
            f"name: {n}\nidentity: Does {n}.\nrouting_description: {n} stuff.\n"
        )
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles_dir))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(hscc, "_base_identity", lambda: "BASE\n")
    import autonomy as _auto
    monkeypatch.setattr(_auto, "AUTONOMY_FILE", str(tmp_path / "autonomy"))
    return roles_dir, profiles_dir


# --------------------------------------------------------------------------- #
# Byte-identity: the machine json.dumps paths stay raw print, never themed.
# --------------------------------------------------------------------------- #


def test_generate_json_byte_identical(tmp_path, monkeypatch):
    """generate's stdout is PURE JSON, byte-identical to the raw dump with the
    exact same indent=2 / ensure_ascii=False formatting."""
    _roles_setup(tmp_path, monkeypatch, ["tester"])
    import generator as _gen
    monkeypatch.setattr(_gen, "generate_profile", lambda spec, base: True)

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    ret = hscc.cmd_generate()
    assert ret == 0

    stdout = captured.getvalue()
    assert _ESC not in stdout, "json path must never carry ANSI"
    expected = json.dumps(
        {"generated": [{"role": "tester", "changed": True}]},
        indent=2, ensure_ascii=False,
    ) + "\n"
    assert stdout == expected, "generate json.dumps must stay byte-identical"


def test_orch_json_byte_identical(tmp_path, monkeypatch):
    """orch success stdout is PURE JSON, byte-identical to the raw dump."""
    _roles_setup(tmp_path, monkeypatch, [])
    import orchestrators as _orch
    result = {"project": "acme", "profile": "acme-orch",
              "session": "acme", "board": "acme", "changed": True}
    monkeypatch.setattr(_orch, "ensure_orchestrator",
                        lambda project, base_identity=None, path=None: result)

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    ret = hscc.cmd_orch(["acme"])
    assert ret == 0

    stdout = captured.getvalue()
    assert _ESC not in stdout, "json path must never carry ANSI"
    assert stdout == json.dumps(result, indent=2, ensure_ascii=False) + "\n", \
        "orch json.dumps must stay byte-identical"


def test_orch_all_json_byte_identical(tmp_path, monkeypatch):
    """orch-all stdout is PURE JSON (per its docstring), byte-identical.

    The stub varies per project (like the real ensure_orchestrator) so the
    assertion proves each project's fields survive into the raw dump.
    """
    _roles_setup(tmp_path, monkeypatch, [])
    import orchestrators as _orch
    monkeypatch.setattr(
        _orch, "ensure_orchestrator",
        lambda project, base_identity=None, path=None: {
            "project": project, "profile": f"{project}-orch",
            "session": project, "board": project, "changed": False,
        },
    )
    monkeypatch.setattr(_orch, "list_registry_projects",
                        lambda registry=None: ["acme", "beta"])

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    ret = hscc.cmd_orch_all([])
    assert ret == 0

    stdout = captured.getvalue()
    assert _ESC not in stdout, "json path must never carry ANSI"
    expected_report = {
        "requested": ["acme", "beta", "general"],
        "ensured": [
            {"project": "acme", "profile": "acme-orch", "session": "acme",
             "board": "acme", "changed": False},
            {"project": "beta", "profile": "beta-orch", "session": "beta",
             "board": "beta", "changed": False},
            {"project": "general", "profile": "general-orch", "session": "general",
             "board": "general", "changed": False},
        ],
    }
    assert stdout == json.dumps(expected_report, indent=2,
                                ensure_ascii=False) + "\n", \
        "orch-all json.dumps must stay byte-identical"


# --------------------------------------------------------------------------- #
# No-ANSI: every themed human view degrades to plain on a non-TTY stdout.
# --------------------------------------------------------------------------- #


def test_create_human_view_is_plain(tmp_path, monkeypatch):
    """create's themed output renders plain (no ANSI) and shows role + spec."""
    _roles_setup(tmp_path, monkeypatch, [])
    import author as _author
    spec_path = str(tmp_path / "roles" / "wizard.yaml")
    monkeypatch.setattr(_author, "create_role", lambda name, desc: spec_path)
    out = _no_ansi(lambda: hscc.cmd_create(["wizard", "casts spells"]))
    assert "wizard" in out
    assert "generate" in out  # the "next:" hint survives


def test_create_usage_error_is_plain(tmp_path, monkeypatch):
    """The missing-args usage line goes to stderr, still no ANSI when piped."""
    _roles_setup(tmp_path, monkeypatch, [])
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ret = hscc.cmd_create([])
    assert ret == 1
    out = buf.getvalue()
    assert _ESC not in out
    assert "Usage: hscc-roles create" in out


def test_list_human_view_is_plain(tmp_path, monkeypatch):
    """list's themed table degrades to plain (no ANSI) and shows the roles."""
    _roles_setup(tmp_path, monkeypatch, ["coder", "reviewer"])
    out = _no_ansi(lambda: hscc.cmd_list())
    assert "coder" in out and "reviewer" in out
    assert "profile" in out.lower()


def test_validate_ok_is_plain(tmp_path, monkeypatch):
    _roles_setup(tmp_path, monkeypatch, ["tester"])
    out = _no_ansi(lambda: hscc.cmd_validate())
    assert "valid" in out.lower()


def test_validate_error_is_plain(tmp_path, monkeypatch):
    """A bad spec surfaces its error text plainly (no ANSI)."""
    _roles_setup(tmp_path, monkeypatch, [])
    roles_dir = tmp_path / "roles"
    (roles_dir / "bad.yaml").write_text("name:\nidentity:\nrouting_description:\n")
    out = _no_ansi(lambda: hscc.cmd_validate())
    assert "error" in out.lower() or "bad" in out


def test_autonomy_off_is_plain(tmp_path, monkeypatch):
    _roles_setup(tmp_path, monkeypatch, [])
    out = _no_ansi(lambda: hscc.cmd_autonomy([]))
    assert "is off" in out.lower()


def test_autonomy_on_is_plain(tmp_path, monkeypatch):
    _roles_setup(tmp_path, monkeypatch, [])
    import autonomy as _auto
    monkeypatch.setattr(_auto, "is_on", lambda: True)
    out = _no_ansi(lambda: hscc.cmd_autonomy([]))
    assert "is on" in out.lower()


def test_orch_error_is_plain(tmp_path, monkeypatch):
    """orch failure routes through a themed status panel — plain, no ANSI."""
    _roles_setup(tmp_path, monkeypatch, [])
    import orchestrators as _orch
    monkeypatch.setattr(
        _orch, "ensure_orchestrator",
        lambda project, base_identity=None, path=None:
        (_ for _ in ()).throw(_orch.OrchestratorError("registry missing")),
    )
    out = _no_ansi(lambda: hscc.cmd_orch(["acme"]))
    assert "registry missing" in out


def test_unknown_command_is_plain(tmp_path, monkeypatch):
    """main()'s unknown-command error + usage go to stderr, no-ANSI output."""
    _roles_setup(tmp_path, monkeypatch, [])
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ret = _main_with_argv(["hscc", "bogus"])
    assert ret == 1
    assert _ESC not in buf.getvalue()


def _main_with_argv(argv):
    """Invoke hscc.main with a patched sys.argv (mirrors how it reads argv)."""
    orig = sys.argv
    sys.argv = argv
    try:
        return hscc.main()
    finally:
        sys.argv = orig


# --------------------------------------------------------------------------- #
# Real subprocess (non-TTY fd): the packaged CLI end-to-end, plain, no ANSI.
# --------------------------------------------------------------------------- #


def test_cli_subprocess_list_is_plain(tmp_path):
    """Run hscc.py list via subprocess with captured (non-TTY) stdout and a
    hermetic HERMES_HOME; human output must contain zero escape bytes."""
    plugin_dir = os.path.dirname(os.path.abspath(hscc.__file__))
    py = sys.executable
    hscc_script = os.path.join(plugin_dir, "hscc.py")
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HOME=str(tmp_path))
    res = subprocess.run([py, hscc_script, "list"], capture_output=True,
                         text=True, env=env)
    assert res.returncode == 0
    assert _ESC not in res.stdout
    assert "role" in res.stdout.lower()
