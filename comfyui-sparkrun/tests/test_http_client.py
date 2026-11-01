"""http_client tests: URL joining, error contract, multipart, caps — offline."""

from __future__ import annotations

import json

import pytest
from pack_loader import load_pack

http = load_pack("http_client")


# ── join_url / resolve_ref: the base_url shapes an operator will actually type ─

@pytest.mark.parametrize("base,path,want", [
    ("http://h:4000", "/models", "http://h:4000/v1/models"),
    ("http://h:4000/", "/models", "http://h:4000/v1/models"),
    ("http://h:4000/v1", "/models", "http://h:4000/v1/models"),
    ("http://h:4000/v1/", "/models", "http://h:4000/v1/models"),
    ("http://h:4000", "models", "http://h:4000/models"),  # explicit relative
    ("http://h:4000/base", "/videos", "http://h:4000/base/v1/videos"),
    ("http://h:4000/v1", "/videos/abc/content", "http://h:4000/v1/videos/abc/content"),
])
def test_join_url_handles_every_base_shape(base, path, want):
    assert http.join_url(base, path) == want


def test_join_url_absolute_override():
    assert http.join_url("http://h:4000", "/x",
                         absolute="https://cdn/x.mp4") == "https://cdn/x.mp4"


@pytest.mark.parametrize("base,ref,want", [
    ("http://h:4000/v1", "https://cdn.signed/x?sig=1", "https://cdn.signed/x?sig=1"),
    ("http://h:4000/v1", "/v1/videos/a/content", "http://h:4000/v1/videos/a/content"),
    ("http://h:4000", "/v1/videos/a/content", "http://h:4000/v1/videos/a/content"),
    ("http://h:4000/v1", "out.mp4", "http://h:4000/v1/out.mp4"),
])
def test_resolve_ref_absolute_relative_and_bare(base, ref, want):
    assert http.resolve_ref(base, ref) == want


def test_resolve_ref_rejects_garbage():
    with pytest.raises(http.SparkrunProxyError):
        http.resolve_ref("not-a-url", "/v1/x")
    with pytest.raises(http.SparkrunProxyError):
        http.resolve_ref("http://h:4000", "   ")


# ── error contract against the fake proxy ────────────────────────────────────

def test_error_carries_status_and_body_snippet(proxy):
    proxy.fail_with("/v1/images/generations", 400)
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.post_json(http.join_url(proxy.base_url, "/images/generations"),
                       {"model": "x", "prompt": "y"})
    assert ei.value.status == 400
    assert "proxy refused" in ei.value.body or "refused" in str(ei.value)


def test_error_body_is_truncated():
    long = "x" * 5000
    snip = http._snip(long.encode())
    assert len(snip) <= http.MAX_ERROR_BODY_CHARS + 1  # +1 for the ellipsis


def test_unreachable_proxy_raises_with_status_none(proxy):
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.get_json("http://127.0.0.1:1/v1/models", timeout=2)
    assert ei.value.status is None
    assert "unreachable" in str(ei.value)


def test_non_json_2xx_is_reported_as_a_proxy_fault(proxy):
    """A 200 whose body is not JSON is a proxy fault too — surface it as one
    instead of letting a JSONDecodeError reach the UI."""
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.get_json(proxy.base_url + "/raw")
    assert ei.value.status == 200
    assert "not JSON" in str(ei.value)


def test_json_roundtrip_and_headers(proxy):
    body = http.get_json(http.join_url(proxy.base_url, "/models"))
    assert body["object"] == "list"
    call = proxy.last_call("GET", "/v1/models")
    assert call["headers"]["accept"] == "application/json"


def test_post_json_sets_content_type_and_serialises(proxy):
    out = http.post_json(http.join_url(proxy.base_url, "/images/generations"),
                         {"model": "m", "prompt": "p", "seed": 3})
    assert out["data"][0]["b64_json"]
    call = proxy.last_call("POST", "/v1/images/generations")
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"].decode()) == {"model": "m", "prompt": "p", "seed": 3}


# ── multipart: the shape the edits endpoint needs ────────────────────────────

def test_multipart_body_has_fields_files_and_boundary(proxy):
    fields = {"model": "m", "prompt": "paint it", "seed": "7"}
    files = {"image": ("input.png", b"\x89PNGfake", "image/png"),
             "mask": ("mask.png", b"\x89PNGmask", "image/png")}
    out = http.post_multipart(http.join_url(proxy.base_url, "/images/edits"),
                              fields, files)
    assert out["data"][0]["b64_json"]
    call = proxy.last_call("POST", "/v1/images/edits")
    ctype = call["headers"]["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1]
    text = call["body"].decode("latin-1")
    for frag in ('name="model"', 'name="prompt"', 'name="seed"',
                 'name="image"; filename="input.png"',
                 'name="mask"; filename="mask.png"',
                 "Content-Type: image/png"):
        assert frag in text, f"missing {frag} in multipart body"
    assert text.count(f"--{boundary}") == len(fields) + len(files) + 1
    assert text.endswith(f"--{boundary}--\r\n")


def test_multipart_skips_none_fields(proxy):
    http.post_multipart(http.join_url(proxy.base_url, "/images/edits"),
                        {"model": "m", "size": None},
                        {"image": ("i.png", b"x", "image/png")})
    text = proxy.last_call("POST", "/v1/images/edits")["body"].decode("latin-1")
    assert 'name="size"' not in text


def test_multipart_guesses_mime_from_filename(proxy):
    http.post_multipart(http.join_url(proxy.base_url, "/images/edits"), {},
                        {"image": ("i.png", b"x", None)})
    text = proxy.last_call("POST", "/v1/images/edits")["body"].decode("latin-1")
    assert "Content-Type: image/png" in text


# ── b64 + download caps ──────────────────────────────────────────────────────

def test_b64_to_bytes_tolerates_embedded_newlines():
    import base64
    raw = base64.b64encode(b"hello world").decode()
    spaced = raw[:4] + "\n" + raw[4:] + "\n"
    assert http.b64_to_bytes(spaced) == b"hello world"


def test_b64_to_bytes_rejects_garbage():
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.b64_to_bytes("not base64 @@@")
    assert "undecodable" in str(ei.value)


def test_download_returns_container_bytes(proxy):
    blob = http.download(proxy.base_url + "/v1/assets/out.mp4")
    assert blob.startswith(b"\x00\x00\x00\x18ftyp")


def test_download_cap_refuses_an_oversized_declared_body(proxy, monkeypatch):
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.download(proxy.base_url + "/v1/assets/out.mp4", max_bytes=8)
    assert "exceeds cap" in str(ei.value)


def test_timeout_raises_the_same_error_type(proxy, monkeypatch):
    # A timeout is a proxy fault, not a socket detail leaking to the UI.
    real = http.urllib.request.urlopen

    def slow(*a, **kw):
        raise TimeoutError("too slow")

    monkeypatch.setattr(http.urllib.request, "urlopen", slow)
    with pytest.raises(http.SparkrunProxyError) as ei:
        http.get_json("http://127.0.0.1:9/v1/models", timeout=0.01)
    assert "timed out" in str(ei.value)
    assert ei.value.status is None
    monkeypatch.setattr(http.urllib.request, "urlopen", real)
