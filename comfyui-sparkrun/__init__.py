"""comfyui-sparkrun — ComfyUI custom-node pack for the sparkrun proxy.

ComfyUI's loader execs this file through ``importlib.util.spec_from_file_location``
with NO package context (``nodes.py:load_custom_node`` in 0.43.2), so a relative
``from .nodes import ...`` raises ``ImportError: attempted relative import with no
known parent package`` inside ComfyUI while working fine under pytest. The pack is
therefore loaded by file location here as well, and the classic
``NODE_CLASS_MAPPINGS`` / ``NODE_DISPLAY_NAME_MAPPINGS`` contract that the loader
reads is re-exported from ``nodes.py``.

Why not the V3 (``comfy_entrypoint``) form: 0.43.2 accepts either
(``NODE_CLASS_MAPPINGS`` **or** ``comfy_entrypoint``), and no ``comfy.plugin.json``
is read by the loader anywhere in that tree — so the classic layout is the one
that needs no ComfyUI import at all, which is what lets the HSCC repo suite
(torch-less, ComfyUI-less) collect this pack's tests.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_PACK_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(stem: str):
    """Import ``<pack>/<stem>.py`` under a collision-proof name (idempotent)."""
    modname = f"comfyui_sparkrun__{stem}"
    cached = sys.modules.get(modname)
    if cached is not None:
        return cached
    path = os.path.join(_PACK_DIR, f"{stem}.py")
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise ImportError(f"comfyui-sparkrun: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return mod


_nodes = _load_sibling("nodes")

SparkrunImage = _nodes.SparkrunImage
SparkrunImageEdit = _nodes.SparkrunImageEdit
SparkrunVideo = _nodes.SparkrunVideo
SparkrunProxyError = _nodes.SparkrunProxyError
MediaRuntimeMissing = _nodes.MediaRuntimeMissing
DEFAULT_BASE_URL = _nodes.DEFAULT_BASE_URL

NODE_CLASS_MAPPINGS = _nodes.NODE_CLASS_MAPPINGS
NODE_DISPLAY_NAME_MAPPINGS = _nodes.NODE_DISPLAY_NAME_MAPPINGS

#: No web assets in this pack. Stated explicitly because the loader treats an
#: absent WEB_DIRECTORY and a None one the same, and a stray value here would
#: register a phantom extension route.
WEB_DIRECTORY = None

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "SparkrunImage",
    "SparkrunImageEdit",
    "SparkrunVideo",
    "SparkrunProxyError",
    "MediaRuntimeMissing",
    "DEFAULT_BASE_URL",
]
