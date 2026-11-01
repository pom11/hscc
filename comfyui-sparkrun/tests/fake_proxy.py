"""A fake sparkrun proxy: stdlib ``http.server`` + recorded requests.

Kept free of any ``pytest`` import on purpose. The pack's tests drive the real
node methods through it, and the same fixture is reusable by an integration
probe that runs inside ComfyUI's own venv — which has torch but NOT pytest.
Keeping it importable without pytest is what lets one fake serve both.

Answers the proxy's documented media surface:

    GET  /v1/models                    fixture catalogue (image/video/chat/untagged)
    POST /v1/images/generations        b64_json (or url / empty, via state)
    POST /v1/images/edits              multipart in, b64_json out
    POST /v1/videos                    job id (flat or nested shape)
    GET  /v1/videos/<id>               scripted status lifecycle
    GET  /v1/videos/<id>/content       container bytes
    GET  /v1/assets/out.{mp4,png}      absolute-URL variant
    GET  /raw                          a 200 whose body is not JSON

Every request lands on ``proxy.calls`` with method/path/headers/body, so tests
assert on the exact bytes the node sent rather than on "it returned something".
"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image


def make_png(width: int = 8, height: int = 6, color=(255, 0, 0)) -> bytes:
    """A real PNG: the decode path must parse actual bytes, not a stub."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = make_png()
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
CONTAINER_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"video-container-body" * 4


# The fixture catalogue. `sd-diffusion-t2i` / `wan-diffusion-t2v` are the shapes
# media M1/M3 produce (concrete id + alias, each with model_info.mode);
# `cluster-chat`, `legacy-untagged` and `wrong-mode-spelling` exist so the tests
# can prove a non-media entry is NEVER offered to a media node.
MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "sd-diffusion-t2i", "object": "model",
         "model_info": {"mode": "image_generation"}},
        {"id": "sd-diffusion-alias", "object": "model",
         "model_info": {"mode": "image_generation"}},
        {"id": "wan-diffusion-t2v", "object": "model",
         "model_info": {"mode": "video_generation"}},
        {"id": "cluster-chat", "object": "model",
         "model_info": {"mode": "chat"}},
        {"id": "legacy-untagged", "object": "model"},
        {"id": "wrong-mode-spelling", "object": "model",
         "model_info": {"mode": "image"}},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep pytest output readable
        pass

    @property
    def state(self):
        return self.server.state

    def _record(self, body=b""):
        self.state["calls"].append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _blob(self, raw: bytes, ctype="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        st = self.state
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._record()
            if st.get("models_status") == 500:
                return self._json({"error": "boom"}, status=500)
            return self._json(st["models"])
        if path in ("/v1/assets/out.mp4", "/v1/assets/out.png"):
            self._record()
            is_video = path.endswith(".mp4")
            return self._blob(CONTAINER_BYTES if is_video else PNG_BYTES,
                              "video/mp4" if is_video else "image/png")
        if path.startswith("/v1/videos/"):
            tail = path[len("/v1/videos/"):]
            if tail.endswith("/content"):
                self._record()
                if st.get("content_status"):
                    return self._json({"error": "no content"}, status=st["content_status"])
                return self._blob(CONTAINER_BYTES, "video/mp4")
            job_id = tail
            self._record()
            seq = st["video_status"]
            idx = min(st["video_polls"], len(seq) - 1)
            st["video_polls"] += 1
            status = seq[idx]
            job = {"id": job_id, "object": "video", "status": status,
                   "progress": 0.5 if status != "completed" else 1.0}
            if status in st["done_states_for_content"]:
                style = st.get("content_style")
                if style == "absolute_url":
                    job["output"] = {"url": st["absolute_content_url"]}
                elif style == "missing":
                    pass
                else:
                    job["output"] = {"url": f"/v1/videos/{job_id}/content"}
            return self._json(job)
        if path == "/raw":
            self._record()
            return self._blob(b"<html>not json</html>", "text/html")
        self._record()
        return self._json({"error": "not found"}, status=404)

    def do_POST(self):
        st = self.state
        path = self.path.split("?")[0]
        body = self._read_body()
        code = st["fail"].get(path)
        if code:
            self._record(body)
            return self._json({"error": {"message": f"{path} refused",
                                         "type": "proxy_error"}}, status=code)
        if path == "/v1/images/generations":
            self._record(body)
            style = st.get("image_style")
            if style == "url":
                reply = {"created": 1, "data": [{"url": "/v1/assets/out.png"}]}
            elif style == "empty":
                reply = {"created": 1, "data": []}
            else:
                reply = {"created": 1, "data": [{"b64_json": PNG_B64,
                                                 "revised_prompt": "revised"}]}
            return self._json(reply)
        if path == "/v1/images/edits":
            self._record(body)
            return self._json({"created": 1, "data": [{"b64_json": PNG_B64}]})
        if path == "/v1/videos":
            self._record(body)
            if st.get("record_submit"):
                st["submitted"] = json.loads(body.decode("utf-8")) if body else {}
            if st.get("submit_style") == "nested":
                return self._json({"data": {"job_id": "vid_job_42"}})
            return self._json({"id": "vid_job_42", "object": "video",
                               "status": "queued"})
        self._record(body)
        return self._json({"error": "not found"}, status=404)


class FakeProxy:
    """Handle on a running fake proxy: ``base_url`` + recorded ``calls``."""

    def __init__(self):
        self.state = {
            "calls": [],
            "models": json.loads(json.dumps(MODELS_PAYLOAD)),
            "video_status": ["queued", "processing", "completed"],
            "video_polls": 0,
            "fail": {},
            "content_style": "relative",
            "done_states_for_content": ("completed", "succeeded", "success"),
        }
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.state["absolute_content_url"] = f"{self.base_url}/v1/assets/out.mp4"

    @property
    def calls(self):
        return self.state["calls"]

    def last_call(self, method=None, path=None):
        for c in reversed(self.calls):
            if method and c["method"] != method:
                continue
            if path and not c["path"].startswith(path):
                continue
            return c
        return None

    def json_body(self, call):
        return json.loads(call["body"].decode("utf-8"))

    def set_models(self, payload):
        self.state["models"] = payload

    def fail_with(self, path, code):
        self.state["fail"][path] = code

    def queue_video_statuses(self, statuses):
        self.state["video_status"] = list(statuses)
        self.state["video_polls"] = 0

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class FakeTensor:
    """The tensor surface media.py uses, for hosts with no torch at all.

    Only the ENCODE path tolerates this (numpy-backed); the decode path needs a
    real ``torch`` module, which tests/conftest.py provides when absent.
    """

    def __init__(self, arr):
        self._arr = np.asarray(arr, dtype="float32")

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self._arr

    def unsqueeze(self, dim):
        """The one op the DECODE path uses (media adds the batch axis)."""
        out = np.expand_dims(self._arr, axis=dim)
        return FakeTensor(out)

    @property
    def shape(self):
        return self._arr.shape

    @property
    def ndim(self):
        return self._arr.ndim

    def __len__(self):
        return len(self._arr)

    def __getitem__(self, k):
        return FakeTensor(self._arr[k])


def rgb_batch(h=6, w=8, n=1, color=(1.0, 0.0, 0.0)):
    arr = np.zeros((n, h, w, 3), dtype="float32")
    arr[...] = np.array(color, dtype="float32")
    return FakeTensor(arr)


def mask_batch(h=6, w=8, n=1, value=0.5):
    return FakeTensor(np.full((n, h, w), value, dtype="float32"))


class FakeVideoFromFile:
    """Stands in for comfy_api's ``VideoFromFile`` where comfy_api is absent."""

    instances = []

    def __init__(self, file, **kw):
        self.file = file
        self.payload = file.getvalue() if hasattr(file, "getvalue") else b"<path>"
        FakeVideoFromFile.instances.append(self)

    def get_stream_source(self):
        return self.file


def fake_torch_module():
    """A module object shaped enough for media.png_bytes_to_image_tensor."""
    import types

    mod = types.ModuleType("torch")

    def from_numpy(arr):
        return FakeTensor(np.asarray(arr, dtype="float32"))

    mod.from_numpy = from_numpy
    mod.Tensor = FakeTensor
    return mod
