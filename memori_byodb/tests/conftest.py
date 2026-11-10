"""Fixtures for the memori_byodb suite.

Same convention as every other plugin dir's conftest under
``scripts/run_tests.sh`` (one pytest process per dir): put the dir this suite
belongs to on ``sys.path`` so modules can be imported the way they are at
runtime — the deployed ``sys.path`` entry is ``~/.hermes/plugins``, and the
payload copy excludes ``tests/``, so ``import _cache_env`` bare is how the
helper module is reachable in production and therefore here.

The **repo root is deliberately NOT added**. The repo's ``memori/`` dir — the
cloud memory provider — shadows the installed ``memori`` SDK for any process
whose ``sys.path`` contains the directory that holds it. Measured on this card:
a child with the repo root on ``PYTHONPATH`` failed with
``ImportError: cannot import name 'embed_texts' from 'memori'
(.../hscc/memori/__init__.py)``. A suite that put the repo root on ``sys.path``
would hand that trap to the next test in this dir that touches the SDK. The one
test here that must import the provider by package name builds a one-symlink
shim dir instead (``test_cache_env.py::_provider_shim_dir``), which is also
closer to how Hermes loads plugins — by file location, not by path.

Nothing here touches the network, ``~/.hermes`` or ``~/.hscc``: every path the
tests use is a ``tmp_path`` (the helper takes ``$HOME`` / ``FASTEMBED_CACHE_DIR``
as its only inputs, so there is nothing to reach for).
"""

from __future__ import annotations

import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PLUGIN_DIR)

if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
