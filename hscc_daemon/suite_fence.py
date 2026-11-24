"""The one in-suite predicate of this package (t_fdbc24f9).

THE QUESTION
------------
"Is a test suite driving this code — in this process, or in any process that suite
SPAWNED?" Every fence in this repo that protects operator state (a doctor row's
default ``~/.hscc`` read, an ``os.kill`` liveness probe, a service-manager query)
answers that question before it touches anything, and the answer is only worth having
if it is the SAME answer everywhere. A fence whose copy reads only
``"pytest" in sys.modules`` is a fence that guards the process nobody is asking about:
the door that reads operator state is a FRESH interpreter the suite item exec'd, and
pytest was never imported there.

THE TWO TESTS, AND WHY BOTH
---------------------------
``PYTEST_CURRENT_TEST`` is the channel that crosses the spawn: pytest exports it for
the duration of each item and every subprocess of that item inherits it. The
``sys.modules`` test covers the other direction — code reached inside the suite process
itself during a phase where no item is active (collection, a plugin's own hooks). Either
alone leaves a hole, so neither is optional here.

THE RULING (t_fdbc24f9): ONE PREDICATE PER PACKAGE, PINNED TO AGREE
--------------------------------------------------------------------
Two packages ask this question and they do not share a Python namespace:

* ``hscc-bootstrap/doctor_rows.py`` is a FLAT module. Its imports resolve only when
  its own directory is ``sys.path[0]`` — i.e. when ``doctor.py`` runs as a script,
  which is how the door and the installer run it. A module inside this package cannot
  import it: the deployed daemon plugin (``~/.hermes/plugins/hscc_daemon``) has no
  bootstrap directory to find, and a fence that raises ``ImportError`` on the fleet
  node is worse than no fence.
* Delegating the other way — ``doctor_rows.under_pytest()`` calling into this package
  — would put a plugin-package import on the fence that guards bootstrap's Stage-1
  gate, which must answer on a fresh install where ``hscc_daemon`` is not deployed
  yet (``hscc-bootstrap/_theme.py`` documents exactly that case for a much smaller
  dependency). A guarded import that falls back would have to restate this same
  expression in the fallback, so it buys no single copy and makes a safety line
  failable for the first time.

So the rule is duplicated deliberately, once per package, each copy stdlib-only and
unable to fail — and it is PINNED, not trusted:
``hscc_daemon/tests/test_suite_fence_agrees_with_doctor_rows_t_fdbc24f9.py`` loads the
bootstrap predicate and asserts both answer identically for every combination of the
two signals. That pin is what separates a tolerated copy from the drift t_eb768c2a
warns about ("a copy without the marker is a fence that only guards the process nobody
is asking about").

SCOPE: this predicate answers the question for THIS package. Nothing here is a general
"are we in CI" test — it says nothing about whether a sandbox exists, only that a
suite is driving, which is when a default root may not be read.
"""

from __future__ import annotations

import os
import sys

#: The marker pytest exports for the duration of EACH item — and which every
#: subprocess of that item therefore inherits. The same channel the rest of the
#: repo already asks of (``hscc_daemon/main_cli.py``, ``hscc-api/repl_inject.py``,
#: ``hscc-project/flightdeck/core/tmux_chat.py``), rather than a second invented one.
PYTEST_MARKER = "PYTEST_CURRENT_TEST"


def under_pytest() -> bool:
    """True in a pytest process **or in any process a pytest process spawned**.

    The fence this serves is "a suite must never read the operator's state". Use it
    in front of any probe whose answer depends on the operator's machine — a
    default-rooted file read, a pid-file liveness probe, a service manager — and
    refuse with the seam a caller must inject instead of reading it.
    """
    return "pytest" in sys.modules or bool(os.environ.get(PYTEST_MARKER))
