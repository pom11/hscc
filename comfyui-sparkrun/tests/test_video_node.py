"""SparkrunVideo: submit -> poll -> download, against the fake job lifecycle."""

from __future__ import annotations

import pytest
from pack_loader import load_pack

nodes = load_pack("nodes")
from conftest import CONTAINER_BYTES  # noqa: E402


def make_video(node=None, **over):
    kw = dict(base_url=over.pop("base_url", None) or "", model="wan-diffusion-t2v",
              prompt="a cat walking", size="1024x1024", steps=20, seed=5,
              fps=24, duration=5.0, poll_interval=0.01, poll_timeout=5.0)
    kw.update(over)
    n = node or nodes.SparkrunVideo()
    return n, n.generate(**kw)


@pytest.fixture
def base(proxy):
    return proxy.base_url


def test_submit_payload_matches_the_video_surface(proxy, base):
    node, (video, meta) = make_video(base_url=base)
    call = proxy.last_call("POST", "/v1/videos")
    assert call is not None, "video node must POST /v1/videos"
    sent = proxy.json_body(call)
    assert sent["model"] == "wan-diffusion-t2v"
    assert sent["prompt"] == "a cat walking"
    assert sent["size"] == "1024x1024"
    assert sent["steps"] == 20
    assert sent["seed"] == 5
    assert sent["fps"] == 24
    assert sent["seconds"] == 5.0  # duration is OpenAI's `seconds`
    assert meta["endpoint"] == "/v1/videos"


def test_job_is_polled_until_completed(proxy, base):
    node, (video, meta) = make_video(base_url=base)
    polls = [c for c in proxy.calls
             if c["method"] == "GET" and c["path"] == "/v1/videos/vid_job_42"]
    assert len(polls) == 3, "queued -> processing -> completed must take 3 polls"
    assert meta["polls"] == 3
    assert meta["status"] == "completed"
    assert meta["job_id"] == "vid_job_42"


def test_content_is_downloaded_after_completion(proxy, base):
    _, (video, meta) = make_video(base_url=base)
    content = [c for c in proxy.calls if c["path"] == "/v1/videos/vid_job_42/content"]
    assert len(content) == 1, "the container is fetched exactly once, after completion"
    assert content[0]["method"] == "GET"
    assert meta["bytes"] == len(CONTAINER_BYTES)


def test_video_output_is_a_comfy_video_object(proxy, base, comfy_api_stand_in):
    """With comfy_api present, output must be the VIDEO runtime type wrapping the
    downloaded bytes — not raw bytes and not a re-encoded copy."""
    from conftest import FakeVideoFromFile
    _, (video, _) = make_video(base_url=base)
    assert isinstance(video, FakeVideoFromFile)
    assert len(FakeVideoFromFile.instances) == 1
    assert video.payload == CONTAINER_BYTES


def test_video_output_falls_back_to_bytes_without_comfy_api(proxy, base):
    _, (video, _) = make_video(base_url=base)
    assert video == CONTAINER_BYTES


def test_absent_content_path_is_used_when_job_names_no_url(proxy, base):
    proxy.state["content_style"] = "missing"
    _, (_, meta) = make_video(base_url=base)
    assert meta["content_url"].endswith("/v1/videos/vid_job_42/content")
    assert any(c["path"] == "/v1/videos/vid_job_42/content" for c in proxy.calls)


def test_absolute_output_url_is_fetched_verbatim(proxy, base):
    proxy.state["content_style"] = "absolute_url"
    _, (_, meta) = make_video(base_url=base)
    assert meta["content_url"] == f"{base}/v1/assets/out.mp4"
    assert any(c["path"] == "/v1/assets/out.mp4" for c in proxy.calls)


def test_signed_url_failure_falls_through_to_the_content_path(proxy, base, monkeypatch):
    """A job that hands back a signed CDN url which has already expired must not
    kill the node — the conventional content endpoint is tried next.

    ``nodes._http`` IS the module ``load_pack("http_client")`` returns (the pack
    loads siblings under that shared name), so patching it once is enough — a
    second patch of the same object would only imply two copies exist.
    """
    proxy.state["content_style"] = "absolute_url"
    http = load_pack("http_client")
    assert nodes._http is http, "the node and the test must hold one http module"
    real = http.download

    def flaky(url, **kw):
        if "out.mp4" in url:
            raise http.SparkrunProxyError("403 expired", status=403)
        return real(url, **kw)

    monkeypatch.setattr(http, "download", flaky)
    _, (_, meta) = make_video(base_url=base)
    assert meta["bytes"] == len(CONTAINER_BYTES)
    assert meta["content_url"].endswith("/content")


def test_submit_without_job_id_is_a_clear_error(proxy, base, monkeypatch):
    http = load_pack("http_client")
    real = http.post_json
    monkeypatch.setattr(http, "post_json", lambda *a, **kw: {"status": "queued"})
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base)
    assert "no job id" in str(ei.value)
    monkeypatch.setattr(http, "post_json", real)


def test_failed_job_fails_the_node_with_the_proxy_status(proxy, base):
    proxy.queue_video_statuses(["queued", "failed"])
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base)
    assert "failed" in str(ei.value)
    assert not any(c["path"].endswith("/content") for c in proxy.calls), \
        "a failed job must not be downloaded"


def test_timeout_fails_the_node_naming_the_last_status_and_poll_count(proxy, base):
    proxy.queue_video_statuses(["queued", "processing"])  # never completes
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base, poll_timeout=0.05, poll_interval=0.01)
    msg = str(ei.value)
    assert "still processing" in msg and "polls" in msg


def test_unknown_intermediate_status_keeps_polling(proxy, base):
    """An unfamiliar in-flight spelling must behave as in-flight, not as a
    failure — a proxy version that adds a new status word must not break video
    nodes, it must just keep waiting."""
    proxy.queue_video_statuses(["in_progress", "starting_up", "completed"])
    _, (_, meta) = make_video(base_url=base)
    assert meta["status"] == "completed"


def test_nested_job_id_shape_is_accepted(proxy, base):
    proxy.state["submit_style"] = "nested"
    _, (_, meta) = make_video(base_url=base)
    assert meta["job_id"] == "vid_job_42"


def test_poll_interval_and_timeout_are_honoured(proxy, base):
    """The node must sleep between polls (a busy-loop would hammer the proxy)
    and must stop at its deadline."""
    import time

    proxy.queue_video_statuses(["queued", "queued", "queued", "completed"])
    t0 = time.monotonic()
    _, (_, meta) = make_video(base_url=base, poll_interval=0.06, poll_timeout=5.0)
    assert time.monotonic() - t0 >= 0.12, "3 gaps of ~0.06s between 4 polls"
    assert meta["polls"] == 4


def test_video_node_refuses_image_models(proxy, base, live_discovery):
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base, model="sd-diffusion-t2i")
    assert "video_generation" in str(ei.value)
    assert proxy.last_call("POST", "/v1/videos") is None


def test_video_node_refuses_the_sentinel(proxy, base):
    catalog = load_pack("catalog")
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base, model=catalog.NO_MODELS_SENTINEL)
    assert "video_generation" in str(ei.value)


def test_submit_500_surfaces(proxy, base):
    proxy.fail_with("/v1/videos", 503)
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_video(base_url=base)
    assert ei.value.status == 503


# ── pure helper tests for the response-shape parsing ─────────────────────────

@pytest.mark.parametrize("body,want", [
    ({"id": "a"}, "a"),
    ({"job_id": "b"}, "b"),
    ({"data": {"job_id": "c"}}, "c"),
    ({"output": {"id": "d"}}, "d"),
    ({"status": "queued"}, None),
    ("junk", None),
    ({}, None),
])
def test_extract_job_id_shapes(body, want):
    assert nodes._extract_job_id(body) == want


@pytest.mark.parametrize("body,want", [
    ({"data": [{"b64_json": "x"}]}, 1),
    ({"data": []}, 0),
    ({"result": {"b64_json": "x"}}, 1),
    ([{"b64_json": "x"}, {"b64_json": "y"}], 2),
    ({"data": "not-a-list"}, 0),
    (None, 0),
])
def test_extract_image_items_shapes(body, want):
    assert len(nodes._extract_image_items(body)) == want


def test_image_item_without_payload_is_reported():
    http = load_pack("http_client")
    with pytest.raises(http.SparkrunProxyError) as ei:
        nodes._image_item_to_bytes({"index": 0}, "http://127.0.0.1:4000")
    assert "neither b64_json nor url" in str(ei.value)
