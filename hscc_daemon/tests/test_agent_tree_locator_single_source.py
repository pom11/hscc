"""The Hermes agent-tree locator has ONE seam, and a test that half-controls it
is a red waiting for a runner (t_ce473bf0).

Why this guard exists
---------------------
The roster-wide HOME seal (t_2b2a6c3b, PR #606) moves ``HOME`` to an empty
``/tmp`` dir for every leg and DECLARES the machine's real paths instead
(``HERMES_AGENT_PATH``, ``SPARKRUN_SRC``, ``HSCC_TAP_DIR`` — the shape #604
established for ``SPARKRUN_SRC``). Under that posture four ``hscc_daemon`` cases
went from silently-skipped to RED, and all four were the same harness bug with
two faces:

  1. a case that re-derived the agent tree from the home —
     ``REAL_AGENT = Path.home() / <fragments>`` in
     ``test_hermes_cli_import_isolation.py``, twice (the outer module and nested
     probe file B). Under a seal that names a directory the seal just emptied,
     while the code under test resolves through the declared pin; and
  2. a case that controlled the locator through only ONE of the two sources the
     production resolver reads: ``test_autodown.py`` patched the fallback
     constant while ``autodown.py:874-875`` reads the environment variable
     FIRST — so the runner's declaration silently outranked the fixture and the
     fixture's "tree missing" premise never held.

Both are invisible on an unsealed workstation and only the seal can show them,
and #606's declaration fixed the *capability* without fixing the *test* —
nothing stopped a fifth site from being written the same way. So the two rules
are swept here, in the same AST position
``test_planted_hermes_cli_single_source.py`` holds for the plant rule.

The rules
---------

R1  No test file may spell the agent tree out of the home. The tree is asked of
    the owner — ``hermes_cli_plant.agent_tree_root()`` (harness) or
    ``hermes_import.resolve_agent_tree()`` (production), which is what the
    former calls — because both honour the documented override. A home-derived
    spelling reads the workstation instead of the seam.
R2  A function that patches the fallback constant must neutralise the override
    that outranks it in the same function (``setenv``/``delenv`` of the env
    name), because the override wins.

Scope decisions (stated, not hidden):

  * The sweep is ``hscc_daemon/tests/**.py`` — the harness. Production keeps its
    documented default (``hermes_import.DEFAULT_AGENT_PATH``,
    ``autodown._HERMES_AGENT_PATH``); that is the ONE place the ``~``-spelling
    belongs and it is deliberately not swept.
  * R1 fires when the tree literal and a home read share a STATEMENT, not merely
    a file or a function: a fixture that builds a fabricated ``.hermes/hermes-agent``
    path under ``tmp_path`` is constructing a path (allowed, and common), while
    an unrelated ``expanduser`` two lines away is not the hazard.
  * Positive controls below feed the checker synthetic sources, so the sweep
    cannot rot into a no-op that reports nothing about anything.
"""

import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SWEEP_ROOT = "hscc_daemon/tests"


def _frag(*parts):
    """Assemble a swept literal at runtime so this file cannot sweep itself."""
    return "".join(parts)


TREE_WORD = _frag("hermes-", "agent")
ENV_PIN = _frag("HERMES_", "AGENT_PATH")
FALLBACK_CONST = _frag("_", "HERMES_AGENT_PATH")

#: Reads of the operator's home — the thing a seal moves.
HOME_CALL_ATTRS = ("home", "expanduser")
HOME_ENV_NAMES = ("HOME", "USERPROFILE")


#: The ONE statement inside the swept tree allowed to spell the default out of
#: the home, keyed by (file, exact unparsed statement) so a rewrite silently
#: drops the exemption and the guard reddens instead of widening.
#:
#: It is the harness OWNER's own fallback for the case where ``hermes_import``
#: cannot be imported at all (``# pragma: no cover``). It reads
#: ``env or <default>``, so a declared pin still outranks the literal and a seal
#: cannot hide behind it — which is precisely the difference from the shape R1
#: bans, a home read that no override can outrank.
_R1_ALLOW = {
    "hscc_daemon/tests/hermes_cli_plant.py": {
        "raw = os.path.expanduser(env or '~/.hermes/hermes-agent')":
            "the owner's documented fallback when hermes_import is unimportable; "
            "the env override is honoured in the same expression "
            "(hermes_cli_plant.py:226-233)",
    },
}


def _tracked_test_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", SWEEP_ROOT],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert files, f"{SWEEP_ROOT} is not tracked — the guard would sweep nothing"
    return files


def _parents(tree):
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _enclosing_stmt(node, parents):
    """The statement ``node`` sits in — R1's unit of scope."""
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.stmt):
            return node
    return node


def _home_reads(tree):
    """``(node, why)`` for every home read inside ``tree``."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in HOME_CALL_ATTRS:
            base = node.value
            base_src = ast.unparse(base) if isinstance(
                base, (ast.Name, ast.Attribute)) else ""
            if node.attr == "home" and base_src == "Path":
                hits.append((node, "Path.home()"))
            elif node.attr == "expanduser" and base_src in ("os.path", "os"):
                hits.append((node, "os.path.expanduser()"))
        elif isinstance(node, ast.Name) and node.id == "expanduser" \
                and isinstance(node.ctx, ast.Load):
            hits.append((node, "expanduser() (imported directly)"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in HOME_ENV_NAMES:
            hits.append((node, f"the {node.value} environment name"))
    return hits


def _sources(rel, src):
    """``(label, code)`` for a module AND for every pytest file it embeds.

    A nested harness file (``_PLANT_FILE``, ``_PROBE_FILE``, ``_WINDOW_FILE`` in
    ``test_hermes_cli_import_isolation.py``) is a STRING at the outer level, so
    the AST never sees the code inside it — and one of the two home-spellings
    this card fixed lived exactly there (nested probe file B). The marker is
    deliberately narrow: a generated test file carries its own imports, so a
    ``def test_`` plus an import line is what distinguishes source from prose
    that merely mentions a test.
    """
    out = [(rel, src)]
    try:
        tree = ast.parse(src, rel)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        body = node.value
        if "def test_" not in body:
            continue
        if not any(mark in body for mark in ("\nimport ", "\nfrom ")):
            continue
        try:
            ast.parse(body)
        except SyntaxError:
            continue  # not source after all — prose that mentions a test
        out.append((f"{rel}:{node.lineno} (nested source)", body))
    return out


def _r1_offenders(rel, src):
    """``(offenders, allowed)`` for one source — see ``_R1_ALLOW`` above."""
    offenders, allowed = [], []
    for label, code in _sources(rel, src):
        try:
            tree = ast.parse(code, label)
        except SyntaxError as exc:  # a file that cannot be parsed cannot be swept
            offenders.append(f"{label}: unparseable, so unsweepable: {exc}")
            continue
        parents = _parents(tree)
        for lit in (n for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and TREE_WORD in n.value):
            stmt = _enclosing_stmt(lit, parents)
            for _node, why in _home_reads(stmt):
                text = " ".join(ast.unparse(stmt).strip().split())
                reason = _R1_ALLOW.get(rel, {}).get(text)
                if reason is not None:
                    allowed.append(f"{label}:{lit.lineno} {text} — "
                                   f"allowed: {reason}")
                    continue
                offenders.append(
                    f"{label}:{lit.lineno} spells the agent tree out of the "
                    f"home ({why} in the same statement): {text[:110]}")
    return offenders, allowed


def _calls_named(tree, names):
    """Every ``ast.Call`` whose function ends in one of ``names``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else \
            (fn.id if isinstance(fn, ast.Name) else "")
        if fname in names:
            yield fname, node


def _sets_fallback_const(tree):
    """``setattr(mod, FALLBACK_CONST, …)`` or ``mod.X = …`` inside ``tree``."""
    for fname, call in _calls_named(tree, {"setattr"}):
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) \
                and call.args[1].value == FALLBACK_CONST:
            yield call
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == FALLBACK_CONST:
                    yield node


def _neutralises_env_pin(tree):
    """A setenv/delenv of the override, anywhere inside ``tree``."""
    for fname, call in _calls_named(tree, {"setenv", "delenv"}):
        if call.args and isinstance(call.args[0], ast.Constant) \
                and call.args[0].value == ENV_PIN:
            return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign)
                      else [node.target]):
                if ast.unparse(t).endswith(f"environ[{ENV_PIN!r}]"):
                    return True
    return False


def _r2_offenders(rel, src):
    """Functions that patch the fallback constant but not the override."""
    out = []
    for label, code in _sources(rel, src):
        try:
            tree = ast.parse(code, label)
        except SyntaxError as exc:
            out.append(f"{label}: unparseable, so unsweepable: {exc}")
            continue
        for fn in (n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            patches = list(_sets_fallback_const(fn))
            if patches and not _neutralises_env_pin(fn):
                first = patches[0]
                out.append(
                    f"{label}:{first.lineno} {fn.name}() patches the fallback "
                    f"{FALLBACK_CONST} without setenv/delenv of {ENV_PIN} in "
                    "the same function — the environment outranks the constant "
                    "(autodown.py:874-875), so a runner-declared pin silently "
                    "wins and the fixture's premise never holds")
    return out


def _sweep_r1():
    """R1 over every tracked test file: (offenders, allowances-exercised)."""
    offenders, allowed = [], []
    for rel in _tracked_test_files():
        hits, hits_allowed = _r1_offenders(rel, (REPO / rel).read_text())
        offenders += hits
        allowed += hits_allowed
    return offenders, allowed


def _sweep_r2():
    offenders = []
    for rel in _tracked_test_files():
        offenders += _r2_offenders(rel, (REPO / rel).read_text())
    return offenders


def test_no_test_file_spells_the_agent_tree_out_of_the_home():
    """R1 over the swept tree, with the reason naming the statement."""
    offenders, _allowed = _sweep_r1()
    assert not offenders, (
        "the Hermes agent tree must be asked of the seam, never derived from "
        "the home: hermes_cli_plant.agent_tree_root() (harness) or "
        "hermes_import.resolve_agent_tree() (production). A ~-derived spelling "
        "reads the workstation: it is what reddened two cases the moment the "
        "roster-wide HOME seal moved HOME to an empty /tmp dir (t_ce473bf0).\n"
        + "\n".join(offenders))


def test_the_r1_allowance_is_exactly_the_owner_fallback_and_is_still_used():
    """No dead exemption, and no exemption wider than the one statement.

    A stale allowlist is the quiet half of every allowlist: the day the fallback
    is rewritten the entry stops matching, this reddens, and the next reader
    decides instead of the guard silently gaining a hole.
    """
    _offenders, allowed = _sweep_r1()
    seen = {(row.split(" ")[0].split(":")[0]) for row in allowed}
    expected_files = set(_R1_ALLOW)
    assert seen == expected_files, (
        f"allowances exercised in {sorted(seen)}, allowlist written for "
        f"{sorted(expected_files)} — one of them is dead or the tree gained a "
        "home spelling nobody approved")
    assert len(allowed) == sum(len(v) for v in _R1_ALLOW.values()), (
        "an allowance matched more than its single statement:\n"
        + "\n".join(allowed))


def test_a_fallback_constant_patch_also_neutralises_the_override():
    """R2 over the swept tree."""
    offenders = _sweep_r2()
    assert not offenders, (
        "patching the module constant is only half of controlling the locator "
        "— the environment is read first (autodown.py:874-875), so a fixture "
        "that wants the default branch has to delenv the override (the shape "
        "test_autodown.py's own sibling case already uses) or setenv the tree "
        "it means (t_ce473bf0).\n" + "\n".join(offenders))


# ── positive controls: the sweep must bite, not merely run ──────────────────

#: The controls are assembled at RUNTIME on purpose: a Constant holding a whole
#: generated test file is what ``_sources`` treats as nested source, and a
#: literal control here would be swept as if it were a shipped offender (and
#: the R2 one would be a true positive, which is the wrong place for a mutant).
_R1_LINES = (
    "from pathlib import Path",
    "import os.path",
    "",
    'REAL = Path.home() / ".hermes" / "' + TREE_WORD + '"',
    'MISSING = os.path.expanduser("~/{0}/.hermes/{0}".format("' + TREE_WORD + '"))',
    'OK_TMP = Path("/tmp/x") / ".hermes" / "' + TREE_WORD + '"',
    "OK_SEAM = Path(hermes_import.resolve_agent_tree())",
)

_R2_LINES = (
    "import pytest",
    "",
    "",
    "def test_patches_the_constant_only(monkeypatch, ad):",
    "    monkeypatch.setattr(ad, '" + FALLBACK_CONST + "', '/nope')",
    "    assert ad._load_kanban_db_or_default() is None",
    "",
    "",
    "def test_patches_both_halves(monkeypatch, ad):",
    "    monkeypatch.delenv('" + ENV_PIN + "', raising=False)",
    "    monkeypatch.setattr(ad, '" + FALLBACK_CONST + "', '/nope')",
    "    assert ad._load_kanban_db_or_default() is None",
)


def test_r1_control_reports_the_home_spelling_and_forgives_the_seam():
    """A no-op R1 (a checker that found nothing anywhere) fails here first."""
    hits, allowed = _r1_offenders("control_r1.py", "\n".join(_R1_LINES))
    assert len(hits) == 2, (
        f"expected exactly the two ~-derived spellings to be reported, got "
        f"{hits}")
    assert all("control_r1.py" in h for h in hits), hits
    assert allowed == [], (
        f"the control file has no right to an allowance: {allowed}")
    # The tmp-built and seam-built spellings must NOT be reported: R1's hazard
    # is the home, not the word.
    tmp_ok, _ = _r1_offenders(
        "control_ok.py",
        'from pathlib import Path\nX = Path("/tmp/t") / ".hermes" / "'
        + TREE_WORD + '"\n')
    assert tmp_ok == [], tmp_ok


def test_r1_also_reads_a_test_file_embedded_as_a_string():
    """The recursion is pinned, not assumed — one of the two shipped reds was
    inside ``_PROBE_FILE``, which the outer AST cannot see.

    Deleting ``_sources`` would otherwise shrink the guard quietly instead of
    reddening it.
    """
    nested = "\n".join(("import os",
                        "from pathlib import Path",
                        "",
                        "def test_nested_probe():",
                        '    REAL = Path.home() / ".hermes" / "' + TREE_WORD + '"'))
    outer = "import os\n_NESTED = " + repr(nested) + "\n"
    hits, _allowed = _r1_offenders("nested_control.py", outer)
    assert len(hits) == 1 and "nested source" in hits[0], (
        f"the embedded-file sweep is not running: {hits}")


def test_r2_control_reports_the_half_patch_only():
    """A no-op R2 fails here first, and the correct shape stays unreported."""
    hits = _r2_offenders("control_r2.py", "\n".join(_R2_LINES))
    assert len(hits) == 1 and "test_patches_the_constant_only" in hits[0], hits
    assert not _r2_offenders(
        "control_r2b.py",
        "def f(monkeypatch, ad):\n"
        "    monkeypatch.delenv('" + ENV_PIN + "', raising=False)\n"
        "    monkeypatch.setattr(ad, '" + FALLBACK_CONST + "', '/nope')\n")


def test_the_sweep_is_not_sweeping_nothing():
    """The roster R1/R2 run over a non-empty file set (a broken glob is silent).

    Pinned at the number of tracked test files rather than ``> 0``: the guard's
    value is proportional to its reach, and a moved directory would otherwise
    shrink it to zero without a word.
    """
    files = _tracked_test_files()
    assert len(files) >= 100, (
        f"{SWEEP_ROOT} resolved to {len(files)} tracked files — the sweep is "
        "reading something other than the leg it was written for")
    assert "hscc_daemon/tests/test_autodown.py" in files
    assert "hscc_daemon/tests/test_hermes_cli_import_isolation.py" in files
