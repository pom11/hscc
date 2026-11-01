"""SparkrunImageEdit: multipart POST /v1/images/edits, IMAGE + MASK encoding."""

from __future__ import annotations

import io

import numpy as np
import pytest
from pack_loader import load_pack

from conftest import PNG_BYTES  # noqa: E402

from fake_proxy import FakeTensor as FakeImageTensor  # the doubles the ComfyUI
from fake_proxy import mask_batch, rgb_batch          # probe shares, not copies

nodes = load_pack("nodes")
media = load_pack("media")


def make_edit(proxy, **over):
    kw = dict(base_url=proxy.base_url, model="sd-diffusion-t2i",
              image=rgb_batch(), prompt="add a hat", seed=3, size="1024x1024")
    kw.update(over)
    return nodes.SparkrunImageEdit().edit(**kw)


def test_posts_multipart_with_image_part(proxy):
    tensor, meta = make_edit(proxy)
    call = proxy.last_call("POST", "/v1/images/edits")
    assert call is not None, "edit node must hit /v1/images/edits"
    assert call["headers"]["content-type"].startswith("multipart/form-data")
    text = call["body"].decode("latin-1")
    assert 'name="image"; filename="input.png"' in text
    assert "Content-Type: image/png" in text
    assert meta["endpoint"] == "/v1/images/edits"
    assert meta["masked"] is False


def test_form_fields_carry_model_prompt_size_seed(proxy):
    make_edit(proxy, size="512x512", seed=11)
    text = proxy.last_call("POST", "/v1/images/edits")["body"].decode("latin-1")
    for frag in ('name="model"', 'name="prompt"', 'name="size"', 'name="seed"',
                 "add a hat", "512x512"):
        assert frag in text


def test_image_part_is_a_real_png_of_the_input_frame(proxy):
    """Round-trip the actual multipart bytes: the PNG the node encoded must
    decode back to the input colour and size. Encoding silently producing a
    blank or transposed image is the failure this catches."""
    make_edit(proxy)
    body = proxy.last_call("POST", "/v1/images/edits")["body"]
    from PIL import Image
    start = body.index(b"\x89PNG")
    img = Image.open(io.BytesIO(body[start:start + 4096]))
    img.load()
    assert img.size == (8, 6)  # W,H — matches rgb_batch(w=8, h=6)
    assert img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_mask_input_becomes_a_grayscale_mask_part(proxy):
    make_edit(proxy, mask=mask_batch())
    call = proxy.last_call("POST", "/v1/images/edits")
    text = call["body"].decode("latin-1")
    assert 'name="mask"; filename="mask.png"' in text
    _, meta = make_edit(proxy, mask=mask_batch())
    assert meta["masked"] is True


def test_mask_png_encodes_the_mask_values(proxy):
    """The mask part must carry the mask's own values (0.5 -> mid grey), not an
    all-white image — an inverted or clipped mask edits the wrong region."""
    make_edit(proxy, mask=mask_batch(value=0.5))
    body = proxy.last_call("POST", "/v1/images/edits")["body"]
    # two PNG blobs in the body: the image, then the mask
    first = body.index(b"\x89PNG")
    second = body.index(b"\x89PNG", first + 1)
    from PIL import Image
    img = Image.open(io.BytesIO(body[second:second + 4096]))
    assert img.mode == "L"
    assert img.size == (8, 6)
    px = int(np.asarray(img)[0, 0])
    assert 120 <= px <= 135  # 0.5 * 255 = 127


def test_batch_input_uses_the_first_frame(proxy):
    make_edit(proxy, image=rgb_batch(n=3, color=(0.0, 1.0, 0.0)))
    body = proxy.last_call("POST", "/v1/images/edits")["body"]
    from PIL import Image
    start = body.index(b"\x89PNG")
    img = Image.open(io.BytesIO(body[start:start + 4096]))
    assert img.convert("RGB").getpixel((0, 0)) == (0, 255, 0)


def test_response_decodes_to_an_image_tensor(proxy):
    tensor, meta = make_edit(proxy)
    assert tensor.shape == (1, 6, 8, 3)
    assert tensor.numpy().max() <= 1.0


def test_edited_image_mode_guard_applies(proxy, live_discovery):
    with pytest.raises(nodes.SparkrunProxyError):
        make_edit(proxy, model="wan-diffusion-t2v")
    assert proxy.last_call("POST", "/v1/images/edits") is None


def test_proxy_failure_surfaces(proxy):
    proxy.fail_with("/v1/images/edits", 413)
    with pytest.raises(nodes.SparkrunProxyError) as ei:
        make_edit(proxy)
    assert ei.value.status == 413


# ── media.py conversions, exercised directly (offline, no torch needed) ──────

def test_image_batch_to_first_png_accepts_channel_first(proxy):
    """A [C,H,W] frame must not be emitted as a garbled PNG."""
    arr = np.zeros((3, 6, 8), dtype="float32")
    arr[1] = 1.0  # green channel first-plane
    png = media.image_tensor_to_png(FakeImageTensor(arr))
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    assert img.size == (8, 6)
    assert img.convert("RGB").getpixel((0, 0)) == (0, 255, 0)


def test_image_conversion_accepts_0_255_input(proxy):
    arr = (np.ones((6, 8, 3), dtype="float32") * 255.0)
    png = media.image_tensor_to_png(FakeImageTensor(arr))
    from PIL import Image
    assert Image.open(io.BytesIO(png)).convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_grayscale_input_expands_to_three_channels(proxy):
    arr = np.ones((6, 8), dtype="float32")
    png = media.image_tensor_to_png(FakeImageTensor(arr))
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    assert img.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_unusable_frame_shape_is_rejected_not_garbled():
    with pytest.raises(ValueError):
        media.image_tensor_to_png(FakeImageTensor(np.zeros((5, 7, 9), dtype="float32")))


def test_size_widget_value_reaches_the_proxy_verbatim(proxy):
    """The pack never rewrites the size — it forwards the widget's string so the
    proxy/backend stays the single parser. A silent normalisation here would
    make the node and the proxy disagree about what was asked for."""
    make_edit(proxy, size="768x1152")
    body = proxy.last_call("POST", "/v1/images/edits")["body"].decode("latin-1")
    assert 'name="size"\r\n\r\n768x1152' in body
