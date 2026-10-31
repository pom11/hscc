"""SparkrunImage against the fake proxy: POST /v1/images/generations.

Driven through the node's own ``generate`` method — not through a helper — so
the request the operator's ComfyUI will actually send is what is asserted.
"""

from __future__ import annotations

import base64

import pytest
from pack_loader import load_pack

from conftest import PNG_B64, PNG_BYTES  # noqa: E402

nodes = load_pack("nodes")


def make_image(proxy, **over):
    kw = dict(base_url=proxy.base_url, model="sd-diffusion-t2i",
              prompt="a red square", size="1024x1024", steps=20, seed=7)
    kw.update(over)
    return nodes.SparkrunImage().generate(**kw)


def test_posts_the_documented_payload(proxy):
    tensor, meta = make_image(proxy)
    call = proxy.last_call("POST", "/v1/images/generations")
    assert call is not None, "image node must hit /v1/images/generations"
    sent = proxy.json_body(call)
    assert sent["model"] == "sd-diffusion-t2i"
    assert sent["prompt"] == "a red square"
    assert sent["size"] == "1024x1024"
    assert sent["steps"] == 20
    assert sent["seed"] == 7
    assert sent["n"] == 1
    # b64_json is asked for explicitly: a url response depends on the backend
    # serving the file again, which the proxy does not always do.
    assert sent["response_format"] == "b64_json"
    assert call["headers"]["content-type"] == "application/json"
    assert meta["endpoint"] == "/v1/images/generations"
    assert meta["model"] == "sd-diffusion-t2i"


def test_b64_response_decodes_to_an_image_tensor(proxy):
    tensor, meta = make_image(proxy)
    assert tensor.shape == (1, 6, 8, 3)  # [B,H,W,C] — ComfyUI's IMAGE layout
    arr = tensor.numpy()
    assert arr.min() >= 0.0 and arr.max() <= 1.0  # ComfyUI expects 0..1 float
    # The fake proxy returns a solid red 8x6 PNG; a decode that swapped channels
    # or forgot the /255 would not reproduce red.
    assert arr[0, 0, 0, 0] > 0.9 and arr[0, 0, 0, 1] < 0.1
    assert meta["subfolder"] == "" and meta["type"] == "output"


def test_revised_prompt_and_seed_are_surfaced_in_meta(proxy):
    _, meta = make_image(proxy, seed=0)
    assert meta["revised_prompt"] == "revised"
    assert meta["seed"] == 0  # 0 is a seed, not "absent"


def test_negative_prompt_only_travels_when_set(proxy):
    make_image(proxy)
    assert "negative_prompt" not in proxy.json_body(proxy.last_call("POST"))
    make_image(proxy, negative_prompt="blurry")
    assert proxy.json_body(proxy.last_call("POST"))["negative_prompt"] == "blurry"


def test_size_is_forwarded_verbatim(proxy):
    make_image(proxy, size="1536x1024")
    assert proxy.json_body(proxy.last_call("POST"))["size"] == "1536x1024"


def test_url_response_is_downloaded_and_resolved(proxy):
    """A backend that answers with a url instead of b64 must still work, and a
    root-relative url must resolve against the host root, not under /v1 twice."""
    proxy.state["image_style"] = "url"
    tensor, _ = make_image(proxy)
    assert tensor.shape[0] == 1
    got = [c for c in proxy.calls if c["method"] == "GET" and "/assets/" in c["path"]]
    assert len(got) == 1, "the url must be fetched exactly once"


def test_empty_data_list_is_a_clear_error(proxy):
    proxy.state["image_style"] = "empty"
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_image(proxy)
    assert "no image data" in str(ei.value)


def test_proxy_400_surfaces_the_proxy_message(proxy):
    proxy.fail_with("/v1/images/generations", 400)
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_image(proxy)
    assert ei.value.status == 400
    assert "refused" in str(ei.value)


# ── the mode pre-flight (M3 contract enforcement at the node) ────────────────

def test_model_registered_for_the_wrong_mode_is_refused_before_the_post(proxy):
    """Chat/untagged models must never be POSTed to an image endpoint: the proxy
    would reject them, and the error would arrive as an opaque 400."""
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_image(proxy, model="cluster-chat")
    assert "not registered as" in str(ei.value)
    assert "image_generation" in str(ei.value)
    assert proxy.last_call("POST", "/v1/images/generations") is None, \
        "the node must not send the doomed request"


def test_video_model_on_the_image_node_is_refused(proxy):
    with pytest.raises(nodes.SparkrunProxyError):
        make_image(proxy, model="wan-diffusion-t2v")


def test_unregistered_model_is_refused_with_the_registered_list(proxy):
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_image(proxy, model="does-not-exist")
    msg = str(ei.value)
    assert "sd-diffusion-t2i" in msg, "the message should name what IS available"


def test_sentinel_selection_names_the_cause(proxy):
    catalog = load_pack("catalog")
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_image(proxy, model=catalog.NO_MODELS_SENTINEL)
    msg = str(ei.value)
    assert "image_generation" in msg and "hscc" in msg


def test_blank_model_is_refused(proxy):
    with pytest.raises(nodes.SparkrunProxyError):
        make_image(proxy, model="   ")


def test_guard_steps_aside_when_discovery_itself_failed(proxy):
    """If /v1/models cannot be read, the pre-flight must not invent a refusal —
    the real request reports the outage instead (fail-open on the guard,
    fail-closed on the request)."""
    proxy.state["models_status"] = 500
    tensor, _ = make_image(proxy, model="anything-at-all")
    assert tensor.shape[0] == 1
    assert proxy.last_call("POST", "/v1/images/generations") is not None


def test_last_payload_is_what_was_sent(proxy):
    node = nodes.SparkrunImage()
    node.generate(base_url=proxy.base_url, model="sd-diffusion-t2i",
                  prompt="p", size="512x512", steps=4, seed=1)
    call = proxy.last_call("POST", "/v1/images/generations")
    assert node.last_payload["size"] == "512x512"
    assert node.last_payload == proxy.json_body(call)
