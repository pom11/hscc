"""Mode-filtered model dropdown tests (the M3 contract, client side)."""

from __future__ import annotations

import pytest
from pack_loader import load_pack

catalog = load_pack("catalog")
nodes = load_pack("nodes")

from conftest import MODELS_PAYLOAD  # noqa: E402


def test_image_mode_and_video_mode_are_the_litellm_enum_strings():
    assert catalog.IMAGE_MODE == "image_generation"
    assert catalog.VIDEO_MODE == "video_generation"
    assert catalog.KIND_TO_MODE == {"image": "image_generation",
                                    "video": "video_generation"}


def test_models_for_mode_lists_only_matching_entries():
    got = catalog.models_for_mode(MODELS_PAYLOAD, catalog.IMAGE_MODE)
    assert got == ["sd-diffusion-t2i", "sd-diffusion-alias"]
    got = catalog.models_for_mode(MODELS_PAYLOAD, catalog.VIDEO_MODE)
    assert got == ["wan-diffusion-t2v"]


def test_chat_and_untagged_entries_never_reach_a_media_dropdown():
    """Fail-closed: a model the proxy would route to chat must not be selectable
    by an image/video node — listing it would only produce a confusing 400."""
    for mode in (catalog.IMAGE_MODE, catalog.VIDEO_MODE):
        names = catalog.models_for_mode(MODELS_PAYLOAD, mode)
        assert "cluster-chat" not in names
        assert "legacy-untagged" not in names
        # `mode: "image"` is NOT the enum — the loose spelling must not match.
        assert "wrong-mode-spelling" not in names


def test_modes_of_reports_every_advertised_mode():
    assert catalog.modes_of(MODELS_PAYLOAD) == [
        "image_generation", "video_generation", "chat", "image"]


def test_accepts_unwrapped_entry_list():
    entries = MODELS_PAYLOAD["data"]
    assert catalog.models_for_mode(entries, catalog.VIDEO_MODE) == ["wan-diffusion-t2v"]


def test_unusable_payloads_produce_no_names_rather_than_crashing():
    for payload in (None, {}, {"data": None}, {"data": ["nonsense", 7]}, 42):
        assert catalog.models_for_mode(payload, catalog.IMAGE_MODE) == []


def test_blank_ids_are_dropped_and_untagged_entries_stay_out():
    """A bare name is not a media model: without model_info.mode nothing may be
    offered to an image or video node, however well-formed the id looks."""
    payload = {"data": [
        {"id": "  "},
        {"id": "dup"}, {"id": "dup"},
        {"model": "by-model-key"},
    ]}
    assert catalog.models_for_mode(payload, catalog.IMAGE_MODE) == []
    tagged = {"data": [{"id": "m", "model_info": {"mode": "image_generation"}},
                       {"id": "m", "model_info": {"mode": "image_generation"}}]}
    assert catalog.models_for_mode(tagged, catalog.IMAGE_MODE) == ["m"]


def test_rejects_a_non_media_mode_instead_of_returning_everything():
    with pytest.raises(ValueError):
        catalog.models_for_mode(MODELS_PAYLOAD, "chat")


# ── dropdown against the fake proxy ──────────────────────────────────────────

def test_model_widget_populates_from_the_proxy_image_mode(proxy, live_discovery):
    opts = nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    assert opts == ["sd-diffusion-t2i", "sd-diffusion-alias"]
    assert proxy.last_call("GET", "/v1/models") is not None


def test_model_widget_for_video_mode_excludes_image_models(proxy, live_discovery):
    opts = nodes._model_options(proxy.base_url, catalog.VIDEO_MODE)
    assert opts == ["wan-diffusion-t2v"]


def test_unreachable_proxy_degrades_to_the_sentinel_not_an_exception(proxy, live_discovery):
    """A closed port must not raise while ComfyUI renders the node — the
    dropdown gets the sentinel and the UI stays usable."""
    opts = nodes._model_options("http://127.0.0.1:1", catalog.IMAGE_MODE)
    assert opts == [catalog.NO_MODELS_SENTINEL]


def test_proxy_500_on_models_degrades_to_the_sentinel(proxy, live_discovery):
    proxy.state["models_status"] = 500
    opts = nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    assert opts == [catalog.NO_MODELS_SENTINEL]


def test_empty_media_registry_shows_the_sentinel(proxy, live_discovery):
    """The honest pre-fork-patch state: a proxy that lists only chat models."""
    proxy.set_models({"data": [{"id": "cluster-chat",
                                "model_info": {"mode": "chat"}}]})
    opts = nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    assert opts == [catalog.NO_MODELS_SENTINEL]


def test_discovery_is_cached_so_the_ui_is_not_stalled(proxy, live_discovery):
    nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    hits = [c for c in proxy.calls if c["path"].startswith("/v1/models")]
    assert len(hits) == 1, "second call must come from the cache"


def test_cache_is_keyed_by_mode_so_the_two_dropdowns_stay_distinct(proxy, live_discovery):
    nodes._model_options(proxy.base_url, catalog.IMAGE_MODE)
    nodes._model_options(proxy.base_url, catalog.VIDEO_MODE)
    assert len([c for c in proxy.calls if c["path"].startswith("/v1/models")]) == 2


def test_input_types_model_widget_carries_the_filtered_options_as_default(
        proxy, monkeypatch, live_discovery):
    """The dropdown's DEFAULT must be a real registered model: a ComfyUI combo
    whose default is not in its option list renders as an empty selector."""
    monkeypatch.delenv("SPARKRUN_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(nodes, "DEFAULT_BASE_URL", proxy.base_url)
    spec = nodes.SparkrunImage.INPUT_TYPES()
    options, opts = spec["required"]["model"]
    assert options == ["sd-diffusion-t2i", "sd-diffusion-alias"]
    assert opts["default"] == "sd-diffusion-t2i"
    vspec = nodes.SparkrunVideo.INPUT_TYPES()
    assert vspec["required"]["model"][0] == ["wan-diffusion-t2v"]


def test_default_base_url_used_by_input_types_is_the_placeholder(monkeypatch):
    """With no proxy configured the dropdown still has a valid (sentinel) value,
    so /object_info cannot be broken by a dead proxy — and the placeholder host
    is never actually contacted (the loopback guard would fail this test)."""
    monkeypatch.delenv("SPARKRUN_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(nodes, "DEFAULT_BASE_URL", "http://10.0.0.x:4000")
    spec = nodes.SparkrunImage.INPUT_TYPES()
    assert spec["required"]["model"][0] == [catalog.NO_MODELS_SENTINEL]


def test_the_offline_guard_blocks_any_non_loopback_target():
    """The suite's "no network" property is a mechanism, not a claim: a request
    aimed at anything but loopback is refused by the autouse guard, so a fixture
    typo pointing at the operator's real proxy fails loudly instead of quietly
    passing against live infrastructure."""
    http = load_pack("http_client")
    with pytest.raises(AssertionError) as ei:
        http.get_json("http://10.0.0.x:4000/v1/models", timeout=1)
    assert "NON-LOCAL" in str(ei.value)


def test_discovery_disable_env_var_short_circuits(proxy, monkeypatch):
    monkeypatch.setenv("SPARKRUN_DISABLE_MODEL_DISCOVERY", "1")
    assert nodes._model_options(proxy.base_url, catalog.IMAGE_MODE) == \
        [catalog.NO_MODELS_SENTINEL]
    assert proxy.calls == [], "must not even attempt the request"
