"""End-to-end: the openai_compat plugin against the REAL hermes core ABC and
the REAL openai SDK, over the same fake /v1/videos server (card t_ec488ce3).

The stubbed suite (test_video_gen_openai_compat.py) proves the provider logic;
this file proves the two things stubs cannot: the plugin satisfies the real
``agent.video_gen_provider.VideoGenProvider`` ABC (instantiable — no unmet
abstractmethods — and its response dicts match the core response shape), and
the actual ``openai`` SDK speaks the fake server's create/poll/content wire.

Skips cleanly when hermes core or the openai SDK is not importable (e.g. a
stripped CI python) — the stubbed suite still runs everywhere.
"""

import threading

import pytest

from _video_plugin_harness import FAKE_MP4, make_handler, FakeSGLang, _PLUGIN_PATH  # noqa: F401

try:
    import openai  # noqa: F401
    from agent import video_gen_provider as core_vgp
    import httpx
    _CORE_OK = True
    _CORE_SKIP = ""
except Exception as exc:  # pragma: no cover - environment-dependent
    _CORE_OK = False
    _CORE_SKIP = f"hermes core or openai SDK not importable here: {exc}"

pytestmark = pytest.mark.skipif(not _CORE_OK, reason=_CORE_SKIP)


@pytest.fixture()
def real_fake_server():
    fake = FakeSGLang()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    fake.base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        yield fake
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


@pytest.fixture()
def real_plugin(tmp_path, monkeypatch):
    """Import the plugin WITHOUT stubbing core — the real ABC and helpers load.
    Only the secret scope and config are redirected (no live credentials or
    operator config in a test; the config-resolution ladder is covered by the
    stubbed suite)."""
    monkeypatch.setenv("HSCC_PROXY_API_KEY", "sk-test-fake")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # cache/videos lands here
    import importlib.util
    spec = importlib.util.spec_from_file_location("hscc_openai_compat_real", _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_config_block", lambda: {})
    return module


def test_plugin_satisfies_real_abc(real_plugin):
    p = real_plugin.OpenAICompatVideoGenProvider()
    assert isinstance(p, core_vgp.VideoGenProvider)
    caps = p.capabilities()
    assert set(caps["modalities"]) == {"text", "image"}
    assert "max_duration" not in caps and "min_duration" not in caps  # ruling Q5


def test_real_sdk_round_trip_against_fake_server(real_plugin, real_fake_server, tmp_path):
    """openai.OpenAI(videos.create/retrieve/download_content) -> real provider
    -> saved MP4 under $HERMES_HOME/cache/videos/ — the whole client chain."""
    p = real_plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: real_fake_server.base_url
    p._poll_interval = lambda: 0.05

    res = p.generate("a corgi on a skateboard", model="Wan2.2-T2V-A14B",
                     duration=5, aspect_ratio="16:9", resolution="720p")
    assert res["success"] is True, res.get("error")
    assert res["provider"] == "openai_compat"
    # response shape == core success_response keys
    for key in ("success", "video", "model", "prompt", "modality", "aspect_ratio",
                "duration", "provider"):
        assert key in res, f"missing core response key: {key}"
    assert res["duration"] == 5
    # the MP4 really landed in the (redirected) hermes cache
    from pathlib import Path
    saved = list(Path(tmp_path, "cache", "videos").glob("openai_compat_*.mp4"))
    assert saved, "provider did not save the downloaded bytes via core save_bytes_video"
    assert saved[0].read_bytes() == FAKE_MP4
    # the SDK sent the bearer key on every hop
    assert real_fake_server.content_headers == ["Bearer sk-test-fake"]


def test_real_sdk_relative_url_download_uses_core_save_url(real_plugin, real_fake_server, tmp_path):
    """mode=url: the completed job hands a root-relative content URL; core
    save_url(trusted_origin) must fetch it from the fake proxy and save."""
    real_fake_server.mode = "url"
    p = real_plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: real_fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("relative url cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is True, res.get("error")
    from pathlib import Path
    assert list(Path(tmp_path, "cache", "videos").glob("openai_compat_*.mp4"))
    assert real_fake_server.content_headers == ["Bearer sk-test-fake"]


def test_core_registry_accepts_the_plugin(real_plugin):
    """ctx.register_video_gen_provider's contract end-to-end: the instance the
    plugin registers passes the registry's isinstance gate (what
    PluginContext._register_scoped_provider enforces at load time)."""
    registered = []

    class _Ctx:
        def register_video_gen_provider(self, provider):
            assert isinstance(provider, core_vgp.VideoGenProvider)
            registered.append(provider)

    real_plugin.register(_Ctx())
    assert len(registered) == 1 and registered[0].name == "openai_compat"


def test_real_httpx_client_close_is_invoked(real_plugin, real_fake_server, monkeypatch):
    """The provider closes its SDK client in finally (no socket leak per call)."""
    closed = {"n": 0}
    real_client = openai.OpenAI

    class WatchingClient(real_client):
        def close(self_inner):
            closed["n"] += 1
            super().close()

    # The plugin imports openai lazily inside generate(); patch the package
    # attribute so its `import openai` sees the watching class.
    monkeypatch.setattr(openai, "OpenAI", WatchingClient)
    p = real_plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: real_fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("closing cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is True
    assert closed["n"] == 1
