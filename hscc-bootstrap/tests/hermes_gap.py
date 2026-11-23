"""One definition of "does THIS machine have the real hermes-agent install?"
for the hscc-bootstrap leg (t_4e405e3a).

WHY A PROBE AND NOT A ``skipif``. Five test modules here each carried their own
``REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" /
"python"`` plus a private ``@pytest.mark.skipif(not REAL_VENV_PY.is_file(),
reason=...)``. Two faults, both the ones t_02c98915 -> t_2cc3d482 -> t_0fd2c149
-> t_b884497b measured elsewhere:

  * a mark's predicate is evaluated while pytest is still COLLECTING, so the
    answer is frozen before anything in the leg has run and it can only ever move
    a case from running to skipped. Asking here instead means a case runs
    whenever it can run; and
  * five private constants mean five spellings of one machine path and five
    skip REASONS, and ``scripts/run_tests.sh``'s stamp rule reads the REASON off
    ``-rs``. One missing install, five wordings, a leg nobody can read.

The shape the doctrine prescribes and this module is: ONE owner per leg, called
at RUNTIME, skipping with a reason that names the thing that is missing.

WHY THE FAMILY MAY READ THE MACHINE AT ALL (the decision, not an omission).
These are integration cases: they execute the shipped ``bootstrap.sh`` against
the REAL ``hermes-agent`` checkout and the REAL venv interpreter, because three
releases shipped an installer that did not parse while the Python suite was green
(see ``test_bootstrap_isolated.py``'s docstring). A ``tmp_path`` venv would assert
against a fabricated install no operator runs. So the machine read is kept, but it
is *asked for*, from this one file, at run time — which is also what lets
``scripts/hermetic_spawn_guard.py`` ban every *spelled* home expression at a spawn
argument without an exemption list: a call into this owner has no home-read head.

WHAT ``require_real_install()`` gates: the checkout AND the venv interpreter.
They are one capability here, not two, because no case in this leg can use one
without the other — every gated case symlinks the checkout into a sandbox home
and spawns the interpreter from it. (Split them the way ``hscc-roles`` split
``profiles``/``config`` the day a case wants one alone; the split is what t_2cc3d482
warned against skipping a family for a neighbouring reason.)

NOT IN THE RUNNER'S ROSTER YET. ``scripts/run_tests.sh``'s "sibling interpreter is
THIN on purpose" block is the declared live authority for which probes exist, and
this card may not edit that file. The roster line is a follow-up card; until it
lands this owner is discoverable here and from the five modules that import it.

Importable as a bare module because pytest puts the test dir on ``sys.path``
(``prepend`` import mode, no ``__init__.py``) — the same way ``skill_guard`` is
imported by ``test_skill_resolver_xval.py`` in this leg.
"""

import pytest

from pathlib import Path

#: Where the real install lives on a normal operator host. The ONE spelling in
#: this leg; ``test_hermetic_subprocess_spawn.py`` pins that no other module in
#: the leg defines a module-level constant built from a home read.
CHECKOUT = Path(".hermes") / "hermes-agent"
VENV_INTERPRETER = CHECKOUT / "venv" / "bin" / "python"


def real_install_missing():
    """``None`` when the real install is usable here, else the reason as text.

    Returns text rather than calling ``pytest.skip`` so a guard can ask the
    question without skipping (the ``*_import_error`` idiom in the other legs'
    owners), and so the SAME string is what the skip carries.
    """
    checkout = Path.home() / CHECKOUT
    interpreter = Path.home() / VENV_INTERPRETER
    if not checkout.is_dir():
        return ("no hermes-agent checkout at %s" % checkout)
    if not interpreter.is_file():
        return ("no venv interpreter at %s" % interpreter)
    return None


def checkout_path():
    """The real ``hermes-agent`` checkout — a pure answer, never a skip.

    For a consumer that is legal without the install: ``symlink_to`` accepts a
    missing target, so the isolated-install sandbox builder uses THIS and stays
    runnable on a thin sibling. Only a case that actually spawns or reads the
    install may skip, and it says so by asking for the skipping accessor.
    """
    return Path.home() / CHECKOUT


def interpreter_path():
    """The real install's venv interpreter — a pure answer, never a skip."""
    return Path.home() / VENV_INTERPRETER


def real_install_interpreter():
    """The real install's interpreter, or skip the calling case.

    The only sanctioned way to obtain a spawn argv0 from this leg's own tests:
    the expression has no ``Path.home()`` head at the call site, so the spawn
    guard sees a call into the leg's owner rather than a machine read, and the
    reader sees where the machine read actually happens.
    """
    require_real_install()
    return str(interpreter_path())


def real_install_checkout():
    """The real ``hermes-agent`` checkout, or skip the calling case."""
    require_real_install()
    return checkout_path()


def require_real_install():
    """Skip the calling case when this machine has no real install to use.

    Call it at RUNTIME — as a case's first statement, or from the helper that
    actually uses the install (``_run`` in ``test_bootstrap_isolated.py``, the
    sandbox builders elsewhere), never from a fixture an ungated case also takes:
    a gate that skips a case which could run is the leg going green by silence.
    """
    missing = real_install_missing()
    if missing is not None:
        pytest.skip(_reason(missing))


def _reason(missing):
    return ("needs the real Hermes install to run the installer against it "
            "(%s) — these cases are the integration tier: they execute the "
            "shipped bootstrap.sh against the real hermes-agent checkout and "
            "its venv interpreter, so they are neither hermetic nor runnable on "
            "a thin sibling (t_4e405e3a)" % missing)


#: Every probe this module owns, in definition order — the roster a future
#: single-posture guard for this leg reads instead of carrying its own copy.
PROBES = (
    "real_install_missing",
    "checkout_path",
    "interpreter_path",
    "real_install_interpreter",
    "real_install_checkout",
    "require_real_install",
)

#: ``(capability, the phrase every skip for it must contain)`` — the same table
#: shape ``hscc-roles/tests/hermes_gap.py`` uses, so a guard can pin that no
#: second wording of this reason appears elsewhere in the leg.
CAPABILITIES = (
    ("the real hermes-agent install (checkout + venv interpreter)",
     "needs the real Hermes install to run the installer against it"),
)
