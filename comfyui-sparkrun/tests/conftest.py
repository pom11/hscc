"""Offline fixtures for the comfyui-sparkrun suite.

The fake proxy itself lives in ``fake_proxy.py`` (pytest-free, so an
integration probe can reuse it inside ComfyUI's own venv). This module only
wires it up for pytest and enforces the two properties the pack's tests are
only credible because of:

1. **no network** — an autouse guard refuses any request whose host is not
   loopback, so a fixture typo pointing at the operator's real :4000 proxy
   fails loudly instead of quietly passing against live infrastructure;
2. **importable without torch/ComfyUI** — a torch stand-in is installed only
   when the real torch is absent, so the same assertions run in the repo's
   torch-less venv and, where torch does exist, against the real thing.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

import pytest

# This dir goes on sys.path so the test modules can `import pack_loader` /
# `import fake_proxy` bare — the same pattern every other plugin dir's conftest
# uses under scripts/run_tests.sh (one pytest process per dir).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fake_proxy  # noqa: E402
from fake_proxy import (  # noqa: E402,F401  (re-exported for the test modules)
    CONTAINER_BYTES,
    FakeProxy,
    FakeTensor,
    FakeVideoFromFile,
    MODELS_PAYLOAD,
    PNG_B64,
    PNG_BYTES,
    mask_batch,
    rgb_batch,
)


@pytest.fixture
def proxy():
    p = FakeProxy().start()
    try:
        yield p
    finally:
        p.stop()


# ── fake torch: installed only when the real thing is absent ─────────────────
# media.png_bytes_to_image_tensor() must hand back a torch tensor. The repo
# suite has no torch, so a minimal stand-in is registered under the name
# 'torch' BEFORE the pack is loaded; when real torch IS importable it is left
# alone and the same assertions run against it.

@pytest.fixture(autouse=True)
def torch_stand_in(monkeypatch):
    """Guarantee a torch-shaped module for the decode path; keep the real one."""
    try:
        import torch  # noqa: F401
        yield "real"
        return
    except ImportError:
        pass
    monkeypatch.setitem(sys.modules, "torch", fake_proxy.fake_torch_module())
    yield "fake"


@pytest.fixture
def comfy_api_stand_in(monkeypatch):
    """Register a fake ``comfy_api.latest.InputImpl`` so video output is typed."""
    import types

    impl = types.ModuleType("comfy_api.latest.InputImpl")
    impl.VideoFromFile = FakeVideoFromFile
    latest = types.ModuleType("comfy_api.latest")
    latest.InputImpl = impl
    pkg = types.ModuleType("comfy_api")
    pkg.latest = latest
    monkeypatch.setitem(sys.modules, "comfy_api", pkg)
    monkeypatch.setitem(sys.modules, "comfy_api.latest", latest)
    monkeypatch.setitem(sys.modules, "comfy_api.latest.InputImpl", impl)
    FakeVideoFromFile.instances = []
    return impl


# ── hard "no network" guard ──────────────────────────────────────────────────
# "No network in tests" is only a fact if something enforces it: a fixture typo
# that points base_url at the real proxy would otherwise pass every assertion
# while the operator's proxy happens to be up, and fail mysteriously when it is
# down. This wraps urlopen itself (http_client calls it through the module
# attribute, so the patch is seen) and refuses any non-loopback target.

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    real = urllib.request.urlopen

    def guarded(req, *a, **kw):
        url = req if isinstance(req, str) else getattr(req, "full_url", "") or ""
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in _ALLOWED_HOSTS:
            raise AssertionError(
                f"test attempted a NON-LOCAL network request to {url!r} — the "
                f"pack's suite must be fully offline (fake proxy only)")
        return real(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)
    yield


@pytest.fixture(autouse=True)
def discovery_guard(monkeypatch):
    """Default the dropdown fetch OFF, and clear the discovery cache per test.

    ``INPUT_TYPES`` fetches ``/v1/models``; left alone it would ask the
    placeholder host committed as the default base_url — a real DNS lookup in
    the suite. Tests that want the real dropdown path request the
    ``live_discovery`` fixture, which re-enables it.
    """
    from pack_loader import load_pack

    monkeypatch.setattr(load_pack("nodes"), "_DISCOVERY_CACHE", {})
    monkeypatch.setenv("SPARKRUN_DISABLE_MODEL_DISCOVERY", "1")
    yield


@pytest.fixture
def live_discovery(monkeypatch):
    """Re-enable the dropdown's /v1/models fetch for a test that needs it.

    An explicit fixture rather than a custom mark: registering a mark means
    editing the root pytest.ini, which is outside this card's file set. The
    autouse guard above has already set the env var for this test, so undoing
    it here is the whole job.
    """
    monkeypatch.delenv("SPARKRUN_DISABLE_MODEL_DISCOVERY", raising=False)
    return True
