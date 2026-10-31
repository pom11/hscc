"""Load the pack the way ComfyUI's loader does, for offline tests.

ComfyUI's ``load_custom_node`` (nodes.py in 0.43.2) execs ``<pack>/__init__.py``
through ``importlib.util.spec_from_file_location`` — a bare module with NO
package context and NO sys.path entry for the pack dir. A test that instead did
``import comfyui_sparkrun`` would exercise a path ComfyUI never takes and would
silently bless a relative import that fails in the real host. So this helper
reproduces the loader exactly: one spec_from_file_location per pack file, unique
module names, zero sys.path mutation.

Returns the loaded ``__init__`` namespace, whose ``NODE_CLASS_MAPPINGS`` is the
same object the loader hands ComfyUI.
"""

from __future__ import annotations

import importlib.util
import os
import sys

PACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pack(stem: str = "__init__"):
    """Load ``<pack>/<stem>.py`` as ComfyUI would; return the module object.

    Uses the SAME module-name scheme as the pack's own ``_load_sibling``
    (``comfyui_sparkrun__<stem>``), on purpose. If the test side chose its own
    prefix, every sibling would exist TWICE in ``sys.modules`` — two copies of
    ``http_client``, each with its own ``SparkrunProxyError`` class — and an
    ``except SparkrunProxyError`` in the node would not catch what the test
    raised, nor would a monkeypatch of one copy be seen by the other. A
    two-class exception bug that only exists in the test harness is the worst
    kind, so the names are shared rather than namespaced per-caller.

    Cached and NEVER re-execed by a test: the fixtures patch module attributes
    (``_DISCOVERY_CACHE``), and a reload would leave other test modules holding
    a stale object. Use :func:`probe_import` when the point IS a fresh exec.
    """
    modname = f"comfyui_sparkrun__{stem}"
    cached = sys.modules.get(modname)
    if cached is not None:
        return cached
    path = os.path.join(PACK_DIR, f"{stem}.py")
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return mod


def probe_import(stem: str):
    """Exec ``<pack>/<stem>.py`` into a THROWAWAY module name.

    Proves the file is importable in this interpreter (no torch, no comfy)
    without disturbing the cached modules the rest of the suite holds.
    """
    modname = f"comfyui_sparkrun__probe__{stem}"
    path = os.path.join(PACK_DIR, f"{stem}.py")
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return mod


def pack_file(name: str) -> str:
    return os.path.join(PACK_DIR, name)
