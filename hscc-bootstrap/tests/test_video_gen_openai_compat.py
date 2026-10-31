"""openai_compat video-gen provider — contract tests against a fake server.

Card t_ec488ce3 (media M6). The fixture lifecycle mirrors
docs/design/media-serving.md §2 (POST /v1/videos -> queued -> poll
GET /v1/videos/{id} -> completed -> GET /v1/videos/{id}/content) and pins the
operator ruling §8 Q5: the client carries NO deadline and NO size/seconds
caps — the ~5 s polling interval is the only pacing rule. Everything here runs
against a fake stdlib server (operator ruling: no live media units in tests).
"""

import base64
import time

import pytest

from _video_plugin_harness import (  # noqa: F401  — fixtures + helpers
    FAKE_MP4, fake_server, plugin, set_cfg, set_key)

KEY = "sk-fake-proxy-master-key"


# ── identity / availability ──────────────────────────────────────────────────

def test_provider_name_matches_config_key(plugin):
    """video_gen.provider: openai_compat must resolve to this provider."""
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.name == "openai_compat"


def test_is_available_false_without_any_key(plugin):
    """No key anywhere => not available (the registry's auto-select rule)."""
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.is_available() is False


def test_is_available_true_with_proxy_key_env(plugin):
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.is_available() is True


def test_is_available_true_with_key_env_indirection(plugin):
    """api_key_env names the variable holding the bearer, so a fleet can reuse
    its existing proxy-key variable name instead of duplicating the value."""
    set_cfg(openai_compat={"api_key_env": "LITELLM_MASTER_KEY"})
    set_key(LITELLM_MASTER_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.is_available() is True


def test_is_available_never_raises(plugin, monkeypatch):
    """Picker contract: is_available() must not raise (CatalogProviderBase) —
    patch the binding the plugin actually imported, not the stub class."""
    def boom(name, default=""):
        raise RuntimeError("scope exploded")

    monkeypatch.setattr(plugin, "get_secret_str", boom)
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.is_available() is False


# ── model / base-url resolution ──────────────────────────────────────────────

def test_default_model_is_ruled_first_tier(plugin):
    """§8 Q1 ruling: the video tier is Wan2.2-T2V-A14B — the concrete alias the
    fleet template will serve. Sent verbatim; no renaming."""
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.default_model() == "Wan2.2-T2V-A14B"


def test_config_model_overrides_and_is_sent_verbatim(plugin, fake_server):
    set_key(HSCC_PROXY_API_KEY=KEY)
    set_cfg(model="video-top")  # video_gen.model (top-level, tool-preferred)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    res = p.generate("a cat", model=p.default_model())
    assert res["success"] is True
    assert fake_server.create_calls[-1]["model"] == "video-top"


def test_provider_scoped_model_used_when_top_level_absent(plugin):
    set_cfg(openai_compat={"model": "my-cool-model"})
    p = plugin.OpenAICompatVideoGenProvider()
    assert p.default_model() == "my-cool-model"


def test_base_url_default_is_the_proxy(plugin):
    """Media-serving §7: clients talk to the proxy at :4000, never a node."""
    p = plugin.OpenAICompatVideoGenProvider()
    assert p._base_url().endswith(":4000/v1")


def test_base_url_env_then_config(plugin, monkeypatch):
    monkeypatch.setenv("HSCC_VIDEO_BASE_URL", "http://10.0.0.x:4000/v1")
    p = plugin.OpenAICompatVideoGenProvider()
    assert p._base_url() == "http://10.0.0.x:4000/v1"
    monkeypatch.delenv("HSCC_VIDEO_BASE_URL")
    set_cfg(openai_compat={"base_url": "http://10.0.0.x:4000/v1/"})
    assert p._base_url() == "http://10.0.0.x:4000/v1"  # trailing slash normalized


# ── job lifecycle (the M0 §2 contract) ───────────────────────────────────────

def test_generate_happy_path_polls_to_completed_and_downloads(plugin, fake_server):
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05  # pacing is contract; speed is test time
    res = p.generate("a cat surfing", model="Wan2.2-T2V-A14B")

    assert res["success"] is True, res
    assert res["provider"] == "openai_compat"
    assert res["model"] == "Wan2.2-T2V-A14B"
    assert res["modality"] == "text"
    assert res["job_id"] == "job1"
    # created once, polled at least once, content fetched once — the full §2 chain
    assert len(fake_server.create_calls) == 1
    assert fake_server.poll_count >= 1
    assert fake_server.content_headers == [f"Bearer {KEY}"]
    # every request carried the bearer (proxy auth posture, §7)
    assert f"Bearer {KEY}" in fake_server.auth_seen


def test_create_body_carries_openai_video_fields(plugin, fake_server):
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("a cat", model="Wan2.2-T2V-A14B", duration=5,
                     aspect_ratio="16:9", resolution="720p", seed=42,
                     negative_prompt="blur")
    assert res["success"] is True
    body = fake_server.create_calls[-1]
    assert body["prompt"] == "a cat"
    assert body["model"] == "Wan2.2-T2V-A14B"
    assert body["seconds"] == "5"            # OpenAI video API: seconds is a string
    assert body["size"] == "1280x720"        # tier x ratio
    assert body["seed"] == 42                # provider-extension fields ride flattened
    assert body["negative_prompt"] == "blur"


def test_relative_content_url_resolves_against_base(plugin, fake_server):
    """§2 'b64_json/url exactness': SGLang returns RELATIVE /v1/videos/<id>/content;
    the client resolves it against the configured base (proxy-side resolution)."""
    fake_server.mode = "url"
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is True
    # saved via provider_media.save_url with trusted_origin (the URL was built
    # from the operator's own base_url — LAN proxy must not trip the SSRF gate)
    url_calls = [c for c in _pcalls() if c[0] == "url"]
    assert url_calls, "save_url_video was not used"
    _, kind, url, headers, trusted = url_calls[-1]
    assert url == fake_server.base_url + "/videos/job1/content"
    assert trusted is True
    assert headers == {"Authorization": f"Bearer {KEY}"}


def test_download_endpoint_used_when_no_delivery_url(plugin, fake_server):
    """Default fake: completed job has no data[] -> GET /v1/videos/{id}/content."""
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is True
    bytes_calls = [c for c in _pcalls() if c[0] == "bytes"]
    assert bytes_calls and bytes_calls[-1][2] == len(FAKE_MP4)


def _pcalls():
    import _video_plugin_harness as h
    return h._ProviderMedia.calls


# ── failure paths ────────────────────────────────────────────────────────────

def test_failed_job_surfaces_server_error(plugin, fake_server):
    fake_server.mode = "fail"
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is False
    assert res["error_type"] == "job_failed"
    assert "denoise OOM" in res["error"]


def test_poll_404_fails_fast_job_gone(plugin, fake_server):
    """Job stores are process-local (§2): a 404 mid-poll means the unit
    restarted and the job is dead — poll a ghost forever would be the bug."""
    fake_server.mode = "poll404"
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    t0 = time.monotonic()
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is False
    assert res["error_type"] == "job_failed"
    assert "re-submit" in res["error"]
    assert time.monotonic() - t0 < 5  # failed fast, did not keep polling


def test_create_400_reported_as_api_error(plugin, fake_server):
    """A size the model does not support is the SERVER's 400 (no client caps) —
    the provider surfaces it, it does not clamp or retry."""
    fake_server.mode = "create400"
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is False
    assert res["error_type"] == "api_error"
    assert "Invalid size" in res["error"]


def test_missing_key_is_missing_credentials(plugin, fake_server):
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    res = p.generate("a cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is False
    assert res["error_type"] == "missing_credentials"


def test_empty_prompt_invalid_request(plugin):
    p = plugin.OpenAICompatVideoGenProvider()
    res = p.generate("   ")
    assert res["success"] is False
    assert res["error_type"] == "invalid_request"


# ── ruling Q5: pacing only, no deadline, no caps ─────────────────────────────

def test_default_poll_interval_is_five_seconds(plugin):
    p = plugin.OpenAICompatVideoGenProvider()
    assert p._poll_interval() == 5.0


def test_poll_interval_config_and_env_override(plugin, monkeypatch):
    set_cfg(openai_compat={"poll_interval_s": 2.5})
    p = plugin.OpenAICompatVideoGenProvider()
    assert p._poll_interval() == 2.5
    monkeypatch.setenv("HSCC_VIDEO_POLL_INTERVAL_S", "1")
    assert p._poll_interval() == 1.0
    monkeypatch.setenv("HSCC_VIDEO_POLL_INTERVAL_S", "junk")
    assert p._poll_interval() == 5.0  # junk falls back to the default


def test_long_running_job_keeps_polling_without_deadline(plugin, fake_server, monkeypatch):
    """§8 Q5: the client polls until terminal. Feed 6 'in_progress' polls
    (~30 s of job time at the real 5 s pacing) and assert success — with the
    deleted 1800 s deadline this shape of job is exactly what must NOT fail."""
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.02
    n_progress = 6

    def status_for(n):
        return ("completed", None) if n > n_progress else ("in_progress", None)

    monkeypatch.setattr(fake_server, "status_for", status_for)
    res = p.generate("a long cat", model="Wan2.2-T2V-A14B")
    assert res["success"] is True
    assert fake_server.poll_count == n_progress + 1  # polls 1..n stay in_progress, n+1 completes


def test_no_duration_bounds_in_capabilities(plugin):
    """The dynamic video_generate schema clamps duration ONLY when both
    min_duration+max_duration are present; Q5 forbids shipping them."""
    caps = plugin.OpenAICompatVideoGenProvider().capabilities()
    assert "max_duration" not in caps
    assert "min_duration" not in caps


def test_unknown_resolution_sends_no_size(plugin, fake_server):
    """Tier defaults are defaults, not ceilings: an unmapped tier omits size and
    the server applies its own default."""
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("a cat", model="Wan2.2-T2V-A14B", resolution="4K")
    assert res["success"] is True
    assert "size" not in fake_server.create_calls[-1]


# ── image-to-video routing (reference_url vs input_reference) ───────────────

def test_http_image_routes_as_reference_url(plugin, fake_server):
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("animate this", model="Wan2.2-T2V-A14B",
                     image_url="https://example.org/frame.png")
    assert res["success"] is True
    assert res["modality"] == "image"
    assert fake_server.create_calls[-1]["reference_url"] == "https://example.org/frame.png"


def test_data_url_image_routes_as_input_reference_bytes(plugin, fake_server):
    """The tool's confinement chokepoint hands non-local backends data: URLs —
    decode to bytes and POST as multipart input_reference."""
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    b64 = base64.b64encode(b"png-bytes").decode()
    res = p.generate("animate this", model="Wan2.2-T2V-A14B",
                     image_url=f"data:image/png;base64,{b64}")
    assert res["success"] is True
    assert fake_server.create_multipart, "expected a multipart create"
    ctype, raw = fake_server.create_multipart[-1]
    assert "multipart/form-data" in ctype
    assert b"input_reference" in raw and b"png-bytes" in raw


def test_reference_image_urls_falls_back_to_first(plugin, fake_server):
    set_key(HSCC_PROXY_API_KEY=KEY)
    p = plugin.OpenAICompatVideoGenProvider()
    p._base_url = lambda: fake_server.base_url
    p._poll_interval = lambda: 0.05
    res = p.generate("animate", model="Wan2.2-T2V-A14B",
                     reference_image_urls=["https://example.org/a.png",
                                           "https://example.org/b.png"])
    assert res["success"] is True
    assert fake_server.create_calls[-1]["reference_url"] == "https://example.org/a.png"


def test_malformed_data_url_is_invalid_request(plugin):
    set_key(HSCC_PROXY_API_KEY=KEY)  # key present so we reach reference handling
    p = plugin.OpenAICompatVideoGenProvider()
    res = p.generate("animate", model="m", image_url="data:image/png;base64,!!!notbase64!!!")
    assert res["success"] is False
    assert res["error_type"] == "invalid_request"


# ── plugin registration contract ─────────────────────────────────────────────

class _Ctx:
    def __init__(self):
        self.registered = []

    def register_video_gen_provider(self, provider):
        self.registered.append(provider)


def test_register_wires_instance_into_registry(plugin):
    ctx = _Ctx()
    plugin.register(ctx)
    assert len(ctx.registered) == 1
    assert ctx.registered[0].name == "openai_compat"
