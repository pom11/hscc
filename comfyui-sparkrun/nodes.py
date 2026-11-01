"""comfyui-sparkrun — ComfyUI nodes that call the sparkrun proxy (:4000).

Three nodes, one backend (the operator's LiteLLM proxy, which only serves
models registered through hscc — see ``hscc-cluster/media_registration.py``):

* ``SparkrunImage``     POST /v1/images/generations
* ``SparkrunImageEdit`` POST /v1/images/edits   (multipart)
* ``SparkrunVideo``     POST /v1/videos + job polling + content download

Each model dropdown is populated from ``GET /v1/models`` filtered by
``model_info.mode`` (``image_generation`` for the image nodes,
``video_generation`` for the video node) — see ``catalog.py``.

Two structural rules this file obeys, both of which the repo suite pins:

1. NO import of ``comfy``, ``torch``, ``numpy`` or ``PIL`` at module level or
   in ``INPUT_TYPES``. The repo's own test venv has neither torch nor ComfyUI,
   and ``scripts/run_tests.sh`` collects this pack — so an unguarded heavy
   import would break the repo suite with the pack's tests never running. All
   heavy use goes through ``media.py``, whose conversions raise
   :class:`MediaRuntimeMissing` when the runtime is absent. The AST scan in
   ``tests/test_manifest.py`` enforces the rule; it does not rely on it.

2. Sibling modules load by FILE PATH under namespaced names, not by plain
   ``import``. ComfyUI's custom-node loader (``nodes.py:load_custom_node`` in
   0.43.2) execs a pack's ``__init__.py`` via ``spec_from_file_location`` and
   does NOT put the pack dir on ``sys.path``, so ``import catalog`` would fail
   in ComfyUI while passing in pytest. And the pack must not insert its own
   dir either: this file is ``nodes.py``, and ComfyUI's own ``nodes`` module is
   already importable — shadowing it would break every other installed pack.
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


_http = _load_sibling("http_client")
_catalog = _load_sibling("catalog")
_media = _load_sibling("media")

SparkrunProxyError = _http.SparkrunProxyError
MediaRuntimeMissing = _media.MediaRuntimeMissing

#: Default proxy root. ``10.0.0.x`` is the repo's documented LAN placeholder
#: (address_guard allows it, a real host does not) — set
#: ``SPARKRUN_PROXY_BASE_URL`` to the operator's real proxy and the widget
#: default becomes that value, so no real address is ever committed here.
DEFAULT_BASE_URL = os.environ.get("SPARKRUN_PROXY_BASE_URL", "http://10.0.0.x:4000")

#: Size options offered by the widgets. The string is forwarded verbatim to
#: the backend, so this list is convenience, not a whitelist.
SIZE_CHOICES = ["1024x1024", "1024x1536", "1536x1024", "832x1216", "1216x832", "512x512"]

#: (base_url, mode) -> (monotonic_ts, options). ComfyUI re-requests INPUT_TYPES
#: on every /object_info refresh; a slow or dead proxy must not stall the UI.
_DISCOVERY_CACHE = {}
_DISCOVERY_TTL_S = float(os.environ.get("SPARKRUN_MODEL_TTL_SECONDS", "30"))


def _fetch_media_names(base_url: str, mode: str):
    """Model names on ``base_url`` carrying ``mode`` — or ``None`` if unknown.

    ``None`` means discovery itself failed (unreachable, 5xx, non-JSON); the
    caller must NOT read that as "no models". Successful answers are cached
    for ``_DISCOVERY_TTL_S`` so a node run costs one ``/v1/models`` request
    total, not one per dropdown plus one per pre-flight. Failures are NOT
    cached: a proxy that comes back up should be seen immediately, and caching
    the failure is how a transient blip turns into a node that looks broken.
    """
    import time

    if os.environ.get("SPARKRUN_DISABLE_MODEL_DISCOVERY"):
        return None
    key = (str(base_url), mode)
    now = time.monotonic()
    hit = _DISCOVERY_CACHE.get(key)
    if hit and now - hit[0] < _DISCOVERY_TTL_S:
        return list(hit[1])
    try:
        body = _http.get_json(_http.join_url(base_url, "/models"),
                              timeout=_http.DISCOVERY_TIMEOUT_S)
        names = _catalog.models_for_mode(body, mode)
    except Exception:
        return None
    _DISCOVERY_CACHE[key] = (now, list(names))
    return names


def _model_options(base_url: str, mode: str):
    """Combo options for a LiteLLM ``mode`` at ``base_url`` — never raises.

    ``SPARKRUN_DISABLE_MODEL_DISCOVERY=1`` skips the request entirely (the repo
    suite sets it; air-gapped authoring can too). Empty or unreachable yields
    the sentinel, so the dropdown always holds a valid value and the node's own
    runtime guard turns a selected sentinel into an actionable error.
    """
    names = _fetch_media_names(base_url, mode)
    return (names or [_catalog.NO_MODELS_SENTINEL])


def _model_widget(base_url: str, mode: str):
    options = _model_options(base_url, mode)
    return (list(options), {
        "default": options[0],
        "tooltip": f"Models on the proxy with model_info.mode == {mode!r}. "
                   f"Listed from GET /v1/models (cached "
                   f"{int(_DISCOVERY_TTL_S)}s); a model name can also be typed.",
    })


class _SparkrunBase:
    """Shared plumbing: the mode-checked model pre-flight and seed handling."""

    KIND = "image"  # subclasses set 'image' or 'video'

    @classmethod
    def _guard_model(cls, base_url: str, model: str):
        """Turn "the dropdown was empty when this graph was built" into a message.

        The proxy is the authority on what it serves, so this is a pre-flight
        against the same cached ``/v1/models`` the dropdown uses (one request
        per node run, not two). It fires only when the answer is already known
        to contradict the request, and stays out of the way whenever discovery
        itself was unavailable or disabled — the real request reports that.
        """
        name = (model or "").strip()
        if not name:
            raise SparkrunProxyError("no model selected")
        mode = _catalog.KIND_TO_MODE[cls.KIND]
        if name in (_catalog.NO_MODELS_SENTINEL, _catalog.UNREACHABLE_SENTINEL):
            raise SparkrunProxyError(
                f"{name}: nothing registered for mode {mode!r} on {base_url}. "
                f"Register the unit through hscc (the mode tag also needs the "
                f"sparkrun fork patch from media M3), or type the model name "
                f"into the dropdown.")
        allowed = _fetch_media_names(base_url, mode)
        if allowed and name not in allowed:
            raise SparkrunProxyError(
                f"{name!r} is not registered as {mode!r} on {base_url} "
                f"(registered for that mode: {', '.join(allowed) or 'none'}).")

    @staticmethod
    def _seed_value(seed):
        """Seed as int, or None when it cannot be one. 0 IS a valid seed."""
        try:
            if seed is None or (isinstance(seed, str) and not seed.strip()):
                return None
            return int(float(seed))
        except (TypeError, ValueError):
            return None


class SparkrunImage(_SparkrunBase):
    """Text -> image through the sparkrun proxy (POST /v1/images/generations)."""

    KIND = "image"

    def __init__(self):
        self.last_payload = None  # kept for the meta output and for tests

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL, "tooltip":
                    "sparkrun proxy root (port 4000). The proxy serves only models "
                    "registered through hscc; override the default with "
                    "SPARKRUN_PROXY_BASE_URL."}),
                "model": _model_widget(DEFAULT_BASE_URL, _catalog.IMAGE_MODE),
                "prompt": ("STRING", {"multiline": True, "default": "",
                                      "tooltip": "The text prompt."}),
                "size": (SIZE_CHOICES, {"default": SIZE_CHOICES[0], "tooltip":
                    "Forwarded verbatim to the backend."}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 200, "step": 1,
                                  "tooltip": "Diffusion steps, forwarded as the "
                                             "backend's `steps` parameter."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            },
            "optional": {
                "negative_prompt": ("STRING", {"multiline": True, "default": ""}),
            },
        }

    RETURN_TYPES = ("IMAGE", "DICT")
    RETURN_NAMES = ("image", "meta")
    FUNCTION = "generate"
    CATEGORY = "sparkrun"
    DESCRIPTION = ("Generate an image with an image_generation unit registered on the "
                   "sparkrun proxy. Output is a ComfyUI IMAGE tensor [1,H,W,3] in 0..1 "
                   "plus a DICT of request/response metadata.")

    def generate(self, base_url, model, prompt, size, steps, seed,
                 negative_prompt=""):
        self._guard_model(base_url, model)
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "response_format": "b64_json",
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        s = self._seed_value(seed)
        if s is not None:
            payload["seed"] = s
        if steps is not None:
            payload["steps"] = int(steps)
        self.last_payload = dict(payload)

        body = _http.post_json(_http.join_url(base_url, "/images/generations"), payload)
        items = _extract_image_items(body)
        if not items:
            raise SparkrunProxyError(
                f"proxy returned no image data for {model!r}: {_brief(body)}")
        blob = _image_item_to_bytes(items[0], base_url)
        tensor, meta = _media.png_bytes_to_image_tensor(blob)
        meta = dict(meta, model=model, size=size, steps=steps, seed=s,
                    endpoint="/v1/images/generations",
                    revised_prompt=items[0].get("revised_prompt") or "")
        return (tensor, meta)


class SparkrunImageEdit(_SparkrunBase):
    """Image (+ optional mask) -> edited image (POST /v1/images/edits, multipart)."""

    KIND = "image"

    def __init__(self):
        self.last_fields = None       # form fields actually sent
        self.last_file_fields = None  # field -> (filename, byte size, mime)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "model": _model_widget(DEFAULT_BASE_URL, _catalog.IMAGE_MODE),
                "image": ("IMAGE", {"tooltip": "Source image (first frame of a batch)."}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            },
            "optional": {
                "mask": ("MASK", {"tooltip": "Edit region, sent as a grayscale PNG "
                                             "`mask` part (backend-dependent)."}),
                "size": (SIZE_CHOICES, {"default": SIZE_CHOICES[0]}),
            },
        }

    RETURN_TYPES = ("IMAGE", "DICT")
    RETURN_NAMES = ("image", "meta")
    FUNCTION = "edit"
    CATEGORY = "sparkrun"
    DESCRIPTION = ("Edit an image with an image_generation unit on the sparkrun proxy. "
                   "The IMAGE input is PNG-encoded and posted as multipart/form-data; "
                   "an optional MASK becomes the `mask` part.")

    def edit(self, base_url, model, image, prompt, seed, mask=None,
             size=SIZE_CHOICES[0]):
        self._guard_model(base_url, model)
        png = _media.image_batch_to_first_png(image)
        fields = {"model": model, "prompt": prompt}
        if size:
            fields["size"] = size
        s = self._seed_value(seed)
        if s is not None:
            fields["seed"] = str(s)
        files = {"image": ("input.png", png, "image/png")}
        if mask is not None:
            files["mask"] = ("mask.png", _media.mask_batch_to_l_png(mask), "image/png")
        self.last_fields = dict(fields)
        self.last_file_fields = {k: (v[0], len(v[1]), v[2]) for k, v in files.items()}

        body = _http.post_multipart(_http.join_url(base_url, "/images/edits"),
                                    fields, files)
        items = _extract_image_items(body)
        if not items:
            raise SparkrunProxyError(
                f"proxy returned no image data for {model!r}: {_brief(body)}")
        blob = _image_item_to_bytes(items[0], base_url)
        tensor, meta = _media.png_bytes_to_image_tensor(blob)
        meta = dict(meta, model=model, size=size, seed=s,
                    masked=mask is not None, endpoint="/v1/images/edits")
        return (tensor, meta)


class SparkrunVideo(_SparkrunBase):
    """Text -> video: POST /v1/videos, poll the job, download the container."""

    KIND = "video"

    #: Statuses that end polling. Anything else keeps polling to the deadline,
    #: so an unfamiliar in-flight spelling (queued/created/in_progress/
    #: pending/...) behaves as in-flight rather than as a failure.
    DONE_STATES = ("completed", "succeeded", "success", "done", "ready")
    FAIL_STATES = ("failed", "error", "cancelled", "canceled", "expired")

    def __init__(self):
        self.last_payload = None
        self.polls = 0

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "model": _model_widget(DEFAULT_BASE_URL, _catalog.VIDEO_MODE),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "size": (SIZE_CHOICES, {"default": SIZE_CHOICES[0]}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 200, "step": 1}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 120, "step": 1}),
                "duration": ("FLOAT", {"default": 5.0, "min": 0.1, "max": 600.0,
                                       "step": 0.1, "tooltip":
                            "Requested clip length, sent as `seconds`."}),
                "poll_interval": ("FLOAT", {"default": 2.0, "min": 0.1, "max": 60.0,
                                            "step": 0.1, "tooltip":
                            "Seconds between GET /v1/videos/<id> polls."}),
                "poll_timeout": ("FLOAT", {"default": 900.0, "min": 1.0, "max": 7200.0,
                                           "step": 1.0, "tooltip":
                            "Fail the node after this many seconds polling."}),
            },
        }

    RETURN_TYPES = ("VIDEO", "DICT")
    RETURN_NAMES = ("video", "meta")
    FUNCTION = "generate"
    CATEGORY = "sparkrun"
    DESCRIPTION = ("Generate a video with a video_generation unit on the sparkrun "
                   "proxy: submit the job, poll it to completion, download the "
                   "container, emit a ComfyUI VIDEO.")

    def generate(self, base_url, model, prompt, size, steps, seed, fps, duration,
                 poll_interval, poll_timeout):
        import time

        self._guard_model(base_url, model)
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "fps": int(fps),
            "seconds": float(duration),
            "steps": int(steps),
        }
        s = self._seed_value(seed)
        if s is not None:
            payload["seed"] = s
        self.last_payload = dict(payload)

        submitted = _http.post_json(_http.join_url(base_url, "/videos"), payload)
        job_id = _extract_job_id(submitted)
        if not job_id:
            raise SparkrunProxyError(
                f"proxy returned no job id for {model!r}: {_brief(submitted)}")

        status_url = _http.join_url(base_url, f"/videos/{job_id}")
        deadline = time.monotonic() + float(poll_timeout)
        self.polls = 0
        status = ""
        job = submitted
        while True:
            self.polls += 1
            job = _http.get_json(status_url)
            status = str(_first(job, ("status", "state", "task_status")) or "").strip().lower()
            if status in self.DONE_STATES:
                break
            if status in self.FAIL_STATES:
                raise SparkrunProxyError(
                    f"video job {job_id} ended in {status!r}: {_brief(job)}")
            if time.monotonic() >= deadline:
                raise SparkrunProxyError(
                    f"video job {job_id} still {status or 'unknown'} after "
                    f"{poll_timeout}s ({self.polls} polls)")
            time.sleep(max(0.0, float(poll_interval)))

        blob, content_url = _video_content(job, base_url, job_id, _http)
        video = _media.video_bytes_to_output(blob)
        meta = {
            "model": model, "job_id": job_id, "status": status,
            "polls": self.polls, "size": size, "fps": fps,
            "duration": duration, "seed": s, "endpoint": "/v1/videos",
            "content_url": content_url, "bytes": len(blob),
        }
        return (video, meta)


# ── response-shape helpers: pure, offline-testable, module-level on purpose ──

def _brief(obj, limit=400) -> str:
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "…"


def _first(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _extract_image_items(body):
    """``{"data": [ ... ]}`` (or a bare list) → list of item dicts."""
    if isinstance(body, dict):
        data = body.get("data")
        if data is None:
            data = body.get("result") or body.get("images") or []
            if isinstance(data, dict):
                data = [data]
    elif isinstance(body, list):
        data = body
    else:
        data = []
    return [d for d in (data or []) if isinstance(d, dict)]


def _image_item_to_bytes(item, base_url) -> bytes:
    """One image item → PNG bytes, from ``b64_json`` or a (possibly relative) url."""
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64.strip():
        return _http.b64_to_bytes(b64)
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        return _http.download(_http.resolve_ref(base_url, url))
    raise SparkrunProxyError(f"image item carries neither b64_json nor url: {_brief(item)}")


def _extract_job_id(body):
    """Job id from the shapes the proxy/backends use (top level or one deep)."""
    if isinstance(body, dict):
        direct = _first(body, ("id", "job_id", "videoId", "task_id", "request_id"))
        if direct:
            return str(direct)
        for key in ("data", "result", "output", "job"):
            sub = body.get(key)
            if isinstance(sub, dict):
                nested = _first(sub, ("id", "job_id", "videoId", "task_id"))
                if nested:
                    return str(nested)
    return None


def _video_content(job, base_url, job_id, http):
    """``(bytes, url)`` for a finished video job.

    Every content location the job answer names is tried in order, then the
    conventional ``/v1/videos/<id>/content`` path (OpenAI's video surface). A
    signed URL that has expired is a common real answer, so a failed candidate
    falls through to the content path rather than failing the node.
    """
    candidates = []
    if isinstance(job, dict):
        out = job.get("output")
        out = out if isinstance(out, dict) else {}
        for src in (job, out):
            for key in ("output_url", "content_url", "download_url", "result_url",
                        "video_url", "url"):
                v = src.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
    blob = _first(job, ("content", "video"))
    if isinstance(blob, str) and blob.strip():
        # b64-inline content (some backends return the container inline)
        return _http.b64_to_bytes(blob), f"/v1/videos/{job_id}/content (inline b64)"
    for item in _extract_image_items(job):  # {"data": [{"url": ...}]} shape
        v = item.get("url")
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    for cand in candidates:
        url = http.join_url(base_url, "", absolute=cand)
        try:
            return http.download(url), url
        except SparkrunProxyError:
            continue
    fallback = http.join_url(base_url, f"/videos/{job_id}/content")
    return http.download(fallback), fallback


NODE_CLASS_MAPPINGS = {
    "SparkrunImage": SparkrunImage,
    "SparkrunImageEdit": SparkrunImageEdit,
    "SparkrunVideo": SparkrunVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SparkrunImage": "Sparkrun Image (proxy)",
    "SparkrunImageEdit": "Sparkrun Image Edit (proxy)",
    "SparkrunVideo": "Sparkrun Video (proxy)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS",
           "SparkrunImage", "SparkrunImageEdit", "SparkrunVideo"]
