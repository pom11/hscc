"""Comfy tensor <-> media bytes, with the ``comfy``/``torch`` import guarded.

ComfyUI's wire types used here:

* ``IMAGE`` — ``torch.Tensor`` of shape ``[B, H, W, C]``, float32, 0..1, CPU.
* ``VIDEO`` — the ``VIDEO`` io type, whose runtime value in 0.43.x is a
  ``comfy_api`` ``VideoInput`` (``VideoFromFile`` accepts a path *or* a
  ``BytesIO``, which is exactly what the downloaded job content is).

Every heavy import (``numpy``, ``torch``, ``PIL``, ``comfy_api``) happens
INSIDE the functions that need it, so this module imports cleanly in the repo
test venv — which has numpy and Pillow but no torch and no ComfyUI at all, and
which collects the pack's suite through ``scripts/run_tests.sh``. The
conversions therefore raise :class:`MediaRuntimeMissing` there instead of
ImportError at collection time, and the tests assert on that explicit error
rather than pretending the conversion ran.
"""

from __future__ import annotations

import io as _io


class MediaRuntimeMissing(RuntimeError):
    """A conversion needs a runtime that only exists inside ComfyUI.

    Raised (never ImportError) so a node failure in ComfyUI is legible and the
    repo suite can pin the guard: the pack must be importable where torch and
    comfy do not exist.
    """


def _require_torch():
    try:
        import torch  # type: ignore[import-not-found]  # provided by ComfyUI's env, not the repo suite's
    except ImportError as e:  # pragma: no cover - depends on the host venv
        raise MediaRuntimeMissing(
            "this conversion needs torch (available inside ComfyUI's python, "
            f"not in this interpreter): {e}"
        ) from e
    return torch


def image_tensor_to_png(frame) -> bytes:
    """One ``[H, W, C]`` frame (torch tensor, or a plain ndarray) → PNG bytes.

    Duck-typed rather than torch-typed: the encoding path needs only
    numpy + Pillow, so it works with whatever the caller's tensor hands back
    from ``.numpy()`` and never imports torch itself. (Decoding *must* produce a
    torch tensor, so only :func:`png_bytes_to_image_tensor` requires torch.)
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise MediaRuntimeMissing(f"PNG encoding needs numpy+Pillow: {e}") from e

    t = frame
    if hasattr(t, "detach"):
        t = t.detach()
    if hasattr(t, "cpu"):
        t = t.cpu()
    if hasattr(t, "float"):
        t = t.float()
    arr = t.numpy() if hasattr(t, "numpy") else np.asarray(t)
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4):
        # Channel-first [C, H, W] — accept it rather than emitting a garbled PNG.
        if arr.ndim == 3 and arr.shape[0] in (1, 3, 4):
            arr = np.transpose(arr, (1, 2, 0))
        else:
            raise ValueError(f"cannot read an image frame of shape {arr.shape}")
    if arr.ndim == 2:
        arr = arr[..., None]
    arr = arr[..., :4] if arr.shape[-1] == 4 else arr[..., :3]
    if float(arr.min()) >= 0.0 and float(arr.max()) <= 1.0:
        arr = arr * 255.0
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.shape[-1] == 1:
        arr = arr[..., 0]
    img = Image.fromarray(arr)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_batch_to_first_png(images) -> bytes:
    """First frame of a ComfyUI ``IMAGE`` batch (``[B,H,W,C]``) as PNG bytes."""
    batch = images
    try:
        n = len(batch)
    except TypeError:
        n = 1
    frame = batch[0] if n else batch
    return image_tensor_to_png(frame)


def mask_batch_to_l_png(mask) -> bytes:
    """First frame of a ComfyUI ``MASK`` batch (``[B,H,W]`` float 0..1) as a
    grayscale PNG — the shape OpenAI ``/v1/images/edits`` expects for its
    separate ``mask`` multipart part.
    """
    _require_torch()
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise MediaRuntimeMissing(f"mask encoding needs numpy+Pillow: {e}") from e

    m = mask
    if hasattr(m, "detach"):
        m = m.detach()
    if hasattr(m, "cpu"):
        m = m.cpu()
    if hasattr(m, "float"):
        m = m.float()
    if hasattr(m, "ndim") and m.ndim == 3:
        m = m[0]
    m = m.numpy() if hasattr(m, "numpy") else np.asarray(m, dtype="float32")
    arr = np.clip(m, 0.0, 1.0)
    if arr.ndim == 3 and arr.shape[-1] == 1:
        arr = arr[..., 0]
    img = Image.fromarray((arr * 255.0).astype(np.uint8), mode="L")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def png_bytes_to_image_tensor(blob: bytes):
    """PNG bytes → ``(torch.Tensor[1, H, W, C], {"subfolder": "", "type": "output"})``.

    The metadata dict is ComfyUI's SaveImage convention (an empty subfolder
    means "write to the output root"); returning it keeps the node's second
    output useful downstream.
    """
    torch = _require_torch()
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise MediaRuntimeMissing(f"PNG decoding needs numpy+Pillow: {e}") from e

    img = Image.open(_io.BytesIO(blob))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    arr = np.asarray(img, dtype="float32") / 255.0
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    t = torch.from_numpy(np.ascontiguousarray(arr)).float()
    return t.unsqueeze(0), {"subfolder": "", "type": "output"}


def video_bytes_to_output(blob: bytes):
    """Video container bytes → the ComfyUI ``VIDEO`` runtime value.

    ``VideoFromFile`` accepts a ``BytesIO``, which avoids touching the
    filesystem. Falls back to the raw bytes when the pack runs outside a
    ComfyUI build that ships ``comfy_api`` (a downstream node that only wants
    the container still gets it, and the failure mode is a type mismatch, not
    a crash).
    """
    try:
        from comfy_api.latest import InputImpl  # type: ignore
    except Exception:
        try:
            from comfy_api.input import VideoFromFile  # type: ignore
            return VideoFromFile(_io.BytesIO(blob))
        except Exception:
            return blob
    return InputImpl.VideoFromFile(_io.BytesIO(blob))
