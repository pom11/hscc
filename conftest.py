"""Repo-root pytest plugin: keep the hyphenated plugin payload dirs out of
Python's package machinery (t_0c4989d7).

WHY THIS FILE EXISTS. HSCC's plugin dirs — ``hscc-cluster``, ``hscc-api``,
``hscc-commands``, ``sparkrun-hermes``, ``comfyui-sparkrun`` — each carry an
``__init__.py`` because Hermes loads a plugin payload *as a package* (its entry
point is ``register(ctx)``), not because the directory is an importable Python
package. It cannot be: ``hscc-cluster`` is not a valid module name.

pytest does not know that. Its default directory collector turns any directory
holding ``__init__.py`` into a ``Package`` node and imports that file when the
node is set up. For a hyphenated dir the module pytest synthesizes is named
``__init__`` with no parent package, so the payload's own ``from . import
schemas`` raises::

    ImportError: attempted relative import with no known parent package

— an error that names neither the payload nor collection, and reads like a bug
in whatever the engineer just wrote.

WHEN IT BITES. Only when pytest's rootdir is the repo root, which is what
happens when ONE process collects more than one test leg:

    pytest hscc-cluster/tests/test_ops.py hscc_daemon/tests/test_plugin_manifest_drift.py

rootdir is then the common ancestor (the repo root), and the walk down to
``hscc-cluster/tests/`` passes *through* ``hscc-cluster/``, so a ``Package``
node for the payload is created and set up before any test runs. Each leg on its
own never sees it: ``hscc-cluster/tests`` carries its own ``pytest.ini`` (so its
leg's rootdir is inside it, and this file is above that leg's confcutdir and
never loaded), and the daemon leg's walk never passes through a hyphenated
payload. That is why ``scripts/run_tests.sh`` — one pytest process per leg —
never showed this, and why the failure looked new every time it was hit.

THE FIX. Collect such dirs as plain ``Dir`` nodes: pytest still recurses into
them (collecting ``hscc-cluster/tests/`` is the whole point) but never imports
the payload's ``__init__.py``. ``tryfirst`` so the short-circuit does not depend
on plugin registration order, and the guard is ``isidentifier()`` rather than a
hardcoded dir list, so a sixth hyphenated payload is covered the day it lands.

WHAT THIS DOES *NOT* CHANGE. Legs remain separate pytest processes BY DESIGN —
see the root ``pytest.ini`` comment and ``scripts/run_tests.sh``. Each leg's
conftest puts its own dir on ``sys.path`` and imports modules bare
(``import clusterlib``), so two legs in one process can still collide in
``sys.modules`` on a shared basename. This file removes one specific *cryptic*
failure mode; it does not make cross-leg runs a supported workflow.
"""

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collect_directory(path, parent):
    """Collect a non-importable-named payload dir as a plain directory.

    Returns ``None`` (pytest's own rules) for every directory whose name *is* a
    valid module name — including the real packages here (``hscc_daemon``,
    ``memori_byodb``, ``hscc_cli``), which must keep being ``Package`` nodes.
    """
    if path.name.isidentifier():
        return None
    try:
        has_pkginit = (path / "__init__.py").is_file()
    except PermissionError:
        # Same accommodation pytest's own collector makes for unreadable dirs.
        return None
    if not has_pkginit:
        return None
    return pytest.Dir.from_parent(parent, path=path)
