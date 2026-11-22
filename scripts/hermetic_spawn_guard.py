"""Detector: a leg's own tests must spawn the tree and interpreter they claim.

WHY THIS EXISTS. t_bb057914 shipped two ``hscc-cluster`` tests that were red on
the runner and green on the operator's host — not because CI had machine state
the test read, but because the TEST BODIES read the machine directly:

  * ``test_cli.py::TestCLIArgvNoScriptName::test_argv_without_script_name``
    spawned with a literal ``cwd`` equal to the operator's absolute repo path.
    On a runner the path does not exist (``FileNotFoundError`` inside
    ``_execute_child``); on the operator's host it DOES exist, so the child
    silently exercised a *different checkout* than the one under test. That test
    could not fail locally by construction.
  * ``test_no_ansi.py::TestNoAnsiTemplateCli::test_json_byte_identical``
    spawned argv0 ``"python"`` — a PATH lookup, not the interpreter running
    pytest. On the runner that is the setup-python base interpreter, which has no
    ``rich``; the venv running pytest does.

Both faults are invisible to every *environment* posture (HOME, interpreter
version, hermes absence, terminal width) because the trigger is a **source
literal**. Nothing but a structural sweep of the test bodies can see them, and
nothing but a sweep keeps them from re-appearing in the next test someone adds.

WHAT IT BANS (exactly the three shapes, and only inside a spawn call):

  1. ``path-resolved-interpreter`` — a ``subprocess.{run,Popen,check_output,
     check_call}`` call whose argv[0] is a string constant ``"python"`` or
     ``"python3"``. Require ``sys.executable`` (or a path derived from it).
  2. ``home-prefixed-path`` — a string constant used as that call's ``cwd=`` or
     as one of its argv elements (the script path) that is rooted in a user
     home. Require a path derived from ``__file__`` / ``tmp_path``.
  3. ``expression-built-home-path`` — the same argument position filled by an
     *expression* whose head is ``Path.home()``, ``os.path.expanduser`` or
     ``os.path.expandvars`` (through ``str(...)`` / ``os.path.join(...)`` /
     ``f"..."`` / a module-level constant, and through ``.parent`` /
     ``.resolve()``). Not a ``str`` constant, so rule 2 cannot see it, and it
     reads the machine just as hard. t_bb057914's class of fault re-appears the
     moment someone writes ``Path.home() / "x"`` instead of typing the literal,
     which is exactly what five ``hscc-bootstrap`` modules had done.

WHY AN AST SWEEP AND NOT A SUBSTRING BAN. ``hscc-project/tests/test_registry.py``
and ``test_kanban.py`` deliberately embed home-prefixed paths as *path-parsing
fixture data*, and docstrings across the repo mention such paths while
explaining why they must not be used. A substring ban reddens all of that, and
the failure mode of a substring ban is that someone deletes the ban (see
mutation-probing's AST-vs-substring lesson). So a home-prefixed constant is a
violation ONLY where it is an argument of a spawn call — that is what separates
a machine read from data.

THE HOME THRESHOLD IS NOT ``Path.home()`` ALONE. ``scripts/tests/conftest.py``
and ``hscc-bootstrap/tests/conftest.py`` seal ``HOME`` for every test in their
legs, so a threshold read from the runtime environment would be the sandbox and
would silently stop matching the operator's path — a guard that goes blind
exactly on the legs that seal hardest. The threshold therefore unions the env
home, the passwd-record home (``getpwuid``, ignores ``HOME``), and the
``/Users/<x>`` / ``/home/<x>`` spellings so the same literal is caught on the
macOS workstation and on a Linux runner alike.

LIMITS, STATED RATHER THAN IMPLIED. The sweep is single-module and
single-level: it resolves an argv/cwd passed through a *local* name bound to a
string/list literal in the same function or module body (the common
``cmd = [...]`` then ``subprocess.run(cmd)`` shape), and it does not
inter-procedurally follow a helper defined elsewhere. What it DOES follow, since
t_4e405e3a, is an **expression-built** home path — ``str(Path.home() / ".hermes"
/ "venv" / "bin" / "python")`` and ``os.path.expanduser("~/x")`` read the machine
exactly as a literal does, so they are banned by shape (rule 3) rather than by
constant. Still not policed, as stated limits and not oversights:
``os.environ["HOME"]``, a home path that arrives through a function parameter or
an imported helper, and ``cmds[0]`` subscript calls.

WHAT THE BAN DOES NOT DO IS FORBID AN INTEGRATION TIER. A case that genuinely
must run against the real install (``hscc-bootstrap`` has 25) asks the leg's own
capability owner for the path — ``hermes_gap.real_install_interpreter()`` — and
that call has no home-read head, so it passes rule 3 *because it is the sanctioned
shape*, not because it is whitelisted. The machine read then lives in one named
file per leg, which is what makes it countable; five private ``skipif``-gated
constants in one leg is the shape rule 3 exists to make impossible.

Used by every leg's ``tests/test_hermetic_subprocess_spawn.py`` (one file per
leg dir, sweeping its own dir only, matching the ``test_hermes_gap_single_posture
.py`` doctrine that a leg's guard polices its own leg) and unit-tested in
``scripts/tests/test_hermetic_spawn_guard.py``.
"""

import ast
import os
import pwd
from pathlib import Path

SPAWN_MODULE = "subprocess"

#: The four spawn entry points the card names. ``subprocess.call`` is a thin
#: wrapper over ``check_call``/``Popen``; if a leg ever uses it, add it here and
#: the sweep follows.
SPAWN_FUNCS = frozenset({"run", "Popen", "check_output", "check_call"})

#: argv[0] spellings that mean "ask PATH which interpreter I get".
BANNED_ARGV0 = frozenset({"python", "python3"})

#: Keywords that carry a working directory or an executable to spawn.
PATH_KEYWORDS = frozenset({"cwd", "executable"})

#: Keywords that carry argv (``run`` calls it ``args``, ``Popen`` accepts both).
ARGV_KEYWORDS = frozenset({"args", "cmd"})

#: User-home spellings that exist on the hosts this repo runs on: macOS
#: ``/Users/<name>``, Linux ``/home/<name>``. A literal of this shape in a spawn
#: argument is a read of whichever machine happens to own that directory.
HOME_SHAPED_ROOTS = ("/Users/", "/home/")

KIND_INTERPRETER = "path-resolved-interpreter"
KIND_HOME_PATH = "home-prefixed-path"
KIND_HOME_EXPRESSION = "expression-built-home-path"

#: The heads of an expression that mean "resolve against THIS machine's home".
#: ``expanduser``/``expandvars`` are ``os.path``'s; ``home`` is ``Path``'s, and
#: only reached through a name that actually spells ``pathlib``/``Path`` (see
#: ``_pathlib_bindings``) so an unrelated ``.home()`` method cannot be caught by
#: accident. A bare ``expanduser(...)`` counts, because
#: ``from os.path import expanduser`` is the same read — the same reasoning that
#: makes an aliased ``subprocess.run`` a spawn.
EXPANDER_HEADS = frozenset({"expanduser", "expandvars"})
HOME_METHOD = "home"
DEFAULT_PATHLIB_ROOTS = frozenset({"Path", "pathlib"})


class Violation:
    """One banned spawn argument, addressable as ``rel:line: kind: detail``."""

    __slots__ = ("rel", "lineno", "kind", "detail", "origin")

    def __init__(self, rel, lineno, kind, detail, origin=None):
        self.rel = rel
        self.lineno = lineno
        self.kind = kind
        self.detail = detail
        self.origin = origin

    def __str__(self):
        where = "%s:%d" % (self.rel, self.lineno)
        if self.origin:
            where += " (from %s)" % self.origin
        return "%s: %s: %s" % (where, self.kind, self.detail)

    def __repr__(self):
        return "Violation(%s)" % (self,)


# --------------------------------------------------------------------------- #
# the home threshold
# --------------------------------------------------------------------------- #

def home_roots():
    """Absolute prefixes that mean "this machine's home".

    Three sources, unioned, because each one is blind in a posture the others
    cover:

      * ``Path.home()`` — honours ``HOME``, so it is right on a runner
        (``/home/runner``) and WRONG under a conftest that seals ``HOME``.
      * the passwd record — ignores ``HOME``, so it still names the operator's
        real home inside a sealed leg.
      * ``/Users/`` and ``/home/`` — the account-home spellings, which catch an
        operator's macOS path on a Linux runner where neither of the above
        would match it. This is the arm that makes the guard useful on CI,
        where the literal it must catch belongs to a different OS.
    """
    roots = set()
    try:
        roots.add(str(Path.home()))
    except (RuntimeError, KeyError, OSError):
        pass
    try:
        roots.add(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError, AttributeError):
        # Windows (no pwd module) and any host without a passwd entry.
        pass
    return frozenset(r for r in roots if r and r != "/")


def is_home_prefixed(text, roots=None):
    """True if an absolute-path constant is rooted in a user home."""
    if not isinstance(text, str) or not text.startswith("/"):
        return False
    for root in (home_roots() if roots is None else roots):
        if text == root or text.startswith(root.rstrip("/") + "/"):
            return True
    return text.startswith(HOME_SHAPED_ROOTS)


# --------------------------------------------------------------------------- #
# name resolution: what does this Call actually spawn?
# --------------------------------------------------------------------------- #

def _spawn_bindings(tree):
    """Local names that reach ``subprocess.{run,Popen,check_output,check_call}``.

    Handles ``import subprocess``, ``import subprocess as sp`` and
    ``from subprocess import run as r`` so an alias is not an escape hatch.
    """
    module_names = set()
    func_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SPAWN_MODULE or alias.name.startswith(
                        SPAWN_MODULE + "."):
                    # ``import subprocess.foo`` binds the TOP package name.
                    module_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.module == SPAWN_MODULE and not node.level
                    and not any(a.name == "*" for a in node.names)):
                for alias in node.names:
                    if alias.name in SPAWN_FUNCS:
                        func_names.add(alias.asname or alias.name)
    return module_names, func_names


def _is_spawn_call(node, module_names, func_names):
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr not in SPAWN_FUNCS:
            return False
        holder = func.value
        if not isinstance(holder, ast.Name):
            return False
        return holder.id == SPAWN_MODULE or holder.id in module_names
    if isinstance(func, ast.Name):
        return func.id in func_names
    return False


def _literal_bindings(tree):
    """``name -> [(literal node, lineno), ...]`` for plain assignments.

    Follows the shape a spawn usually takes — ``cmd = ["python3", ...]`` on one
    line, ``subprocess.run(cmd)`` on another — without pretending to be a data
    flow engine. Every literal assignment of a name is kept and every one is
    checked, so a name with two bindings cannot launder a banned literal past
    the sweep; the violation reports at the spawn site *and* names the literal
    line, so a developer sees which one it is. An f-string, a call, or a
    comprehension assigned to the name simply contributes nothing.
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, (ast.Constant, ast.List, ast.Tuple, ast.BinOp)):
            out.setdefault(target.id, []).append((node.value, node.lineno))
    return out


def _resolve_text(node, bindings, _seen=frozenset()):
    """Yield ``(text, reporting_node, origin_line)`` for constant strings here.

    ``node`` may be a sequence literal (argv), a string literal (a shell-style
    command), or a name bound to one of those. ``_seen`` stops a name chain
    (``a = b`` / ``b = a``) from recursing forever.
    """
    if isinstance(node, ast.Name):
        if node.id in _seen:
            return
        _seen = _seen | {node.id}
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield (node.value, node, None)
        return
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            for found in _resolve_text(elt, bindings, _seen):
                yield found
        return
    if isinstance(node, ast.Starred):
        for found in _resolve_text(node.value, bindings, _seen):
            yield found
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # "python" + "3" and "/Users" + "/desac/x" — concatenation of literals
        # is still a literal for our purposes.
        left = [t for t, _, _ in _resolve_text(node.left, bindings, _seen)]
        right = [t for t, _, _ in _resolve_text(node.right, bindings, _seen)]
        if len(left) == 1 and len(right) == 1:
            yield (left[0] + right[0], node, None)
        return
    if isinstance(node, ast.Name) and node.id in bindings:
        for lit, assign_line in bindings[node.id]:
            if lit is node:
                continue          # a self-referential binding cannot recurse
            for text, literal_node, _ in _resolve_text(lit, bindings, _seen):
                # Report at the spawn site, name the literal's own line.
                yield (text, node, assign_line)
        return


# --------------------------------------------------------------------------- #
# rule 3 — an expression-built home path
# --------------------------------------------------------------------------- #

def _pathlib_bindings(tree):
    """Names in this module that spell ``pathlib`` / ``pathlib.Path``.

    An import alias is not an escape hatch here either: ``from pathlib import
    Path as P`` then ``P.home()`` is the same read as ``Path.home()``, so the
    aliases are collected and matched, exactly as ``_spawn_bindings`` does for
    ``import subprocess as sp``.
    """
    roots = set(DEFAULT_PATHLIB_ROOTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pathlib" or alias.name.startswith("pathlib."):
                    roots.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pathlib" and not node.level:
                for alias in node.names:
                    if alias.name == "Path":
                        roots.add(alias.asname or alias.name)
    return frozenset(roots)


def _callee_spelling(call):
    """Dotted spelling of a call's callee, or ``''`` when it is not a name chain.

    Hand-assembled rather than ``ast.unparse`` because it is asked for on every
    call node in a leg and must not depend on the unparser's spacing.
    """
    parts = []
    node = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def home_expression(node, bindings, pathlib_roots=DEFAULT_PATHLIB_ROOTS,
                    _seen=frozenset(), _via=None):
    """Return ``(spelling, origin_line, via_name)`` for a home read at the head.

    ``origin_line`` is the line of the constant the head was reached *through*
    (``REAL_VENV_PY = Path.home() / ...`` on line N, used on line M) and
    ``via_name`` is that constant's name, so the message can point at the
    definition a developer has to edit rather than only at the spawn — the same
    two-place report the literal rules give.

    Only the HEAD is examined, because that is what decides whether the value
    depends on the machine: ``str(...)`` and ``os.path.join(...)`` and an
    f-string are wrappers, ``.parent``/``.resolve()`` are modifiers, and a
    ``BinOp``/``List`` merely composes. A name is followed through the module's
    own literal bindings, which is why a module-level constant is not an escape.
    """
    if isinstance(node, ast.Name):
        if node.id in _seen:
            return None
        _seen = _seen | {node.id}
        for literal, assign_line in bindings.get(node.id, ()):
            if literal is node:
                continue          # a self-referential binding cannot recurse
            found = home_expression(literal, bindings, pathlib_roots, _seen,
                                    _via=node.id)
            if found:
                return (found[0], assign_line, node.id)
        return None
    if isinstance(node, ast.Call):
        spelling = _callee_spelling(node)
        if spelling:
            root = spelling.split(".")[0]
            leaf = spelling.rsplit(".", 1)[-1]
            if leaf in EXPANDER_HEADS:
                return (spelling, None, _via)
            if leaf == HOME_METHOD and root in pathlib_roots:
                return (spelling, None, _via)
        for arg in node.args:
            found = home_expression(arg, bindings, pathlib_roots, _seen, _via)
            if found:
                return found
        for keyword in node.keywords:
            found = home_expression(keyword.value, bindings, pathlib_roots,
                                    _seen, _via)
            if found:
                return found
        return None
    if isinstance(node, ast.Attribute):
        return home_expression(node.value, bindings, pathlib_roots, _seen, _via)
    if isinstance(node, ast.BinOp):
        return (home_expression(node.left, bindings, pathlib_roots, _seen, _via)
                or home_expression(node.right, bindings, pathlib_roots, _seen, _via))
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        # ``f"{Path.home()}/bin/python"`` is a JoinedStr holding a FormattedValue
        # holding the call — stopping at the f-string would be a one-character
        # cloak over the same machine read.
        values = (node.values if isinstance(node, ast.JoinedStr)
                  else (node.value, node.format_spec))
        for value in values:
            if value is None:
                continue
            found = home_expression(value, bindings, pathlib_roots, _seen, _via)
            if found:
                return found
        return None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            found = home_expression(element, bindings, pathlib_roots, _seen, _via)
            if found:
                return found
        return None
    if isinstance(node, ast.Starred):
        return home_expression(node.value, bindings, pathlib_roots, _seen, _via)
    return None


def _home_expression_violation(rel, role, found, report_node):
    spelling, origin_line, via = found
    return Violation(
        rel, getattr(report_node, "lineno", 0), KIND_HOME_EXPRESSION,
        "%s argument is built from %s%s — a machine path expressed as an "
        "expression is the same read as the literal, and no rule about string "
        "constants can see it. Derive it from __file__ / tmp_path, or ask the "
        "leg's capability owner when the case really is 'run against the real "
        "install'" % (role, spelling, (" (via %s)" % via) if via else ""),
        ("%s:%d" % (rel, origin_line)) if origin_line else None)


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #

def _argv_nodes(call):
    """Every node that can carry argv, plus the cwd/executable keywords."""
    if call.args:
        yield "argv", call.args[0]
    for kw in call.keywords:
        if kw.arg in ARGV_KEYWORDS:
            yield "argv", kw.value
        elif kw.arg in PATH_KEYWORDS:
            yield kw.arg, kw.value


def _argv_positions(node, bindings, _seen=frozenset()):
    """Yield ``(index, text, report_node, origin_line)`` per statically-known slot.

    The index is the whole point: the argv0 ban is about the FIRST slot, so an
    element that is not statically resolvable (``sys.executable``, the correct
    spelling) must still hold its index rather than shifting the next element
    into argv0's place. Without that, ``[sys.executable, "python"]`` — a script
    that merely names python — reads as a banned interpreter.

    A starred element ends the walk: nothing after it has a known position, so
    guessing would manufacture a violation.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield (0, node.value, node, None)
        return
    if isinstance(node, (ast.List, ast.Tuple)):
        for index, elt in enumerate(node.elts):
            if isinstance(elt, ast.Starred):
                return
            for text, report, origin_line in _resolve_text(elt, bindings, _seen):
                yield (index, text, report, origin_line)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        for text, report, origin_line in _resolve_text(node, bindings, _seen):
            yield (0, text, report, origin_line)
        return
    if isinstance(node, ast.Name):
        if node.id in _seen:
            return
        _seen = _seen | {node.id}
        for literal, assign_line in bindings.get(node.id, ()):
            if literal is node:
                continue
            for index, text, _, _ in _argv_positions(
                    literal, bindings, _seen):
                # Report at the spawn site, name the literal's own line.
                yield (index, text, node, assign_line)
        return


def scan_source(text, rel="<source>", bindings=None):
    """Return the ``Violation`` list for one module's source text.

    ``rel`` is only used for messages, so this is unit-testable on synthetic
    sources without touching a file.
    """
    tree = ast.parse(text, filename=rel)
    module_names, func_names = _spawn_bindings(tree)
    pathlib_roots = _pathlib_bindings(tree)
    if bindings is None:
        bindings = _literal_bindings(tree)
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawn_call(
                node, module_names, func_names):
            continue
        for role, value in _argv_nodes(node):
            # Rule 3 first and independently: an expression-built home path is
            # not a str constant, so neither literal rule below can see it, and
            # a value can be banned twice over for two different reasons.
            head = home_expression(value, bindings, pathlib_roots)
            if head is not None:
                found.append(_home_expression_violation(rel, role, head, value))
            if role != "argv":
                for text_value, report, origin_line in _resolve_text(
                        value, bindings):
                    if is_home_prefixed(text_value):
                        found.append(_home_violation(
                            rel, role, text_value, report, origin_line))
                continue
            for index, text_value, report, origin_line in _argv_positions(
                    value, bindings):
                if index == 0:
                    argv0 = text_value.split()[0] if text_value.strip() else ""
                    if argv0 in BANNED_ARGV0:
                        found.append(Violation(
                            rel, report.lineno, KIND_INTERPRETER,
                            'argv0 is the PATH-resolved %r — use sys.executable '
                            '(the venv running pytest is not what "python" '
                            'resolves to on the runner)' % argv0,
                            ("%s:%d" % (rel, origin_line)) if origin_line
                            else None))
                if is_home_prefixed(text_value):
                    found.append(_home_violation(
                        rel, role, text_value, report, origin_line))
    return found


def _home_violation(rel, role, text_value, report, origin_line):
    return Violation(
        rel, report.lineno, KIND_HOME_PATH,
        "%s argument %r is rooted in a user home — derive it from __file__ / "
        "tmp_path (a literal home path exists only on the machine that owns "
        "it, so the child runs a different tree there and dies on a runner)"
        % (role, text_value),
        ("%s:%d" % (rel, origin_line)) if origin_line else None)


def scan_file(path, rel=None):
    """Sweep one source file; a file that cannot be parsed is a violation."""
    rel = rel or str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation(rel, 0, "unreadable", "%s" % exc.__class__.__name__)]
    try:
        return scan_source(text, rel)
    except SyntaxError as exc:
        return [Violation(rel, exc.lineno or 0, "unparsable", str(exc.msg))]


def leg_files(leg_dir):
    """Every Python source pytest can collect in a leg dir, plus its helpers.

    ``conftest.py`` is included — it runs before every case, so a spawn written
    there is the most expensive one to get wrong. Non-test helpers the leg
    spawns (``_pvb_sim.py`` style) are included too: they are source in the leg,
    and a banned literal there is reached by a case in the same leg.
    """
    root = Path(leg_dir)
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts)


def scan_leg(leg_dir, files=None):
    """Sweep a leg dir; returns ``(violations, files_scanned)``."""
    root = Path(leg_dir)
    paths = files if files is not None else leg_files(root)
    out = []
    for path in paths:
        out.extend(scan_file(path, _rel_to(path, root)))
    return out, len(paths)


def _rel_to(path, root):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def report(violations, leg_label, scanned):
    """A failure message a developer can act on without opening the detector."""
    lines = [
        "Leg %r: %d test source file(s) swept, %d violation(s)." % (
            leg_label, scanned, len(violations)),
        "A leg's own tests must spawn the tree and interpreter they claim: "
        "argv0 'python'/'python3' is a PATH lookup (the runner's base "
        "interpreter has no project deps) and a home-prefixed cwd/script "
        "exists only on the machine that owns it. Use sys.executable and a "
        "path derived from __file__ or tmp_path. A Path.home()/expanduser/"
        "expandvars *expression* in the same position is the same read; if the "
        "case really is 'run against the real install', ask the leg's "
        "capability owner for the path instead of spelling the home.",
    ]
    lines.extend("  " + str(v) for v in violations)
    return "\n".join(lines)
