"""Where this machine's real ``hermes-agent`` checkout is — the leg's seam (t_5b560adf).

WHY THIS EXISTS. Twenty-four cases here run ``bootstrap.sh`` with the REAL Hermes
venv interpreter (a fabricated home symlinks the real ``hermes-agent`` tree in, so
the installer's stages execute for real), and eleven more in
``test_skill_resolver_xval.py`` cross-check the skill-resolution mirror against the
LIVE resolver in that same tree. Both gates were spelled

    REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    needs_runtime = pytest.mark.skipif(not REAL_VENV_PY.is_file(),
                                       reason="needs the real Hermes venv interpreter …")

i.e. one candidate, derived from ``~``, and no way for a machine to declare the
tree it does have. Measured at ``96a9f38a`` (card t_2b2a6c3b's two-arm roster) the
roster-wide HOME seal in ``scripts/run_tests.sh`` takes ``hscc-bootstrap`` from
``1329 passed`` to ``1294 passed, 35 skipped`` — and that subset alone went **230.35
s unsealed → 2.74 s sealed**. A leg that gets 80x faster by deleting its most
expensive coverage prints green and reads as "nothing was lost". That is the failure
this module closes.

WHAT THE SEAM IS, in the shape ``hscc-cluster`` proved for sparkrun (PR #604,
``test_media_recipes.py`` + ``test_sparkrun_checkout_seam.py``):

  1. ``HERMES_AGENT_PATH`` is the documented pin and, when set, it is the ANSWER —
     nothing else is consulted. So a machine that declares its checkout keeps
     running these 35 cases no matter where ``HOME`` points, and its skip reason
     cannot name a home at all.
  2. With no pin, the candidates are a checkout *beside this repo* (derived from
     ``__file__``, so a seal cannot move it) and then the login-home default — the
     one candidate a seal blinds, and it is tried last.
  3. A machine with neither skips, and the reason names the capability that went
     untested, every path it tried and why, and the pin in the pasteable
     ``HERMES_AGENT_PATH=<path>`` form. ``-rs`` alone has to be enough for a reader
     to rule out "the harness moved my HOME".

SAME NAME AS PRODUCTION, ON PURPOSE. ``HERMES_AGENT_PATH`` is already the seam
``hscc_daemon/hermes_import.py`` (:data:`DEFAULT_AGENT_PATH`),
``hscc_daemon/autodown.py:875``, ``hscc_daemon/kanban_live.py:181`` and
``hscc-roles/hscc.py:398`` honour, and it is the variable
``scripts/run_tests.sh`` declares for the machine's own tree before it seals the
roster (PR #606). One name, one meaning: the root of a ``hermes-agent`` CHECKOUT —
the directory holding ``agent/``/``hermes_cli/`` and ``venv/bin/python``.
``test_hermes_agent_tree_seam.py`` pins the name against those spellings so a
rename on either side reddens here.

Relationship to the daemon leg's locator (t_ce473bf0, landed in #620): production's owner
of this question is ``hscc_daemon/hermes_import.resolve_agent_tree()`` (``HERMES_AGENT_PATH``
then ``DEFAULT_AGENT_PATH``) and ``hscc_daemon/tests/hermes_cli_plant.py::agent_tree_root``
is its harness twin; #620's sweep reddens a ``hscc_daemon`` test that spells the tree out of
the home instead of asking them. This module deliberately does not import either — this leg
spawns the tree's *interpreter* as a subprocess, so it needs an existence gate on
``venv/bin/python`` and a repo-relative candidate for an undeclared checkout (the #604
``SPARKRUN_SRC`` posture), and neither leg seeds the other's harness sys.path. What is
shared is the contract: same pin, same precedence for a declared pin, same rule that a
home-derived spelling is not an answer (the default at the end of ``agent_candidates`` is
``hermes_import.DEFAULT_AGENT_PATH``'s value, kept as the last candidate and nothing more).
If a third leg grows the same need, the smallest upstream addition is one harness module
both legs import — not a third copy of this resolver.

``HERMES_HOME`` IS NOT THIS PATH and is never consulted here. Hermes exports
``HERMES_HOME`` at a *profile* directory (``…/profiles/<name>``) for every
dispatched worker, so a ``$HERMES_HOME/hermes-agent`` join names a path that
cannot exist — the exact bug t_8579fba4 removed from ``apply_patches.py`` and
t_417610e6 removed from ``ensure_review_feature.py``. Reading it here would
re-import that bug into 35 cases.

CALL TIME, NOT IMPORT. Every function below reads ``os.environ`` and ``Path.home()``
when it is called, never at module scope: a frozen constant is a collection-time
answer about a filesystem another process may still change, which is the shape
``test_hermes_agent_tree_seam.py::test_the_locator_freezes_nothing_at_import``
reddens. A ``skipif`` marker condition is unavoidably evaluated at collection, so
the gate stays a marker (cheap, keeps a machine without the tree out of collection)
while the PATHS the cases actually use come from these functions.
"""

import os
from pathlib import Path

import pytest

#: This repo's root (``…/hscc``), ``__file__``-derived so ``HOME`` cannot move it.
REPO = Path(__file__).resolve().parent.parent.parent

#: The documented pin. Same spelling and same meaning as production's
#: ``HERMES_AGENT_PATH`` (module docstring). Read per call, never cached.
AGENT_PIN = "HERMES_AGENT_PATH"

#: What makes a candidate the real thing rather than a directory that happens to
#: be named ``hermes-agent``: a checkout AND its venv interpreter. The installer
#: cases spawn that interpreter; the resolver cases spawn it against ``agent/``.
VENV_PYTHON_PARTS = ("venv", "bin", "python")


def agent_candidates():
    """``[(why, path)]``, best first — see the module docstring for the order.

    An explicit ``HERMES_AGENT_PATH`` short-circuits the list: a pin is the
    operator's answer, not a hint, and consulting ``~`` after one would put a home
    path back into the skip reason, which is precisely what a reader has to be
    able to rule out.
    """
    pinned = os.environ.get(AGENT_PIN)
    if pinned:
        return [(f"{AGENT_PIN} pin", Path(pinned))]
    return [("sibling checkout beside this repo", REPO.parent / "hermes-agent"),
            ("login-home default (production's DEFAULT_AGENT_PATH)",
             Path.home() / ".hermes" / "hermes-agent")]


def is_checkout(path):
    """True when *path* is a hermes-agent checkout this leg can spawn."""
    return path.is_dir() and path.joinpath(*VENV_PYTHON_PARTS).is_file()


def resolve_runtime():
    """``(agent_dir, venv_python)`` from the first usable candidate, else ``None``.

    Per call. Both halves come from the SAME candidate so a case can never symlink
    one checkout's ``venv`` and spawn another's interpreter.
    """
    for _why, cand in agent_candidates():
        if is_checkout(cand):
            return cand, cand.joinpath(*VENV_PYTHON_PARTS)
    return None


def runtime_available():
    """Bool for a ``skipif`` condition (collection time); no exception path."""
    return resolve_runtime() is not None


def runtime_skip_reason(capability):
    """The reason an honest machine-less skip prints: what, where, and the fix.

    ``capability`` is the caller's own words for what the cases do (module name
    included), because "skipped" without "skipped WHAT" is how a leg quietly stops
    testing. The pin is spelled in the ``NAME=<value>`` form on purpose: probe p3
    in #604's battery survived a reason that only mentioned the name in prose,
    because the candidate labels in the same string already spell it.
    """
    tried = " | ".join(f"{path} [{why}]" for why, path in agent_candidates())
    return (
        f"{capability} needs a real hermes-agent checkout (a dir with "
        f"{'/'.join(VENV_PYTHON_PARTS)}) and none was found at any candidate "
        f"({tried}). Set {AGENT_PIN}=<path to the hermes-agent checkout holding "
        f"{'/'.join(VENV_PYTHON_PARTS)}> to run it here; without one these "
        f"cases test nothing, so they skip rather than pass on a stub.")


def runtime_or_skip(capability):
    """``(agent_dir, venv_python)``, or a skip that names what went untested."""
    got = resolve_runtime()
    if got is None:
        pytest.skip(runtime_skip_reason(capability))
    agent_dir, venv_python = got
    return agent_dir, venv_python


def agent_dir_or_skip(capability):
    """The checkout root to symlink into a fabricated home, or a loud skip."""
    return runtime_or_skip(capability)[0]


def venv_python_or_skip(capability):
    """The interpreter to spawn, or a loud skip. Same candidate as the dir."""
    return runtime_or_skip(capability)[1]
