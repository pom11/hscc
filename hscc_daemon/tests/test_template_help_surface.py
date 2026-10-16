"""F2 (t_9032f96b) — `hscc template` help surface.

Pins the two fixes the CLI-help audit gate (t_7b0f65c8) reported for the
template group, and pruned as owner=F2 rules in
``scripts/tests/cli_help_expected.json``:

  1. (B) ``hscc template --help`` no longer overflows at 80/120. The
     validate/apply entries use the two-line layout: command on its own
     line, description wrapped with a continuation indent — no line may
     exceed the rendered width, and no continuation may sit at column 0.
  2. (D-rc) ``hscc template <verb> --help`` prints the verb's usage and
     exits 0 WITHOUT reaching the engine. It used to forward ``--help`` as
     the template name ("Template not found: --help", rc=1) — and for
     ``apply`` that meant dispatching into the template engine at all.

Examples inside the group help name REAL templates (the audit parse-checks
every ``example:`` line), so the names are asserted against the live
template registry rather than a placeholder.
"""

import io
import sys
import types
from contextlib import redirect_stdout

import pytest

from hscc_daemon import hscc as hscc_mod


def _run(argv, monkeypatch, columns):
    """Run _handle_template() with argv/COLUMNS pinned; (stdout, rc)."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out = io.StringIO()
    code = None
    try:
        with redirect_stdout(out):
            code = hscc_mod._handle_template()
    except SystemExit as e:  # the handler returns rc; sys.exit is belt-and-braces
        code = e.code
    return out.getvalue(), code


def _install_boom_engine(monkeypatch):
    """Any engine call during help is a bug — make it loud."""
    fake = types.ModuleType("cluster_template_cli")

    def _boom(args):
        raise AssertionError(f"help path reached the engine: {args!r}")

    fake.cmd_cluster_template = _boom
    monkeypatch.setitem(sys.modules, "cluster_template_cli", fake)


# ── 1. group help wraps at 80 and 120 (operator's original complaint) ─────

@pytest.mark.parametrize("columns", [80, 120])
def test_group_help_lines_fit_width(columns, monkeypatch):
    _install_boom_engine(monkeypatch)
    text, code = _run(["template", "--help"], monkeypatch, columns)
    assert code == 0
    lines = text.rstrip("\n").split("\n")
    over = [ln for ln in lines if len(ln) > columns]
    assert not over, f"lines overflow at COLUMNS={columns}: {over!r}"


@pytest.mark.parametrize("columns", [80, 120])
def test_group_help_two_line_layout_and_indent(columns, monkeypatch):
    """validate/apply: command alone on its line; description continuation
    lines indented (never flush-left) — the exact (B) invariant."""
    _install_boom_engine(monkeypatch)
    text, _ = _run(["template", "--help"], monkeypatch, columns)
    lines = text.split("\n")

    val = [ln for ln in lines
           if ln.startswith("  hscc template validate ")]
    assert len(val) == 1, "validate command must sit alone on its line"
    assert "Validate" not in val[0], "description must not share the command line"
    idx = lines.index(val[0])
    desc = lines[idx + 1]
    assert desc.startswith("      ") and desc.strip(), (
        f"validate description must be continuation-indented: {desc!r}")

    app = [ln for ln in lines if ln.startswith("  hscc template apply ")]
    assert len(app) == 1, "apply command must sit alone on its line"
    idx = lines.index(app[0])
    assert lines[idx + 1].startswith("      "), (
        f"apply description must be continuation-indented: {lines[idx + 1]!r}")

    # No help line may be a flush-left wrap continuation: every non-empty,
    # non-header line is either the header or indented.
    for ln in lines[3:]:
        if ln.strip():
            assert ln[:1].isspace(), f"unindented line in group help: {ln!r}"


def test_group_help_examples_name_real_templates(monkeypatch):
    """Examples must be REAL template names (audit §REAL EXAMPLE DATA), and
    the gate parse-checks them — a placeholder like <name> is a finding."""
    _install_boom_engine(monkeypatch)
    text, _ = _run(["template", "--help"], monkeypatch, 100)
    ex = [ln.strip() for ln in text.split("\n") if ln.strip().startswith("example:")]
    assert len(ex) >= 2, f"validate+apply need example lines, got {ex!r}"
    joined = "\n".join(ex)
    assert "<name>" not in joined, f"placeholder example: {joined!r}"
    # Real name from `hscc template list` / hscc-cluster/templates/.
    assert "4node-coding" in joined
    for line in ex:
        assert len(line) + 6 <= 78  # fits with the continuation indent


# ── 2. per-verb --help: prints usage, exits 0, never reaches the engine ───

@pytest.mark.parametrize("sub", ["list", "status", "preview", "validate", "apply"])
def test_verb_help_exits_zero_without_engine(sub, monkeypatch):
    _install_boom_engine(monkeypatch)
    text, code = _run(["template", sub, "--help"], monkeypatch, 80)
    assert code == 0, f"template {sub} --help exited {code}: {text!r}"
    assert "Usage:" in text
    assert "Template not found" not in text, "help token must not be the name"
    assert f"hscc template {sub}" in text


def test_verb_help_apply_does_not_reach_engine(monkeypatch):
    """The worst case before the fix: apply --help dispatched into the
    template engine (validate-then-apply path) and exited 1."""
    _install_boom_engine(monkeypatch)
    text, code = _run(["template", "apply", "4node-coding", "--help"],
                      monkeypatch, 80)
    assert code == 0
    assert "Usage: hscc template apply" in text


def test_verb_help_dash_h_also_intercepted(monkeypatch):
    _install_boom_engine(monkeypatch)
    text, code = _run(["template", "validate", "-h"], monkeypatch, 80)
    assert code == 0
    assert "Usage: hscc template validate" in text


@pytest.mark.parametrize("columns", [80, 120])
def test_verb_help_lines_fit_width(columns, monkeypatch):
    _install_boom_engine(monkeypatch)
    for sub in ("preview", "validate", "apply"):
        text, code = _run(["template", sub, "--help"], monkeypatch, columns)
        assert code == 0
        over = [ln for ln in text.split("\n") if len(ln) > columns]
        assert not over, f"{sub} --help overflows at {columns}: {over!r}"


def test_group_help_dash_h_matches_help_flag(monkeypatch):
    """`hscc template -h` is the group help too (not the unknown-sub error)."""
    _install_boom_engine(monkeypatch)
    text, code = _run(["template", "-h"], monkeypatch, 80)
    assert code == 0
    assert "Cluster template commands:" in text


def test_real_args_still_reach_the_engine(monkeypatch):
    """Regression guard the other way: the --help interception must not
    swallow a real validate/apply invocation."""
    captured = []
    fake = types.ModuleType("cluster_template_cli")
    fake.cmd_cluster_template = lambda a: (captured.append(list(a))
                                           or {"ok": True})
    monkeypatch.setitem(sys.modules, "cluster_template_cli", fake)
    monkeypatch.setattr(hscc_mod, "_emit", lambda result: 0)
    _run(["template", "validate", "4node-coding", "--json"], monkeypatch, 80)
    # --json is stripped by _strip_theme_and_json (the engine gets the name only)
    assert captured == [["validate", "4node-coding"]]
